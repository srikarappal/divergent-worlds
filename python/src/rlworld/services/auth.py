"""Platform sign in and roles (DESIGN.md section 6.14, Admin 1).

Platform users sign in through the company IdP with OIDC, separate from the
synthetic identity plane worlds use. experimentd verifies each bearer token's
RS256 signature against the IdP's published keys, its issuer, audience and
lifetime, then takes roles from a token claim plus bindings admins manage.
Every route names the roles that may call it; writes nobody listed default to
admins only. Rollout workers keep using run tokens, which are scoped to a run.
"""

import base64
import binascii
import hashlib
import hmac
import json
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

import httpx
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa

ROLES = ("viewer", "researcher", "operator", "labeler", "admin")
DEFAULT_TENANT = "default"
EVERYONE = frozenset(ROLES)
BUILDERS = frozenset({"researcher", "operator", "admin"})
REVIEWERS = frozenset({"researcher", "operator", "labeler", "admin"})
OPERATORS = frozenset({"operator", "admin"})
ADMINS = frozenset({"admin"})
ROUTE_ROLES: dict[tuple[str, str], frozenset[str]] = {
    ("POST", "/v1/specs"): BUILDERS,
    ("POST", "/v1/specs/<digest>/preflight"): BUILDERS,
    ("POST", "/v1/specs/<digest>/dry_run"): BUILDERS,
    ("POST", "/v1/specs/<digest>/launch"): BUILDERS,
    ("POST", "/v1/experiments/<experiment_id>/approve"): ADMINS,
    ("POST", "/v1/runs/<run_id>/control"): BUILDERS,
    ("PUT", "/v1/catalogs/<kind>/<name>"): OPERATORS,
    ("POST", "/v1/endpoints"): BUILDERS,
    ("POST", "/v1/endpoints/<name>/test"): BUILDERS,
    ("PUT", "/v1/endpoints/<name>/key"): BUILDERS,
    ("POST", "/v1/runs/<run_id>/episodes/<episode_id>/actions"): REVIEWERS,
    ("POST", "/v1/runs/<run_id>/failures/analyze"): BUILDERS,
    ("POST", "/v1/review_items"): OPERATORS,
    ("PATCH", "/v1/review_items/<item_id>"): REVIEWERS,
    ("GET", "/v1/runs/<run_id>/exports/<kind>"): BUILDERS,
    ("GET", "/v1/admin/pools"): OPERATORS,
}


class AuthError(Exception):
    """No usable credentials: a missing, malformed, expired or forged token."""


class ForbiddenError(Exception):
    """Signed in, but none of the caller's roles may do this."""


@dataclass(frozen=True)
class Principal:
    subject: str
    email: str
    tenant: str
    roles: frozenset[str]


@dataclass(frozen=True)
class OidcSettings:
    issuer: str
    audience: str
    client_id: str
    role_claim: str = "roles"
    tenant_claim: str = "tenant"
    scopes: tuple[str, ...] = ("openid", "profile", "email")


DEVELOPMENT_PRINCIPAL = Principal(subject="developer", email="", tenant=DEFAULT_TENANT, roles=EVERYONE)


def base64url_bytes(segment: str) -> bytes:
    return base64.urlsafe_b64decode(segment + "=" * (-len(segment) % 4))


def rsa_public_key(jwk: Mapping[str, Any]) -> rsa.RSAPublicKey:
    modulus = int.from_bytes(base64url_bytes(jwk["n"]), "big")
    exponent = int.from_bytes(base64url_bytes(jwk["e"]), "big")
    return rsa.RSAPublicNumbers(exponent, modulus).public_key()


def signing_keys(jwks: Mapping[str, Any]) -> dict[str, rsa.RSAPublicKey]:
    return {jwk["kid"]: rsa_public_key(jwk) for jwk in jwks.get("keys", [])
            if jwk.get("kty") == "RSA" and jwk.get("use", "sig") == "sig" and "kid" in jwk}


class StaticJwks:
    def __init__(self, keys: Mapping[str, rsa.RSAPublicKey]) -> None:
        self.keys = dict(keys)

    def key(self, key_id: str) -> rsa.RSAPublicKey | None:
        return self.keys.get(key_id)


class HttpJwks:
    """The IdP's published keys, refreshed on a schedule or when a token names a key not seen yet."""

    def __init__(self, issuer: str, http_client: httpx.Client | None = None, refresh_seconds: float = 300.0,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.issuer = issuer.rstrip("/")
        self.http_client = http_client or httpx.Client(timeout=10.0)
        self.refresh_seconds = refresh_seconds
        self.clock = clock
        self.keys: dict[str, rsa.RSAPublicKey] = {}
        self.loaded_at = -refresh_seconds
        self.lock = threading.Lock()

    def load(self) -> None:
        configuration = self.http_client.get(f"{self.issuer}/.well-known/openid-configuration").json()
        keys = signing_keys(self.http_client.get(configuration["jwks_uri"]).json())
        with self.lock:
            self.keys = keys
            self.loaded_at = self.clock()

    def key(self, key_id: str) -> rsa.RSAPublicKey | None:
        with self.lock:
            stale = self.clock() - self.loaded_at >= self.refresh_seconds
            found = self.keys.get(key_id)
        if found is None or stale:
            if self.clock() - self.loaded_at >= min(self.refresh_seconds, 30.0):
                self.load()
            with self.lock:
                found = self.keys.get(key_id)
        return found


def claim_value(claims: Mapping[str, Any], dotted_path: str) -> Any:
    """Keycloak puts realm roles at realm_access.roles; other IdPs use a top level claim."""
    value: Any = claims
    for key in dotted_path.split("."):
        value = value.get(key) if isinstance(value, Mapping) else None
    return value


class OidcVerifier:
    def __init__(self, settings: OidcSettings, jwks: StaticJwks | HttpJwks, clock: Callable[[], float] = time.time,
                 leeway_seconds: float = 30.0) -> None:
        self.settings = settings
        self.jwks = jwks
        self.clock = clock
        self.leeway_seconds = leeway_seconds

    def verify(self, token: str) -> Principal:
        segments = token.split(".")
        if len(segments) != 3:
            raise AuthError("the token is not a signed JWT")
        try:
            header = json.loads(base64url_bytes(segments[0]))
            claims = json.loads(base64url_bytes(segments[1]))
            signature = base64url_bytes(segments[2])
        except (ValueError, binascii.Error) as error:
            raise AuthError("the token is malformed") from error
        if header.get("alg") != "RS256":
            raise AuthError("only RS256 signed tokens are accepted")
        key = self.jwks.key(str(header.get("kid", "")))
        if key is None:
            raise AuthError("the token is signed with an unknown key")
        try:
            key.verify(signature, f"{segments[0]}.{segments[1]}".encode("ascii"), padding.PKCS1v15(), hashes.SHA256())
        except InvalidSignature as error:
            raise AuthError("the token signature does not verify") from error
        self.check_claims(claims)
        roles = claim_value(claims, self.settings.role_claim)
        return Principal(
            subject=str(claims["sub"]),
            email=str(claims.get("email", "")),
            tenant=str(claim_value(claims, self.settings.tenant_claim) or DEFAULT_TENANT),
            roles=frozenset(role for role in roles if role in EVERYONE) if isinstance(roles, list) else frozenset(),
        )

    def check_claims(self, claims: Mapping[str, Any]) -> None:
        now = self.clock()
        audiences = claims.get("aud")
        audiences = [audiences] if isinstance(audiences, str) else audiences or []
        problems = (
            (claims.get("iss") != self.settings.issuer, "the token comes from another issuer"),
            (self.settings.audience not in audiences, "the token is for another audience"),
            (not isinstance(claims.get("exp"), (int, float)) or claims["exp"] < now - self.leeway_seconds,
             "the token expired"),
            (isinstance(claims.get("nbf"), (int, float)) and claims["nbf"] > now + self.leeway_seconds,
             "the token is not valid yet"),
            (not claims.get("sub"), "the token names no subject"),
        )
        for failed, message in problems:
            if failed:
                raise AuthError(message)


class RoleBindings:
    """Roles admins grant on top of what the token carries."""

    def __init__(self, bindings: Mapping[str, frozenset[str]] | None = None) -> None:
        self.bindings = dict(bindings or {})
        self.lock = threading.Lock()

    def set(self, subject: str, roles: frozenset[str]) -> frozenset[str]:
        unknown_roles = sorted(roles - EVERYONE)
        if unknown_roles:
            raise ValueError(f"unknown roles {', '.join(unknown_roles)}")
        with self.lock:
            self.bindings[subject] = roles
        return roles

    def granted(self, subject: str) -> frozenset[str]:
        with self.lock:
            return self.bindings.get(subject, frozenset())

    def all(self) -> dict[str, list[str]]:
        with self.lock:
            return {subject: sorted(roles) for subject, roles in sorted(self.bindings.items())}


class FrameGrants:
    """Short lived, per session read grants for frames, since img elements cannot send a bearer token."""

    def __init__(self, secret: bytes, ttl_seconds: float = 900.0, clock: Callable[[], float] = time.time) -> None:
        if not secret:
            raise ValueError("frame grants need a signing secret")
        self.secret = secret
        self.ttl_seconds = ttl_seconds
        self.clock = clock

    def sign(self, payload: str) -> str:
        return hmac.new(self.secret, payload.encode("utf-8"), hashlib.sha256).hexdigest()

    def issue(self, subject: str) -> tuple[str, float]:
        expires_at = int(self.clock() + self.ttl_seconds)
        payload = f"{base64.urlsafe_b64encode(subject.encode()).decode().rstrip('=')}.{expires_at}"
        return f"{payload}.{self.sign(payload)}", float(expires_at)

    def valid(self, grant: str) -> bool:
        payload, _, signature = grant.rpartition(".")
        expires_text = payload.rpartition(".")[2]
        return (bool(payload) and hmac.compare_digest(self.sign(payload), signature) and expires_text.isdigit()
                and int(expires_text) > self.clock())


def allowed_roles(method: str, rule: str) -> frozenset[str]:
    if (method, rule) in ROUTE_ROLES:
        return ROUTE_ROLES[(method, rule)]
    if rule.startswith("/v1/admin/"):
        return ADMINS
    return EVERYONE if method == "GET" else ADMINS


class PlatformAuth:
    def __init__(self, verifier: OidcVerifier, frame_grants: FrameGrants, bindings: RoleBindings | None = None) -> None:
        self.verifier = verifier
        self.frame_grants = frame_grants
        self.bindings = bindings or RoleBindings()

    def authorize(self, method: str, rule: str, authorization: str) -> Principal:
        if not authorization.startswith("Bearer "):
            raise AuthError("sign in first: the request carries no bearer token")
        principal = self.verifier.verify(authorization.removeprefix("Bearer ").strip())
        effective = Principal(principal.subject, principal.email, principal.tenant,
                              principal.roles | self.bindings.granted(principal.subject))
        needed_roles = allowed_roles(method, rule)
        if not effective.roles & needed_roles:
            raise ForbiddenError(f"{method} {rule} needs one of the roles {', '.join(sorted(needed_roles))}")
        return effective

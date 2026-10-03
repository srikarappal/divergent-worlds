"""Per episode sessions and short lived secrets (DESIGN.md section 6.5).

Sessions are minted after restore for one episode and never baked into a
snapshot: a thousand clones replaying one cookie looks like account takeover.
The signed minter stands in for the IdP token endpoint; the ephemeral issuer
stands in for vault dynamic secrets. Both can revoke and verify.
"""

import hashlib
import hmac
import secrets
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Protocol


@dataclass(frozen=True)
class Session:
    persona_key: str
    account: str
    token: str
    cookies: dict[str, str] = field(default_factory=dict)
    expires_at: float = 0.0


@dataclass(frozen=True)
class IssuedSecret:
    name: str
    value: str
    scope: str
    expires_at: float


class SessionMinter(Protocol):
    def mint(self, persona_key: str, account: str, episode_id: str, ttl_seconds: float) -> Session: ...

    def renew(self, session: Session, ttl_seconds: float) -> Session: ...

    def revoke(self, session: Session) -> None: ...


class SecretIssuer(Protocol):
    def issue(self, name: str, scope: str, ttl_seconds: float) -> IssuedSecret: ...

    def renew(self, issued_secret: IssuedSecret, ttl_seconds: float) -> IssuedSecret: ...

    def revoke(self, issued_secret: IssuedSecret) -> None: ...


class SignedSessionMinter:
    """Opaque HMAC signed tokens with a random nonce, so no two episodes ever share a session."""

    def __init__(self, signing_key: bytes, cookie_domain: str, clock: Callable[[], float]) -> None:
        if not signing_key:
            raise ValueError("the session minter needs a signing key")
        self.signing_key = signing_key
        self.cookie_domain = cookie_domain
        self.clock = clock
        self.revoked_tokens: set[str] = set()
        self.expiry_by_token: dict[str, float] = {}
        self.lock = threading.Lock()

    def sign(self, payload: str) -> str:
        return hmac.new(self.signing_key, payload.encode("utf-8"), hashlib.sha256).hexdigest()

    def mint(self, persona_key: str, account: str, episode_id: str, ttl_seconds: float) -> Session:
        expires_at = self.clock() + ttl_seconds
        payload = f"{account}.{episode_id}.{int(expires_at)}.{secrets.token_hex(8)}"
        token = f"{payload}.{self.sign(payload)}"
        with self.lock:
            self.expiry_by_token[token] = expires_at
        return Session(persona_key=persona_key, account=account, token=token,
                       cookies={f"session_{self.cookie_domain}": token}, expires_at=expires_at)

    def verify(self, token: str) -> bool:
        payload, _, signature = token.rpartition(".")
        if not payload or not hmac.compare_digest(self.sign(payload), signature):
            return False
        with self.lock:
            if token in self.revoked_tokens:
                return False
            return self.expiry_by_token.get(token, 0.0) > self.clock()

    def renew(self, session: Session, ttl_seconds: float) -> Session:
        """Extend the server side expiry; the cookie in the VM stays the same."""
        expires_at = self.clock() + ttl_seconds
        with self.lock:
            if session.token not in self.revoked_tokens:
                self.expiry_by_token[session.token] = expires_at
        return Session(persona_key=session.persona_key, account=session.account, token=session.token,
                       cookies=session.cookies, expires_at=expires_at)

    def revoke(self, session: Session) -> None:
        with self.lock:
            self.revoked_tokens.add(session.token)


class EphemeralSecretIssuer:
    def __init__(self, clock: Callable[[], float]) -> None:
        self.clock = clock
        self.active_secrets: dict[str, IssuedSecret] = {}
        self.lock = threading.Lock()

    def issue(self, name: str, scope: str, ttl_seconds: float) -> IssuedSecret:
        issued_secret = IssuedSecret(name=name, value=secrets.token_urlsafe(24), scope=scope,
                                     expires_at=self.clock() + ttl_seconds)
        with self.lock:
            self.active_secrets[issued_secret.value] = issued_secret
        return issued_secret

    def is_valid(self, value: str, scope: str) -> bool:
        with self.lock:
            issued_secret = self.active_secrets.get(value)
        return issued_secret is not None and issued_secret.scope == scope and issued_secret.expires_at > self.clock()

    def renew(self, issued_secret: IssuedSecret, ttl_seconds: float) -> IssuedSecret:
        renewed_secret = IssuedSecret(name=issued_secret.name, value=issued_secret.value, scope=issued_secret.scope,
                                      expires_at=self.clock() + ttl_seconds)
        with self.lock:
            if issued_secret.value in self.active_secrets:
                self.active_secrets[issued_secret.value] = renewed_secret
        return renewed_secret

    def revoke(self, issued_secret: IssuedSecret) -> None:
        with self.lock:
            self.active_secrets.pop(issued_secret.value, None)

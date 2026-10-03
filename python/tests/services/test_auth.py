import base64
import json

import httpx
import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from support.experiment_fakes import build_experiment_service, eval_payload

from rlworld.gateway import EndpointRegistry, StaticSecretStore
from rlworld.preflight.probes import StaticEndpointProbe
from rlworld.services.auth import (
    AuthError,
    ForbiddenError,
    FrameGrants,
    HttpJwks,
    OidcSettings,
    OidcVerifier,
    PlatformAuth,
    RoleBindings,
    StaticJwks,
)
from rlworld.services.catalogs import EndpointService
from rlworld.services.experimentd_http import create_app
from rlworld.specs import dump_spec_document, parse_spec_document

NOW = 1_800_000_000.0
SETTINGS = OidcSettings(issuer="https://sso.example.com/realms/platform", audience="rlworld", client_id="console",
                        role_claim="realm_access.roles")
PRIVATE_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
OTHER_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)


def segment(payload: dict) -> str:
    return base64.urlsafe_b64encode(json.dumps(payload).encode()).rstrip(b"=").decode()


def signed_token(roles=("researcher",), private_key=PRIVATE_KEY, header=None, **claim_updates) -> str:
    claims = {"iss": SETTINGS.issuer, "aud": ["rlworld", "account"], "sub": "user_ada", "email": "ada@example.com",
              "exp": NOW + 300, "nbf": NOW - 10, "tenant": "finance_lab", "realm_access": {"roles": list(roles)}}
    claims.update(claim_updates)
    signing_input = f"{segment(header or {'alg': 'RS256', 'kid': 'key_1'})}.{segment(claims)}"
    signature = private_key.sign(signing_input.encode(), padding.PKCS1v15(), hashes.SHA256())
    return f"{signing_input}.{base64.urlsafe_b64encode(signature).rstrip(b'=').decode()}"


def fixed_now() -> float:
    return NOW


def verifier() -> OidcVerifier:
    return OidcVerifier(SETTINGS, StaticJwks({"key_1": PRIVATE_KEY.public_key()}), clock=fixed_now)


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def test_valid_tokens_become_principals_with_known_roles():
    principal = verifier().verify(signed_token(roles=("researcher", "offline_access")))
    assert (principal.subject, principal.email, principal.tenant) == ("user_ada", "ada@example.com", "finance_lab")
    assert principal.roles == frozenset({"researcher"})


@pytest.mark.parametrize("token, reason", [
    (signed_token(iss="https://evil.example.com"), "another issuer"),
    (signed_token(aud="someone_else"), "another audience"),
    (signed_token(exp=NOW - 120), "expired"),
    (signed_token(nbf=NOW + 120), "not valid yet"),
    (signed_token(header={"alg": "none", "kid": "key_1"}), "only RS256"),
    (signed_token(header={"alg": "RS256", "kid": "key_9"}), "unknown key"),
    (signed_token(private_key=OTHER_KEY), "does not verify"),
    (signed_token().rsplit(".", 1)[0] + ".", "does not verify"),
    ("not.a", "not a signed JWT"),
    ("@@@.@@@.@@@", "malformed"),
])
def test_forged_expired_or_misdirected_tokens_are_rejected(token, reason):
    with pytest.raises(AuthError, match=reason):
        verifier().verify(token)


def test_tampered_claims_break_the_signature():
    header, _, signature = signed_token().split(".")
    tampered = f"{header}.{segment({'iss': SETTINGS.issuer, 'sub': 'mallory', 'aud': 'rlworld', 'exp': NOW + 60})}"
    with pytest.raises(AuthError, match="does not verify"):
        verifier().verify(f"{tampered}.{signature}")


class IdentityProvider:
    """Serves discovery and the key set; rotates to a second key on demand."""

    def __init__(self):
        self.keys = {"key_1": PRIVATE_KEY}
        self.requests = []

    def __call__(self, request):
        self.requests.append(request.url.path)
        if request.url.path.endswith("openid-configuration"):
            return httpx.Response(200, json={"jwks_uri": f"{SETTINGS.issuer}/protocol/openid-connect/certs"})
        jwks = []
        for key_id, key in self.keys.items():
            numbers = key.public_key().public_numbers()
            jwks.append({"kty": "RSA", "use": "sig", "kid": key_id, "alg": "RS256",
                         "n": base64.urlsafe_b64encode(numbers.n.to_bytes(256, "big")).rstrip(b"=").decode(),
                         "e": base64.urlsafe_b64encode(numbers.e.to_bytes(3, "big")).rstrip(b"=").decode()})
        return httpx.Response(200, json={"keys": jwks})


class ManualClock:
    def __init__(self):
        self.now = 0.0

    def time(self):
        return self.now


def test_published_keys_are_fetched_and_refreshed_after_rotation():
    provider = IdentityProvider()
    clock = ManualClock()
    jwks = HttpJwks(SETTINGS.issuer, httpx.Client(transport=httpx.MockTransport(provider)), clock=clock.time)
    http_verifier = OidcVerifier(SETTINGS, jwks, clock=fixed_now)
    assert http_verifier.verify(signed_token()).subject == "user_ada"
    provider.keys["key_2"] = OTHER_KEY
    rotated = signed_token(private_key=OTHER_KEY, header={"alg": "RS256", "kid": "key_2"})
    with pytest.raises(AuthError, match="unknown key"):
        http_verifier.verify(rotated)
    clock.now = 31.0
    assert http_verifier.verify(rotated).subject == "user_ada"
    assert len(provider.requests) == 4


def grants() -> FrameGrants:
    return FrameGrants(b"frame secret", ttl_seconds=60, clock=fixed_now)


def test_roles_gate_routes_and_bindings_add_roles():
    auth = PlatformAuth(verifier(), grants(), RoleBindings())
    with pytest.raises(AuthError, match="no bearer token"):
        auth.authorize("GET", "/v1/runs", "")
    viewer = f"Bearer {signed_token(roles=('viewer',))}"
    assert auth.authorize("GET", "/v1/runs", viewer).roles == frozenset({"viewer"})
    with pytest.raises(ForbiddenError, match="researcher"):
        auth.authorize("POST", "/v1/specs", viewer)
    with pytest.raises(ForbiddenError):
        auth.authorize("POST", "/v1/experiments/<experiment_id>/approve", f"Bearer {signed_token()}")
    auth.bindings.set("user_ada", frozenset({"admin"}))
    assert "admin" in auth.authorize("POST", "/v1/experiments/<experiment_id>/approve", viewer).roles
    with pytest.raises(ValueError, match="unknown roles"):
        auth.bindings.set("user_ada", frozenset({"root"}))


@pytest.fixture
def secured_app(example_payload, tmp_path):
    payload = eval_payload(example_payload)
    service = build_experiment_service(payload, tmp_path)
    endpoints = EndpointService(EndpointRegistry([]), StaticSecretStore({}), StaticEndpointProbe({}))
    app = create_app(service, endpoint_service=endpoints, auth=PlatformAuth(verifier(), grants(), RoleBindings()))
    return app.test_client(), service, dump_spec_document(parse_spec_document(payload))


def test_routes_need_a_signed_in_user_with_a_fitting_role(secured_app):
    client, service, spec_yaml = secured_app
    assert client.get("/healthz").status_code == 200
    assert client.get("/v1/auth/config").get_json() == {
        "enabled": True, "issuer": SETTINGS.issuer, "client_id": "console", "audience": "rlworld",
        "scopes": ["openid", "profile", "email"]}
    assert client.get("/v1/runs").status_code == 401
    viewer = bearer(signed_token(roles=("viewer",)))
    assert client.get("/v1/runs", headers=viewer).status_code == 200
    assert client.post("/v1/specs", data=spec_yaml, headers=viewer).status_code == 403
    researcher = bearer(signed_token())
    assert client.get("/v1/me", headers=researcher).get_json() == {
        "subject": "user_ada", "email": "ada@example.com", "tenant": "finance_lab", "roles": ["researcher"]}
    digest = client.post("/v1/specs", data=spec_yaml, headers=researcher).get_json()["digest"]
    launched = client.post(f"/v1/specs/{digest}/launch", json={"budget_usd": 900}, headers=researcher).get_json()
    assert launched["tenant"] == "finance_lab" and service.run(launched["run_ids"][0]).tenant == "finance_lab"
    run_token = service.issue_run_token(launched["run_ids"][0])
    assert client.post(f"/v1/runs/{launched['run_ids'][0]}/metrics", json={"episodes": 1},
                       headers=bearer(run_token)).status_code == 204


def test_every_write_is_audited_without_secrets(secured_app):
    client, _, spec_yaml = secured_app
    researcher = bearer(signed_token())
    admin = bearer(signed_token(roles=("admin",), sub="user_root"))
    digest = client.post("/v1/specs", data=spec_yaml, headers=researcher).get_json()["digest"]
    experiment = client.post(f"/v1/specs/{digest}/launch", json={"budget_usd": 900}, headers=researcher).get_json()
    client.post(f"/v1/runs/{experiment['run_ids'][0]}/control", json={"command": "stop"}, headers=researcher)
    client.post("/v1/endpoints", json={"name": "byo", "base_url": "https://judge.example/v1",
                                       "allowed_models": ["judge_v1"], "api_key_secret": "byo_key"}, headers=researcher)
    client.put("/v1/endpoints/byo/key", json={"key": "sk_live_secret"}, headers=researcher)
    assert client.put("/v1/admin/roles/user_ada", json={"roles": ["root"]}, headers=admin).status_code == 400
    assert client.put("/v1/admin/roles/user_ada", json={"roles": ["labeler"]}, headers=researcher).status_code == 403
    client.put("/v1/admin/roles/user_ada", json={"roles": ["labeler", "researcher"]}, headers=admin)
    assert client.get("/v1/admin/audit", headers=researcher).status_code == 403
    records = client.get("/v1/admin/audit", headers=admin).get_json()["records"]
    assert [record["action"] for record in reversed(records)] == [
        "save_spec", "launch", "control", "register_endpoint", "set_endpoint_key", "set_roles"]
    by_action = {record["action"]: record for record in records}
    assert by_action["save_spec"]["detail"] == {"digest": digest} and by_action["save_spec"]["actor"] == "user_ada"
    assert by_action["launch"]["detail"]["budget_usd"] == 900
    assert by_action["control"]["detail"] == {"command": "stop"}
    assert by_action["set_roles"]["target"] == {"subject": "user_ada"}
    assert by_action["set_roles"]["actor"] == "user_root"
    assert "sk_live_secret" not in json.dumps(records)
    assert client.get("/v1/admin/roles", headers=admin).get_json()["bindings"] == {
        "user_ada": ["labeler", "researcher"]}
    assert len(client.get("/v1/admin/audit?action=control", headers=admin).get_json()["records"]) == 1


def test_frames_open_with_a_session_grant_instead_of_a_bearer_token(secured_app):
    client, _, _ = secured_app
    assert client.get("/v1/frame_grant").status_code == 401
    grant = client.get("/v1/frame_grant", headers=bearer(signed_token(roles=("viewer",)))).get_json()
    assert grant["expires_at"] == NOW + 60
    frame_route = f"/v1/frames/{'0' * 64}"
    assert client.get(frame_route).status_code == 401
    assert client.get(f"{frame_route}?grant={grant['grant'][:-1]}0").status_code == 401
    assert client.get(f"{frame_route}?grant={grant['grant']}").status_code == 503
    expired = FrameGrants(b"frame secret", ttl_seconds=-1, clock=fixed_now).issue("user_ada")[0]
    assert client.get(f"{frame_route}?grant={expired}").status_code == 401

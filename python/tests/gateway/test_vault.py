import json

import httpx
import pytest

from rlworld.gateway.errors import MissingSecretError, UpstreamError
from rlworld.gateway.secrets import DevelopmentVault, VaultSecretStore


class VaultServer:
    """Plays Vault's KV v2 API: a token check and one value per secret path."""

    def __init__(self, failing=False):
        self.values = {}
        self.requests = []
        self.failing = failing

    def __call__(self, request):
        secret_path = request.url.raw_path.decode()
        self.requests.append((request.method, secret_path, request.headers.get("X-Vault-Token")))
        if self.failing:
            return httpx.Response(503, json={"errors": ["sealed"]})
        if request.method == "POST":
            self.values[secret_path] = json.loads(request.content)["data"]["value"]
            return httpx.Response(200, json={"data": {"version": 1}})
        if secret_path not in self.values:
            return httpx.Response(404, json={"errors": []})
        return httpx.Response(200, json={"data": {"data": {"value": self.values[secret_path]}}})


def vault(server):
    return VaultSecretStore("https://vault.internal:8200/", "s.token", mount="kv",
                            http_client=httpx.Client(transport=httpx.MockTransport(server)))


def test_endpoint_keys_are_written_and_read_through_vault():
    server = VaultServer()
    store = vault(server)
    store.put("byo judge/key", "sk_customer_123")
    assert store.get("byo judge/key") == "sk_customer_123"
    assert server.requests == [("POST", "/v1/kv/data/rlworld/endpoints/byo%20judge%2Fkey", "s.token"),
                               ("GET", "/v1/kv/data/rlworld/endpoints/byo%20judge%2Fkey", "s.token")]
    with pytest.raises(MissingSecretError, match="never_written"):
        store.get("never_written")


def test_a_vault_that_fails_is_an_upstream_error():
    store = vault(VaultServer(failing=True))
    with pytest.raises(UpstreamError, match="503"):
        store.get("key")
    with pytest.raises(UpstreamError, match="503"):
        store.put("key", "value")


def test_the_development_vault_layers_written_keys_over_the_environment(monkeypatch):
    monkeypatch.setenv("RLWORLD_SECRET_FRONTIER_KEY", "from_environment")
    store = DevelopmentVault()
    assert store.get("frontier_key") == "from_environment"
    store.put("frontier_key", "from_console")
    assert store.get("frontier_key") == "from_console"
    with pytest.raises(MissingSecretError):
        store.get("missing_key")

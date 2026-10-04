"""Secret stores for endpoint keys. Keys stay in the gateway and never reach a VM (DESIGN.md 6.5)."""

import os
import re
from collections.abc import Mapping
from typing import Protocol
from urllib.parse import quote

import httpx

from rlworld.gateway.errors import MissingSecretError, UpstreamError

ENVIRONMENT_PREFIX = "RLWORLD_SECRET_"


class SecretStore(Protocol):
    def get(self, secret_name: str) -> str: ...


class SecretVault(SecretStore, Protocol):
    """Where the endpoint registry writes keys; nothing ever reads them back out to a user."""

    def put(self, secret_name: str, secret_value: str) -> None: ...


class StaticSecretStore:
    """In memory store, also a vault for development and tests."""

    def __init__(self, secrets: Mapping[str, str]) -> None:
        self.secrets = dict(secrets)

    def get(self, secret_name: str) -> str:
        if secret_name not in self.secrets:
            raise MissingSecretError(f"secret {secret_name} is not set")
        return self.secrets[secret_name]

    def put(self, secret_name: str, secret_value: str) -> None:
        self.secrets[secret_name] = secret_value


class EnvironmentSecretStore:
    """Development store: RLWORLD_SECRET_<NAME> environment variables; production uses a vault."""

    def get(self, secret_name: str) -> str:
        variable_name = ENVIRONMENT_PREFIX + re.sub(r"[^A-Z0-9]", "_", secret_name.upper())
        secret_value = os.environ.get(variable_name)
        if not secret_value:
            raise MissingSecretError(f"secret {secret_name} is not set, expected {variable_name}")
        return secret_value


class DevelopmentVault:
    """A laptop's vault: keys written from the console live in memory over RLWORLD_SECRET_ environment variables."""

    def __init__(self) -> None:
        self.written = StaticSecretStore({})
        self.environment = EnvironmentSecretStore()

    def get(self, secret_name: str) -> str:
        if secret_name in self.written.secrets:
            return self.written.get(secret_name)
        return self.environment.get(secret_name)

    def put(self, secret_name: str, secret_value: str) -> None:
        self.written.put(secret_name, secret_value)


class VaultSecretStore:
    """Endpoint keys in HashiCorp Vault's KV version 2 engine (DESIGN.md 6.5): the console writes them, only the
    gateway reads them and nothing lists or returns them."""

    def __init__(self, base_url: str, token: str, mount: str = "secret", path_prefix: str = "rlworld/endpoints",
                 http_client: httpx.Client | None = None) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.mount = mount
        self.path_prefix = path_prefix.strip("/")
        self.http_client = http_client or httpx.Client(timeout=10.0)

    def secret_url(self, secret_name: str) -> str:
        return f"{self.base_url}/v1/{self.mount}/data/{self.path_prefix}/{quote(secret_name, safe='')}"

    def get(self, secret_name: str) -> str:
        response = self.http_client.get(self.secret_url(secret_name), headers={"X-Vault-Token": self.token})
        if response.status_code == 404:
            raise MissingSecretError(f"secret {secret_name} is not set")
        if response.status_code >= 400:
            raise UpstreamError(f"vault answered {response.status_code} reading {secret_name}")
        secret_value = ((response.json().get("data") or {}).get("data") or {}).get("value")
        if not secret_value:
            raise MissingSecretError(f"secret {secret_name} is not set")
        return secret_value

    def put(self, secret_name: str, secret_value: str) -> None:
        response = self.http_client.post(self.secret_url(secret_name), headers={"X-Vault-Token": self.token},
                                         json={"data": {"value": secret_value}})
        if response.status_code >= 400:
            raise UpstreamError(f"vault answered {response.status_code} writing {secret_name}")

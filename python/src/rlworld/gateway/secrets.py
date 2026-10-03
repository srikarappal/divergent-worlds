"""Secret stores for endpoint keys. Keys stay in the gateway and never reach a VM (DESIGN.md 6.5)."""

import os
import re
from collections.abc import Mapping
from typing import Protocol

from rlworld.gateway.errors import MissingSecretError

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

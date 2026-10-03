"""Endpoint registry: every model endpoint a run may call, with its pinned models and limits (DESIGN.md 6.8)."""

import threading
from operator import attrgetter
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, PositiveFloat, PositiveInt, model_validator

from rlworld.gateway.errors import EndpointNotFoundError


class EndpointConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(pattern=r"^[a-z0-9_]+$")
    kind: Literal["openai_compatible"] = "openai_compatible"
    base_url: str
    allowed_models: tuple[str, ...] = Field(min_length=1)
    api_key_secret: str | None = None
    provider_kind: Literal["frontier", "self_hosted", "user"] = "user"
    returns_token_ids: bool = False
    accepts_screenshots: bool = True
    max_concurrency: PositiveInt = 64
    requests_per_minute: PositiveInt | None = None
    tokens_per_minute: PositiveInt | None = None
    timeout_seconds: PositiveFloat = 120.0
    max_attempts: PositiveInt = 4
    input_price_per_million_usd: float | None = None
    output_price_per_million_usd: float | None = None
    cached_input_price_per_million_usd: float | None = None

    @model_validator(mode="after")
    def check_pinned_models(self) -> "EndpointConfig":
        moving_aliases = [model for model in self.allowed_models if "latest" in model.lower()]
        if moving_aliases:
            raise ValueError(f"endpoint {self.name} lists moving aliases {moving_aliases}; pin exact versions")
        return self


class DuplicateEndpointError(ValueError):
    """An endpoint name is already registered."""


class EndpointRegistry:
    def __init__(self, endpoints: list[EndpointConfig]) -> None:
        self.endpoints_by_name: dict[str, EndpointConfig] = {}
        self.lock = threading.Lock()
        for endpoint in endpoints:
            self.register(endpoint)

    def register(self, endpoint: EndpointConfig) -> EndpointConfig:
        """Adds a BYO endpoint; a registered name never changes meaning under running runs."""
        with self.lock:
            if endpoint.name in self.endpoints_by_name:
                raise DuplicateEndpointError(f"endpoint {endpoint.name} is registered twice")
            self.endpoints_by_name[endpoint.name] = endpoint
        return endpoint

    def endpoints(self) -> list[EndpointConfig]:
        with self.lock:
            return sorted(self.endpoints_by_name.values(), key=attrgetter("name"))

    def resolve(self, endpoint_reference: str) -> EndpointConfig:
        """A run spec may name an endpoint or give its base URL."""
        endpoint = self.endpoints_by_name.get(endpoint_reference)
        if endpoint is not None:
            return endpoint
        normalized_reference = endpoint_reference.rstrip("/")
        for candidate in self.endpoints_by_name.values():
            if candidate.base_url.rstrip("/") == normalized_reference:
                return candidate
        raise EndpointNotFoundError(f"no endpoint is registered as {endpoint_reference}")


def load_endpoint_registry(registry_path: Path) -> EndpointRegistry:
    payload = yaml.safe_load(registry_path.read_text()) or {}
    return EndpointRegistry([EndpointConfig.model_validate(entry) for entry in payload.get("endpoints", [])])

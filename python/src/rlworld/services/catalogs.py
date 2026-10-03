"""Catalogs and the endpoint registry behind the web console (DESIGN.md section 6.14).

Catalogs list images, worlds, task templates, verifiers, rubrics and endpoints
with their version, gate status, clone fidelity, pass rates per policy and how
many runs use them. Factory jobs publish images, worlds and task templates;
verifiers, rubrics and endpoints are read from their registries. Endpoint keys
go into the vault and are never read back.
"""

import threading
from collections.abc import Callable, Sequence
from operator import attrgetter
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from rlworld.gateway.config import EndpointConfig, EndpointRegistry
from rlworld.gateway.errors import GatewayError, MissingSecretError
from rlworld.gateway.secrets import SecretVault
from rlworld.judge.rubric import RubricError, RubricLibrary
from rlworld.preflight.probes import EndpointProbe, ProbeResult
from rlworld.rewards.verifiers import VerifierRegistry
from rlworld.specs.document import SpecDocument
from rlworld.specs.models import EndpointSpec

CatalogKind = Literal["images", "worlds", "task_templates", "verifiers", "rubrics", "endpoints"]
CATALOG_KINDS = ("images", "worlds", "task_templates", "verifiers", "rubrics", "endpoints")
PUBLISHED_KINDS = ("images", "worlds", "task_templates")


class UnknownCatalogError(KeyError):
    """No catalog has that kind."""


class CatalogWriteError(ValueError):
    """Verifiers, rubrics and endpoints come from their registries and cannot be published directly."""


class CatalogEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: CatalogKind
    name: str = Field(min_length=1)
    version: str = Field(min_length=1)
    description: str = ""
    gate_status: Literal["passed", "failed", "review"] | None = None
    clone_fidelity: float | None = Field(default=None, ge=0.0, le=1.0)
    pass_rates: dict[str, float] = Field(default_factory=dict)
    details: dict[str, Any] = Field(default_factory=dict)
    usage_runs: int = 0


def endpoint_entry(endpoint: EndpointConfig) -> CatalogEntry:
    return CatalogEntry(
        kind="endpoints",
        name=endpoint.name,
        version=", ".join(endpoint.allowed_models),
        description=endpoint.base_url,
        details={"provider_kind": endpoint.provider_kind, "returns_token_ids": endpoint.returns_token_ids,
                 "accepts_screenshots": endpoint.accepts_screenshots, "max_concurrency": endpoint.max_concurrency},
    )


class CatalogService:
    def __init__(
        self,
        run_documents: Callable[[], Sequence[SpecDocument]],
        verifier_registry: VerifierRegistry | None = None,
        rubric_library: RubricLibrary | None = None,
        endpoint_registry: EndpointRegistry | None = None,
    ) -> None:
        self.run_documents = run_documents
        self.verifier_registry = verifier_registry
        self.rubric_library = rubric_library
        self.endpoint_registry = endpoint_registry
        self.published: dict[str, dict[str, CatalogEntry]] = {kind: {} for kind in PUBLISHED_KINDS}
        self.lock = threading.Lock()

    def publish(self, entry: CatalogEntry) -> CatalogEntry:
        if entry.kind not in PUBLISHED_KINDS:
            raise CatalogWriteError(f"{entry.kind} come from their registry and cannot be published")
        with self.lock:
            self.published[entry.kind][entry.name] = entry
        return entry

    def registered_entries(self, kind: str) -> list[CatalogEntry]:
        if kind in PUBLISHED_KINDS:
            with self.lock:
                return list(self.published[kind].values())
        if kind == "verifiers" and self.verifier_registry is not None:
            return [CatalogEntry(kind="verifiers", name=verifier.name, version=verifier.version,
                                 details={"tier": verifier.tier})
                    for verifier in self.verifier_registry.verifiers.values()]
        if kind == "rubrics" and self.rubric_library is not None:
            return [self.rubric_entry(path.relative_to(self.rubric_library.root_directory).as_posix())
                    for path in sorted(self.rubric_library.root_directory.rglob("*.md"))]
        if kind == "endpoints" and self.endpoint_registry is not None:
            return [endpoint_entry(endpoint) for endpoint in self.endpoint_registry.endpoints()]
        return []

    def rubric_entry(self, relative_path: str) -> CatalogEntry:
        try:
            rubric = self.rubric_library.load(relative_path)
        except RubricError as error:
            return CatalogEntry(kind="rubrics", name=relative_path, version="invalid", description=str(error),
                                gate_status="failed")
        return CatalogEntry(kind="rubrics", name=relative_path, version=rubric.pinned_version,
                            description=rubric.guidance, details={"rubric_id": rubric.id, "items": len(rubric.items)})

    def endpoint_name(self, reference: str) -> str:
        """Run specs may name an endpoint or give its base URL."""
        if self.endpoint_registry is None:
            return reference
        try:
            return self.endpoint_registry.resolve(reference).name
        except GatewayError:
            return reference

    def references(self, document: SpecDocument, kind: str) -> set[str]:
        if kind == "images":
            return {document.image.id}
        if kind == "worlds":
            return {document.world.generator}
        if kind == "task_templates":
            return {document.task.generator}
        if kind == "verifiers":
            return {*document.task.verifiers.exact, *document.task.verifiers.semantic}
        if kind == "rubrics":
            return {document.task.judge_rubric} if document.task.judge_rubric else set()
        endpoints = [document.run.policy, *([document.run.judge] if document.run.judge else [])]
        return {self.endpoint_name(endpoint.endpoint) for endpoint in endpoints}

    def entries(self, kind: str) -> list[CatalogEntry]:
        if kind not in CATALOG_KINDS:
            raise UnknownCatalogError(kind)
        run_references = [self.references(document, kind) for document in self.run_documents()]
        return [entry.model_copy(update={"usage_runs": sum(entry.name in names for names in run_references)})
                for entry in sorted(self.registered_entries(kind), key=attrgetter("name"))]


class EndpointService:
    """Register BYO endpoints, probe them and store their keys write only (DESIGN.md 6.14, Configure 5)."""

    def __init__(self, registry: EndpointRegistry, vault: SecretVault, probe: EndpointProbe) -> None:
        self.registry = registry
        self.vault = vault
        self.probe = probe

    def has_key(self, endpoint: EndpointConfig) -> bool:
        if endpoint.api_key_secret is None:
            return False
        try:
            self.vault.get(endpoint.api_key_secret)
        except MissingSecretError:
            return False
        return True

    def view(self, endpoint: EndpointConfig) -> dict[str, Any]:
        return {**endpoint.model_dump(mode="json"), "has_key": self.has_key(endpoint)}

    def list(self) -> list[dict[str, Any]]:
        return [self.view(endpoint) for endpoint in self.registry.endpoints()]

    def register(self, endpoint: EndpointConfig) -> dict[str, Any]:
        return self.view(self.registry.register(endpoint))

    def test(self, name: str) -> ProbeResult:
        endpoint = self.registry.resolve(name)
        return self.probe.probe(EndpointSpec(endpoint=endpoint.name, model=endpoint.allowed_models[0],
                                             token_ids=endpoint.returns_token_ids,
                                             logprobs=endpoint.returns_token_ids))

    def set_key(self, name: str, key: str) -> None:
        endpoint = self.registry.resolve(name)
        if not endpoint.api_key_secret:
            raise CatalogWriteError(f"endpoint {name} names no api_key_secret, so it takes no key")
        if not key:
            raise CatalogWriteError("the key is empty")
        self.vault.put(endpoint.api_key_secret, key)

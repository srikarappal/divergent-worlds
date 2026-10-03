"""Admin records behind the console (DESIGN.md section 6.14, Admin 2 and 3).

The platform audit log keeps every launch, edit, stop, approval, role change and
key write with who did it; values that could be secret never enter it. Data
policies are per tenant: which endpoints may receive screenshots, how long
trajectories and full resolution frames are kept and which exports are allowed.
"""

import threading
import time
from collections.abc import Callable
from typing import Any, Literal

from pydantic import BaseModel, Field, PositiveInt, model_validator

from rlworld.services.auth import DEFAULT_TENANT

ExportKind = Literal["sft", "preferences"]


class AuditRecord(BaseModel):
    sequence: int
    at: float
    actor: str
    tenant: str
    action: str
    target: dict[str, Any] = Field(default_factory=dict)
    detail: dict[str, Any] = Field(default_factory=dict)


class PlatformAuditLog:
    def __init__(self, clock: Callable[[], float] = time.time) -> None:
        self.clock = clock
        self.records: list[AuditRecord] = []
        self.lock = threading.Lock()

    def record(self, actor: str, tenant: str, action: str, target: dict[str, Any], detail: dict[str, Any]) -> None:
        with self.lock:
            self.records.append(AuditRecord(sequence=len(self.records) + 1, at=self.clock(), actor=actor, tenant=tenant,
                                            action=action, target=target, detail=detail))

    def query(self, actor: str | None = None, action: str | None = None, since: float | None = None,
              limit: int = 500) -> list[AuditRecord]:
        """Newest first."""
        with self.lock:
            records = list(self.records)
        matching = [record for record in reversed(records)
                    if (actor is None or record.actor == actor) and (action is None or record.action == action)
                    and (since is None or record.at >= since)]
        return matching[:limit]


class DataPolicy(BaseModel):
    tenant: str = Field(min_length=1)
    screenshot_endpoints: list[str] | None = None
    trajectory_days: PositiveInt = 365
    full_frame_days: PositiveInt = 30
    exports: list[ExportKind] = ["sft", "preferences"]

    @model_validator(mode="after")
    def check_windows(self) -> "DataPolicy":
        if self.full_frame_days > self.trajectory_days:
            raise ValueError("full resolution frames cannot outlive the trajectories they belong to")
        return self


class DataPolicyStore:
    """Policies per tenant; a tenant without one gets the default policy."""

    def __init__(self, tenant_of_run: Callable[[str], str] | None = None) -> None:
        self.tenant_of_run = tenant_of_run
        self.policies: dict[str, DataPolicy] = {}
        self.lock = threading.Lock()

    def get(self, tenant: str) -> DataPolicy:
        with self.lock:
            return self.policies.get(tenant) or DataPolicy(tenant=tenant)

    def set(self, policy: DataPolicy) -> DataPolicy:
        with self.lock:
            self.policies[policy.tenant] = policy
        return policy

    def all(self) -> list[DataPolicy]:
        with self.lock:
            known = dict(self.policies)
        known.setdefault(DEFAULT_TENANT, DataPolicy(tenant=DEFAULT_TENANT))
        return [known[tenant] for tenant in sorted(known)]

    def run_policy(self, run_id: str) -> DataPolicy:
        tenant = self.tenant_of_run(run_id) if self.tenant_of_run is not None else DEFAULT_TENANT
        return self.get(tenant)

    def allows_screenshots(self, run_id: str, endpoint_name: str) -> bool:
        """The gateway asks before any frame leaves for an endpoint."""
        endpoints = self.run_policy(run_id).screenshot_endpoints
        return endpoints is None or endpoint_name in endpoints

    def allows_export(self, run_id: str, kind: str) -> bool:
        return kind in self.run_policy(run_id).exports

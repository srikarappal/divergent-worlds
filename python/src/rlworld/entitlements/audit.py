"""Entitlement audit log: the compliance record (DESIGN.md section 6.5).

Every grant, release, expiry and denial is an event. Seat hours per product
come from pairing grants with their release or expiry, which is what monthly
SPLA usage reports need. Secret values never enter the log, only names.
"""

import threading
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

AuditAction = Literal["grant", "release", "expire", "deny"]
ResourceKind = Literal["license", "host_license", "identity", "secret"]


@dataclass(frozen=True)
class AuditEvent:
    at: float
    action: AuditAction
    resource_kind: ResourceKind
    resource_name: str
    holder: str
    lease_id: str
    run_id: str
    tenant: str
    detail: str = ""


class AuditLog:
    def __init__(self) -> None:
        self.events: list[AuditEvent] = []
        self.lock = threading.Lock()

    def record(self, event: AuditEvent) -> None:
        with self.lock:
            self.events.append(event)

    def snapshot(self) -> list[AuditEvent]:
        with self.lock:
            return list(self.events)


def seat_hours(events: Sequence[AuditEvent], period_start: float, period_end: float) -> dict[str, float]:
    """License hours per product inside a period; grants still open are counted up to period_end."""
    open_grants: dict[tuple[str, str, str], float] = {}
    hours_by_product: dict[str, float] = defaultdict(float)
    for event in sorted(events, key=event_time):
        if event.resource_kind not in ("license", "host_license"):
            continue
        grant_key = (event.resource_kind, event.resource_name, event.holder)
        if event.action == "grant":
            open_grants[grant_key] = event.at
        elif event.action in ("release", "expire") and grant_key in open_grants:
            started_at = open_grants.pop(grant_key)
            hours_by_product[event.resource_name] += overlap_hours(started_at, event.at, period_start, period_end)
    for (_, product_name, _), started_at in open_grants.items():
        hours_by_product[product_name] += overlap_hours(started_at, period_end, period_start, period_end)
    return dict(hours_by_product)


def event_time(event: AuditEvent) -> float:
    return event.at


def overlap_hours(started_at: float, ended_at: float, period_start: float, period_end: float) -> float:
    return max(0.0, min(ended_at, period_end) - max(started_at, period_start)) / 3600

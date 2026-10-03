"""Synchronous client for the fleetd env API (DESIGN.md sections 5 and 6.3)."""

from dataclasses import asdict, dataclass
from typing import Any

import httpx
from pydantic import ValidationError

from rlworld.actions.models import ActionModel, Observation, action_payload


class FleetError(Exception):
    def __init__(self, status_code: int, code: str, message: str) -> None:
        super().__init__(f"{status_code} {code}: {message}")
        self.status_code = status_code
        self.code = code


class AdmissionRejectedError(FleetError):
    """The fleet, the tenant quota or the policy endpoint has no room right now; retry later."""


class NoCapacityError(FleetError):
    """No host can place the request."""


class UnknownLeaseError(FleetError):
    """The lease ended or never existed."""


class InputFrozenError(FleetError):
    """Finish already froze the env; only close is allowed."""


class EnvError(FleetError):
    """The environment failed; the episode is excluded from training and retried."""


class TakenOverError(FleetError):
    """An operator drives the env; the worker waits until the takeover ends."""


ERRORS_BY_STATUS = {429: AdmissionRejectedError, 503: NoCapacityError, 404: UnknownLeaseError,
                    409: InputFrozenError}
ERRORS_BY_CODE = {"taken_over": TakenOverError}


@dataclass(frozen=True)
class LeaseInfo:
    lease_id: str
    env_id: str
    host_id: str
    expires_at: str
    taken_over: bool = False


@dataclass(frozen=True)
class StepResult:
    observation: Observation | dict[str, Any]
    env_seconds: float
    settle_seconds: float
    settle_capped: bool


@dataclass(frozen=True)
class SnapshotSource:
    digest: str
    state_path: str
    memory_path: str
    base_disk_path: str


@dataclass(frozen=True)
class LeaseGroupRequest:
    run_id: str
    tenant: str
    pool: str
    priority: str
    endpoint: str
    snapshot: SnapshotSource
    memory_mib: int
    vcpus: int
    group_size: int = 1
    cpu_millicores: int = 2000
    gpus: int = 0
    cost_center: str = ""
    ttl_seconds: int = 300
    host_scoped_licenses: tuple[str, ...] = ()
    world_time: str | None = None
    observation_kinds: tuple[str, ...] = ()


def parse_observation(payload: Any) -> Observation | dict[str, Any]:
    """Canonical observations parse into the model; anything else stays a dict."""
    try:
        return Observation.model_validate(payload)
    except ValidationError:
        return payload


def lease_infos(payload: dict[str, Any]) -> list[LeaseInfo]:
    return [LeaseInfo(**entry) for entry in payload.get("leases") or []]


class FleetClient:
    def __init__(self, base_url: str, http_client: httpx.Client | None = None, timeout_seconds: float = 120.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.http_client = http_client or httpx.Client(timeout=timeout_seconds)

    def request(self, method: str, path: str, json_body: Any = None) -> Any:
        response = self.http_client.request(method, f"{self.base_url}{path}", json=json_body)
        if response.status_code >= 400:
            try:
                error_payload = response.json()
            except ValueError:
                error_payload = {"code": "unknown", "message": response.text}
            code = error_payload.get("code", "unknown")
            error_class = ERRORS_BY_CODE.get(code) or ERRORS_BY_STATUS.get(response.status_code, EnvError)
            raise error_class(response.status_code, code, error_payload.get("message", ""))
        return response.json() if response.content else None

    def lease_group(self, request: LeaseGroupRequest) -> list[LeaseInfo]:
        body = {
            "run_id": request.run_id,
            "tenant": request.tenant,
            "cost_center": request.cost_center,
            "pool": request.pool,
            "priority": request.priority,
            "endpoint": request.endpoint,
            "snapshot": asdict(request.snapshot),
            "cpu_millicores": request.cpu_millicores,
            "memory_mib": request.memory_mib,
            "gpus": request.gpus,
            "vcpus": request.vcpus,
            "group_size": request.group_size,
            "ttl_seconds": request.ttl_seconds,
            "host_scoped_licenses": list(request.host_scoped_licenses),
            "observation_kinds": list(request.observation_kinds),
        }
        if request.world_time is not None:
            body["world_time"] = request.world_time
        return lease_infos(self.request("POST", "/v1/leases", body))

    def heartbeat(self, lease_id: str) -> LeaseInfo:
        return LeaseInfo(**self.request("POST", f"/v1/leases/{lease_id}/heartbeat"))

    def step_result(self, payload: dict[str, Any]) -> StepResult:
        return StepResult(
            observation=parse_observation(payload.get("observation")),
            env_seconds=payload.get("env_seconds", 0.0),
            settle_seconds=payload.get("settle_seconds", 0.0),
            settle_capped=payload.get("settle_capped", False),
        )

    def reset(self, lease_id: str) -> StepResult:
        return self.step_result(self.request("POST", f"/v1/leases/{lease_id}/reset"))

    def step(self, lease_id: str, action: ActionModel) -> StepResult:
        return self.step_result(self.request("POST", f"/v1/leases/{lease_id}/step", {"action": action_payload(action)}))

    def fork(self, lease_id: str, count: int) -> list[LeaseInfo]:
        return lease_infos(self.request("POST", f"/v1/leases/{lease_id}/fork", {"count": count}))

    def checkpoint(self, lease_id: str) -> dict[str, str]:
        return self.request("POST", f"/v1/leases/{lease_id}/checkpoint")

    def finish(self, lease_id: str, apps: list[str]) -> dict[str, Any]:
        """Freezes input, then returns the extracted app state."""
        return self.request("POST", f"/v1/leases/{lease_id}/finish", {"apps": apps})["final_state"]

    def inject(self, lease_id: str, payload: dict[str, Any]) -> None:
        self.request("POST", f"/v1/leases/{lease_id}/inject", payload)

    def close(self, lease_id: str) -> None:
        self.request("DELETE", f"/v1/leases/{lease_id}")

    def start_takeover(self, lease_id: str) -> dict[str, Any]:
        """Hands the env to an operator: the session id and relay path noVNC connects to."""
        return self.request("POST", f"/v1/leases/{lease_id}/takeover")

    def end_takeover(self, lease_id: str) -> dict[str, Any]:
        """Gives the env back to its worker and returns the operator's recorded input."""
        return self.request("DELETE", f"/v1/leases/{lease_id}/takeover")

    def leases(self, run_id: str | None = None) -> list[LeaseInfo]:
        query = f"?run_id={run_id}" if run_id else ""
        return lease_infos(self.request("GET", f"/v1/leases{query}"))

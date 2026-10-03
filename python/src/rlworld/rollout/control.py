"""Run control for rollout workers inside trainer jobs (DESIGN.md sections 6.7 and 6.12).

A launched trainer job finds its run id, the service URLs and its run token in
the environment (the token as a variable on Kubernetes, as a file elsewhere).
The worker fetches the run's spec, polls the run's desired state and reports
spend, metrics and completion; every call carries the run token, which is
scoped to that one run.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import httpx

from rlworld.sdk.client import FleetClient
from rlworld.services.experiments import RunRecord
from rlworld.specs.document import SpecDocument, load_spec_document

REQUIRED_VARIABLES = ("RLWORLD_RUN_ID", "RLWORLD_FLEET_URL", "RLWORLD_EXPERIMENTD_URL", "RLWORLD_POLICY_ENDPOINT")


class RunControlError(Exception):
    """experimentd could not be reached or refused the call."""


class ExperimentdClient:
    def __init__(self, base_url: str, run_token: str, http_client: httpx.Client | None = None,
                 timeout_seconds: float = 30.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.run_token = run_token
        self.http_client = http_client or httpx.Client(timeout=timeout_seconds)

    def request(self, method: str, path: str, json_body: Any = None) -> Any:
        try:
            response = self.http_client.request(method, f"{self.base_url}{path}", json=json_body,
                                                headers={"Authorization": f"Bearer {self.run_token}"})
        except httpx.HTTPError as error:
            raise RunControlError(f"experimentd unreachable: {error}") from error
        if response.status_code >= 400:
            raise RunControlError(f"experimentd returned {response.status_code} for {path}: {response.text[:200]}")
        return response.json() if response.content else None

    def verify_token(self) -> str:
        return self.request("POST", "/v1/run_tokens/verify", {"token": self.run_token})["run_id"]

    def run(self, run_id: str) -> RunRecord:
        return RunRecord.model_validate(self.request("GET", f"/v1/runs/{run_id}"))

    def run_document(self, run_id: str) -> SpecDocument:
        return load_spec_document(self.request("GET", f"/v1/runs/{run_id}/spec")["yaml"])

    def record_spend(self, run_id: str, amount_usd: float) -> None:
        self.request("POST", f"/v1/runs/{run_id}/spend", {"usd": amount_usd})

    def publish_metrics(self, run_id: str, metrics: dict[str, Any]) -> None:
        self.request("POST", f"/v1/runs/{run_id}/metrics", metrics)

    def mark_finished(self, run_id: str, state: Literal["completed", "failed"], detail: str = "") -> None:
        self.request("POST", f"/v1/runs/{run_id}/finish", {"state": state, "detail": detail})


@dataclass(frozen=True)
class RunConnection:
    run_id: str
    fleet_url: str
    experimentd_url: str
    policy_endpoint: str
    run_token: str

    def experimentd(self, http_client: httpx.Client | None = None) -> ExperimentdClient:
        return ExperimentdClient(self.experimentd_url, self.run_token, http_client)

    def fleet(self, http_client: httpx.Client | None = None) -> FleetClient:
        return FleetClient(self.fleet_url, http_client)


def connection_from_environment(environment: Mapping[str, str]) -> RunConnection:
    """Reads what the trainer launcher put into the job's environment."""
    missing = [name for name in REQUIRED_VARIABLES if not environment.get(name)]
    run_token = environment.get("RLWORLD_RUN_TOKEN", "")
    token_file = environment.get("RLWORLD_RUN_TOKEN_FILE", "")
    if not run_token and token_file:
        run_token = Path(token_file).read_text().strip()
    if not run_token:
        missing.append("RLWORLD_RUN_TOKEN or RLWORLD_RUN_TOKEN_FILE")
    if missing:
        raise RunControlError(f"the trainer job environment lacks {', '.join(missing)}")
    return RunConnection(
        run_id=environment["RLWORLD_RUN_ID"],
        fleet_url=environment["RLWORLD_FLEET_URL"],
        experimentd_url=environment["RLWORLD_EXPERIMENTD_URL"],
        policy_endpoint=environment["RLWORLD_POLICY_ENDPOINT"],
        run_token=run_token,
    )

"""Python SDK for the experiment API, at parity with the web console and the CLI (DESIGN.md 6.7 and 6.14).

One method per public operation in schemas/api_operations.json. Run events
arrive as server sent events and are yielded as they come.
"""

from collections.abc import Iterator
from typing import Any

import httpx

from rlworld.preflight.preflight import ExperimentPreflight
from rlworld.services.catalogs import CatalogEntry
from rlworld.services.experiments import DryRun, ExperimentRecord, RunEvent, RunRecord
from rlworld.services.review import CohortEpisode, Comparison, EpisodeProvenance, FailureAnalysisView, ReviewItem
from rlworld.services.trajectory_views import LiveTile
from rlworld.trajectories.records import EpisodeRecord

EVENT_DATA_PREFIX = "data: "


class ExperimentApiError(Exception):
    def __init__(self, status_code: int, code: str, message: str, problems: list[str] | None = None) -> None:
        super().__init__(f"{status_code} {code}: {message}")
        self.status_code = status_code
        self.code = code
        self.problems = problems or []


def raise_for_error(response: httpx.Response) -> None:
    if response.status_code < 400:
        return
    try:
        payload = response.json()
    except ValueError:
        payload = {}
    problems = payload.get("problems") or []
    message = payload.get("message") or "; ".join(problems) or response.text[:200]
    raise ExperimentApiError(response.status_code, payload.get("code", "unknown"), message, problems)


class ExperimentClient:
    def __init__(self, base_url: str, http_client: httpx.Client | None = None, timeout_seconds: float = 60.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.http_client = http_client or httpx.Client(timeout=timeout_seconds)

    def request(self, method: str, path: str, json_body: Any = None, content: bytes | None = None,
                params: dict[str, Any] | None = None) -> Any:
        headers = {"Content-Type": "application/yaml"} if content is not None else None
        response = self.http_client.request(method, f"{self.base_url}{path}", json=json_body, content=content,
                                            params=params, headers=headers)
        raise_for_error(response)
        return response.json() if response.content else None

    def save_spec(self, spec_yaml: str) -> str:
        return self.request("POST", "/v1/specs", content=spec_yaml.encode("utf-8"))["digest"]

    def get_spec(self, digest: str) -> str:
        return self.request("GET", f"/v1/specs/{digest}")["yaml"]

    def preflight(self, digest: str) -> ExperimentPreflight:
        return ExperimentPreflight.model_validate(self.request("POST", f"/v1/specs/{digest}/preflight"))

    def dry_run(self, digest: str) -> DryRun:
        return DryRun.model_validate(self.request("POST", f"/v1/specs/{digest}/dry_run"))

    def launch(self, digest: str, budget_usd: float | None = None) -> ExperimentRecord:
        body = {} if budget_usd is None else {"budget_usd": budget_usd}
        return ExperimentRecord.model_validate(self.request("POST", f"/v1/specs/{digest}/launch", body))

    def get_experiment(self, experiment_id: str) -> ExperimentRecord:
        return ExperimentRecord.model_validate(self.request("GET", f"/v1/experiments/{experiment_id}"))

    def approve_experiment(self, experiment_id: str) -> ExperimentRecord:
        return ExperimentRecord.model_validate(self.request("POST", f"/v1/experiments/{experiment_id}/approve"))

    def list_runs(self, experiment_id: str | None = None) -> list[RunRecord]:
        params = {"experiment_id": experiment_id} if experiment_id else None
        return [RunRecord.model_validate(run) for run in self.request("GET", "/v1/runs", params=params)["runs"]]

    def get_run(self, run_id: str) -> RunRecord:
        return RunRecord.model_validate(self.request("GET", f"/v1/runs/{run_id}"))

    def get_run_spec(self, run_id: str) -> str:
        return self.request("GET", f"/v1/runs/{run_id}/spec")["yaml"]

    def control_run(self, run_id: str, command: str, value: Any = None) -> RunRecord:
        return RunRecord.model_validate(
            self.request("POST", f"/v1/runs/{run_id}/control", {"command": command, "value": value}))

    def run_events(self, run_id: str, follow: bool = False, after: int = 0) -> Iterator[RunEvent]:
        params = {"follow": str(follow).lower(), "after": after}
        with self.http_client.stream("GET", f"{self.base_url}/v1/runs/{run_id}/events", params=params) as response:
            if response.status_code >= 400:
                response.read()
                raise_for_error(response)
            for line in response.iter_lines():
                if line.startswith(EVENT_DATA_PREFIX):
                    yield RunEvent.model_validate_json(line.removeprefix(EVENT_DATA_PREFIX))

    def list_episodes(self, run_id: str, status: str | None = None) -> list[EpisodeRecord]:
        params = {"status": status} if status else None
        payload = self.request("GET", f"/v1/runs/{run_id}/episodes", params=params)
        return [EpisodeRecord.model_validate(episode) for episode in payload["episodes"]]

    def get_episode(self, run_id: str, episode_id: str, first_step: int = 0) -> EpisodeRecord:
        payload = self.request("GET", f"/v1/runs/{run_id}/episodes/{episode_id}", params={"first_step": first_step})
        return EpisodeRecord.model_validate(payload)

    def live_wall(self, run_id: str, limit: int = 24) -> list[LiveTile]:
        payload = self.request("GET", f"/v1/runs/{run_id}/live", params={"limit": limit})
        return [LiveTile.model_validate(tile) for tile in payload["tiles"]]

    def get_frame(self, frame_digest: str, size: str = "full") -> bytes:
        response = self.http_client.get(f"{self.base_url}/v1/frames/{frame_digest}", params={"size": size})
        raise_for_error(response)
        return response.content

    def cohort(self, run_id: str, **filters: Any) -> list[CohortEpisode]:
        params = {name: value for name, value in filters.items() if value is not None}
        payload = self.request("GET", f"/v1/runs/{run_id}/cohort", params=params)
        return [CohortEpisode.model_validate(member) for member in payload["episodes"]]

    def get_provenance(self, run_id: str, episode_id: str) -> EpisodeProvenance:
        return EpisodeProvenance.model_validate(
            self.request("GET", f"/v1/runs/{run_id}/episodes/{episode_id}/provenance"))

    def get_step_prompt(self, run_id: str, episode_id: str, step_index: int) -> list[dict[str, Any]]:
        return self.request("GET", f"/v1/runs/{run_id}/episodes/{episode_id}/steps/{step_index}/prompt")["messages"]

    def review_episode(self, run_id: str, episode_id: str, action: str, note: str = "",
                       label: dict[str, Any] | None = None) -> ReviewItem:
        body = {"action": action, "note": note, "label": label}
        return ReviewItem.model_validate(self.request("POST", f"/v1/runs/{run_id}/episodes/{episode_id}/actions", body))

    def analyze_failures(self, run_id: str) -> FailureAnalysisView:
        return FailureAnalysisView.model_validate(self.request("POST", f"/v1/runs/{run_id}/failures/analyze"))

    def get_failures(self, run_id: str) -> FailureAnalysisView:
        return FailureAnalysisView.model_validate(self.request("GET", f"/v1/runs/{run_id}/failures"))

    def run_report(self, run_id: str) -> dict[str, Any]:
        return self.request("GET", f"/v1/runs/{run_id}/report")

    def compare_policy_versions(self, run_id: str) -> Comparison:
        return Comparison.model_validate(self.request("GET", f"/v1/runs/{run_id}/compare/policy_versions"))

    def compare_runs(self, run_ids: list[str]) -> Comparison:
        return Comparison.model_validate(self.request("GET", "/v1/compare/runs", params={"run_ids": ",".join(run_ids)}))

    def list_review_items(self, queue: str | None = None, status: str | None = None, run_id: str | None = None,
                          episode_id: str | None = None) -> list[ReviewItem]:
        filters = {"queue": queue, "status": status, "run_id": run_id, "episode_id": episode_id}
        params = {name: value for name, value in filters.items() if value is not None}
        payload = self.request("GET", "/v1/review_items", params=params)
        return [ReviewItem.model_validate(item) for item in payload["items"]]

    def list_catalog(self, kind: str) -> list[CatalogEntry]:
        return [CatalogEntry.model_validate(entry) for entry in self.request("GET", f"/v1/catalogs/{kind}")["entries"]]

    def list_endpoints(self) -> list[dict[str, Any]]:
        return self.request("GET", "/v1/endpoints")["endpoints"]

    def register_endpoint(self, endpoint: dict[str, Any]) -> dict[str, Any]:
        return self.request("POST", "/v1/endpoints", endpoint)

    def test_endpoint(self, name: str) -> dict[str, Any]:
        return self.request("POST", f"/v1/endpoints/{name}/test")

    def set_endpoint_key(self, name: str, key: str) -> None:
        self.request("PUT", f"/v1/endpoints/{name}/key", {"key": key})

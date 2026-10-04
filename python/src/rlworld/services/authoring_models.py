"""What the task authoring flow returns (DESIGN.md sections 6.10 and 6.14), apart from the service so the SDK can
import it without the task registry."""

from typing import Any, Literal

from pydantic import BaseModel, Field, NonNegativeInt

RecordingKind = Literal["oracle", "alternative", "near_miss"]
GateStatus = Literal["not_run", "running", "passed", "failed", "error"]


class Recording(BaseModel):
    """One teleop attempt in a fresh env: the author's input as canonical actions and how the recording went."""

    recording_id: str
    kind: RecordingKind
    status: Literal["recording", "saving", "recorded", "failed"]
    recorded_by: str
    lease_id: str
    actions: list[dict[str, Any]] = Field(default_factory=list)
    complete: bool = True
    lost_reason: str = ""
    error: str = ""


class GateOutcome(BaseModel):
    name: str
    passed: bool
    detail: str
    attempts: list[dict[str, Any]] = Field(default_factory=list)


class AuthoringSession(BaseModel):
    session_id: str
    spec_digest: str
    author: str
    tenant: str
    task_id: str
    template: str
    instruction: str
    hidden_intent: str
    created_at: float
    recordings: list[Recording] = Field(default_factory=list)
    gate_status: GateStatus = "not_run"
    gate_results: list[GateOutcome] = Field(default_factory=list)
    gate_error: str = ""
    review_item_id: str | None = None


class AuthoringStart(BaseModel):
    spec_digest: str = Field(min_length=1)
    task_index: NonNegativeInt = 0


class RecordingRequest(BaseModel):
    kind: RecordingKind = "oracle"


class RecordingStart(BaseModel):
    """takeover_session_id is the relay's one time credential; it is returned here and stored nowhere."""

    recording: Recording
    takeover_session_id: str
    relay_path: str
    expires_at: str

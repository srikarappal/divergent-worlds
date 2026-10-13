"""Spec layers from DESIGN.md section 3: image, world, task, run and experiment.

Every model forbids unknown keys and is immutable, so a typo fails loudly and a
saved spec never changes underneath a run.
"""

import re
from datetime import UTC, datetime
from typing import Annotated, Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    NonNegativeInt,
    PositiveFloat,
    PositiveInt,
    model_validator,
)

NAME_PATTERN = r"^[a-z0-9_]+$"
DOTTED_PATH_PATTERN = r"^[a-z_][a-z0-9_]*(\.[a-z_][a-z0-9_]*)+$"
PINNED_BASE_PATTERN = re.compile(r"^[a-z0-9_.]+@sha256:[0-9a-f]{64}$")
SWEEPABLE_LAYERS = ("image", "world", "task", "run")

DottedPath = Annotated[str, Field(pattern=DOTTED_PATH_PATTERN)]
StatePathPattern = Annotated[str, Field(pattern=r"^[^/]+(/[^/]+)*$")]
OsFamily = Literal["linux", "windows", "macos", "android"]
# The Linux desktop session: a bare openbox window manager or a full Xfce desktop with panel, menu and file manager.
LinuxDesktop = Literal["openbox", "xfce"]
VmmKind = Literal["firecracker", "qemu_templated", "cloud_windows", "apple_virtualization", "cuttlefish"]
ActionModality = Literal["computer", "browser", "terminal", "native", "control"]
ActionFormat = Literal["computer_tool", "typed_actions", "text_actions"]
# Text action models point in pixels of the resized screenshot (Qwen2.5 VL, UI TARS) or on a 1000 by 1000 grid
# (Qwen3 VL and later).
CoordinateMode = Literal["absolute_resized", "relative_1000"]
ObservationKind = Literal["screenshot", "accessibility_tree", "dom", "terminal", "file_diff"]

VMM_KINDS_BY_OS: dict[str, frozenset[str]] = {
    "linux": frozenset({"firecracker", "qemu_templated"}),
    "windows": frozenset({"qemu_templated", "cloud_windows"}),
    "macos": frozenset({"apple_virtualization"}),
    "android": frozenset({"cuttlefish"}),
}


class SpecModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class PoolSpec(SpecModel):
    vmm: VmmKind
    machine: str | None = None
    cpu_model: str


class DisplaySpec(SpecModel):
    width: int = Field(ge=640, le=7680)
    height: int = Field(ge=480, le=4320)
    dpi: int = Field(default=96, ge=72, le=384)
    theme: Literal["light", "dark"] = "light"
    locale: str = "en_US"


class ReadyProbeSpec(SpecModel):
    window: str | None = None
    screen_stable_ms: int = Field(default=500, ge=100, le=10_000)
    timeout_s: int = Field(default=180, ge=1, le=3_600)


class ImageSpec(SpecModel):
    id: str = Field(pattern=NAME_PATTERN)
    os: OsFamily
    base: str
    pool: PoolSpec
    apps: dict[str, str] = Field(default_factory=dict)
    licenses: list[Annotated[str, Field(pattern=NAME_PATTERN)]] = Field(default_factory=list)
    display: DisplaySpec
    desktop: LinuxDesktop = "openbox"
    hardening: list[str] = Field(default_factory=list)
    ready_probe: ReadyProbeSpec = Field(default_factory=ReadyProbeSpec)

    @model_validator(mode="after")
    def check_base_and_pool(self) -> "ImageSpec":
        if not PINNED_BASE_PATTERN.match(self.base):
            raise ValueError(f"base must be pinned as name@sha256:<64 hex>, got {self.base!r}")
        if self.pool.vmm not in VMM_KINDS_BY_OS[self.os]:
            allowed_kinds = ", ".join(sorted(VMM_KINDS_BY_OS[self.os]))
            raise ValueError(f"vmm {self.pool.vmm} cannot run {self.os} guests, use one of: {allowed_kinds}")
        if self.os != "linux" and self.desktop != "openbox":
            raise ValueError(f"desktop {self.desktop} only applies to linux images")
        if self.pool.vmm == "qemu_templated" and not self.pool.machine:
            raise ValueError("qemu_templated pools must pin a machine type so snapshots stay restorable")
        return self


class IdentitySpec(SpecModel):
    idp: str = "keycloak"
    users: str
    sessions: Literal["mint_after_restore", "none"] = "mint_after_restore"


class NetworkSpec(SpecModel):
    dns_overrides: dict[str, str] = Field(default_factory=dict)
    trusted_ca: str | None = None
    internet: Literal["none", "replay_only", "allowlist", "live"] = "none"
    allowlist: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def check_network(self) -> "NetworkSpec":
        if self.internet == "allowlist" and not self.allowlist:
            raise ValueError("internet allowlist mode needs at least one allowlist entry")
        if self.internet != "allowlist" and self.allowlist:
            raise ValueError("allowlist entries only apply when internet is allowlist")
        if self.dns_overrides and not self.trusted_ca:
            raise ValueError("dns_overrides need a trusted_ca so HTTPS works for overridden hostnames")
        return self


class ClockSpec(SpecModel):
    fixed: datetime | None = None

    def lease_time(self) -> str | None:
        """The fixed time in RFC 3339 as leases carry it to fleetd; a time without an offset is UTC."""
        if self.fixed is None:
            return None
        return (self.fixed if self.fixed.tzinfo is not None else self.fixed.replace(tzinfo=UTC)).isoformat()


class WorldSpec(SpecModel):
    generator: DottedPath
    seed: NonNegativeInt
    size: dict[str, NonNegativeInt] = Field(default_factory=dict)
    materialize: list[str] = Field(default_factory=list)
    identity: IdentitySpec | None = None
    network: NetworkSpec = Field(default_factory=NetworkSpec)
    clock: ClockSpec = Field(default_factory=ClockSpec)


class DifficultySpec(SpecModel):
    apps: PositiveInt = 1
    oracle_steps: PositiveInt = 10
    distractors: NonNegativeInt = 0
    corrupted_inputs: NonNegativeInt = 0
    ambiguity: Literal["none", "low", "medium", "high"] = "none"
    dependency_depth: NonNegativeInt = 0


class UserSimulatorSpec(SpecModel):
    persona: str
    hidden_intent: str


class VerifierSetSpec(SpecModel):
    exact: list[DottedPath] = Field(default_factory=list)
    semantic: list[DottedPath] = Field(default_factory=list)


class SideEffectSpec(SpecModel):
    """State paths the task may change, "/" separated globs such as files/*.xlsx or mail/sent."""

    allowed_changes: list[StatePathPattern] = Field(default_factory=list)


class NetworkFaultSpec(SpecModel):
    latency_ms: tuple[NonNegativeInt, NonNegativeInt] = (0, 0)
    packet_loss: tuple[float, float] = (0.0, 0.0)

    @model_validator(mode="after")
    def check_ranges(self) -> "NetworkFaultSpec":
        lowest_latency, highest_latency = self.latency_ms
        if lowest_latency > highest_latency:
            raise ValueError(f"latency_ms range must be ordered, got {list(self.latency_ms)}")
        lowest_loss, highest_loss = self.packet_loss
        if not 0.0 <= lowest_loss <= highest_loss <= 1.0:
            raise ValueError(f"packet_loss range must be ordered within 0 and 1, got {list(self.packet_loss)}")
        return self


class TaskSpec(SpecModel):
    generator: DottedPath
    difficulty: DifficultySpec = Field(default_factory=DifficultySpec)
    allowed_actions: list[ActionModality]
    observation: list[ObservationKind] = Field(min_length=1)
    max_steps: PositiveInt = 50
    user_simulator: UserSimulatorSpec | None = None
    verifiers: VerifierSetSpec = Field(default_factory=VerifierSetSpec)
    side_effects: SideEffectSpec = Field(default_factory=SideEffectSpec)
    judge_rubric: str | None = None
    oracle: DottedPath
    network_faults: NetworkFaultSpec | None = None
    split: Literal["train", "eval", "holdout"]

    @model_validator(mode="after")
    def check_task(self) -> "TaskSpec":
        if len(set(self.allowed_actions)) != len(self.allowed_actions):
            raise ValueError("allowed_actions must not repeat a modality")
        if "control" not in self.allowed_actions:
            raise ValueError("allowed_actions must include control so the agent can finish or ask the user")
        if self.difficulty.ambiguity != "none" and self.user_simulator is None:
            raise ValueError("ambiguous tasks need a user_simulator to answer ask_user")
        if not (self.verifiers.exact or self.verifiers.semantic or self.judge_rubric):
            raise ValueError("a task needs a reward source: exact or semantic verifiers, or a judge rubric")
        return self


class EndpointSpec(SpecModel):
    endpoint: str
    model: str
    action_format: ActionFormat | None = None
    coordinate_mode: CoordinateMode = "absolute_resized"
    token_ids: bool = False
    logprobs: bool = False
    temperature: float = Field(default=1.0, ge=0.0, le=2.0)
    max_tokens: PositiveInt = 1024

    @model_validator(mode="after")
    def check_pinned_model(self) -> "EndpointSpec":
        if "latest" in self.model.lower():
            raise ValueError(f"model {self.model!r} looks like a moving alias, pin an exact version")
        if self.coordinate_mode != "absolute_resized" and self.action_format != "text_actions":
            raise ValueError("coordinate_mode applies to the text_actions format only")
        return self


class TrainerSpec(SpecModel):
    template: str = Field(pattern=NAME_PATTERN)
    launcher: Literal["kubernetes", "slurm", "skypilot", "external"]


class RunSpec(SpecModel):
    type: Literal["rl", "eval", "data"]
    trainer: TrainerSpec | None = None
    policy: EndpointSpec
    judge: EndpointSpec | None = None
    group_size: PositiveInt = 1
    tasks_per_batch: PositiveInt = 64
    batches: PositiveInt = 1
    max_concurrent_envs: PositiveInt
    priority: Literal["interactive", "eval", "training"]
    history: Literal["append_only", "sliding_window"] = "append_only"

    @model_validator(mode="after")
    def check_run(self) -> "RunSpec":
        if self.policy.action_format is None:
            raise ValueError("the policy needs an action_format so its output can be mapped to canonical actions")
        if self.type == "rl":
            if self.trainer is None:
                raise ValueError("rl runs need a trainer template and launcher")
            if not (self.policy.token_ids and self.policy.logprobs):
                raise ValueError(
                    "rl runs need policy token_ids and logprobs; generic chat endpoints are for eval and data runs"
                )
        elif self.trainer is not None:
            raise ValueError("only rl runs take a trainer")
        if self.max_concurrent_envs < self.group_size:
            raise ValueError("max_concurrent_envs must be at least group_size so one group can run at once")
        return self


class ExperimentSpec(SpecModel):
    name: str = Field(pattern=NAME_PATTERN)
    hypothesis: str = ""
    notes: str = ""
    sweep: dict[str, list[Any]] = Field(default_factory=dict)
    sweep_mode: Literal["grid", "random"] = "grid"
    random_samples: PositiveInt | None = None
    random_seed: NonNegativeInt = 0
    budget_usd: PositiveFloat
    tracker: Literal["wandb", "mlflow"] | None = None

    @model_validator(mode="after")
    def check_sweep(self) -> "ExperimentSpec":
        for sweep_path, sweep_values in self.sweep.items():
            is_dotted = re.fullmatch(DOTTED_PATH_PATTERN, sweep_path) is not None
            if not is_dotted or sweep_path.split(".")[0] not in SWEEPABLE_LAYERS:
                raise ValueError(f"sweep path {sweep_path!r} must point into the image, world, task or run layer")
            if not sweep_values:
                raise ValueError(f"sweep path {sweep_path!r} needs at least one value")
        if self.sweep_mode == "random" and self.random_samples is None:
            raise ValueError("random sweeps need random_samples")
        if self.sweep_mode == "grid" and self.random_samples is not None:
            raise ValueError("random_samples only applies to random sweeps")
        return self

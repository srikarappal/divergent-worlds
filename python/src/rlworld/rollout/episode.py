"""The episode loop (DESIGN.md section 5, steps 4 to 6).

Each turn shows the policy the latest observation, maps its reply to canonical
actions through the run's adapter and plays them: the env acts, waits for the
screen to settle, pauses the VM and observes, while ask_user goes to the user
simulator instead. A terminal action or the step budget ends the episode and
the finisher freezes input, extracts and scores state and records the
trajectory before the lease closes. Before each turn the task's scripted
world events and colleagues' chat replies land in its apps and the env is
observed again. Unparseable or disallowed actions are agent errors and count;
failures of the env, the policy endpoint or the user and colleague simulators
are env errors, which are excluded from training.
"""

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

import httpx

from rlworld.actions.adapters import ActionParseError
from rlworld.actions.models import (
    ActionModel,
    ActionNotAllowedError,
    AskUserAction,
    Observation,
    WaitAction,
    action_payload,
    ensure_actions_allowed,
)
from rlworld.entitlements.broker import EntitlementLease, EntitlementRequest
from rlworld.gateway.errors import GatewayError
from rlworld.gateway.messages import ChatResponse
from rlworld.rewards.pipeline import env_error_record
from rlworld.rewards.records import EpisodeOutcome, RewardRecord
from rlworld.rollout.policy import PolicyClient, assistant_reply
from rlworld.rollout.prompts import PastTurn, PromptBuilder
from rlworld.rollout.user_simulator import NpcSimulator, UserSimulator
from rlworld.rollout.world_events import MissingSimulatorError, NonPlayer, ScheduledDelivery, WorldScript
from rlworld.sdk.client import FleetError, LeaseGroupRequest, LeaseInfo, SnapshotSource, StepResult, TakenOverError
from rlworld.services.reward_service import EpisodeContext, EpisodeObserver
from rlworld.specs.models import TaskSpec
from rlworld.tasks.curriculum import difficulty_key
from rlworld.trajectories.records import EpisodeKey, EpisodeRecord, ModelTurn, StepRecord, StepTimings
from rlworld.trajectories.trajectory_store import TrajectoryStore, TrajectoryWriter, utc_timestamp

FLEET_FAILURES = (FleetError, httpx.HTTPError)
HIDDEN_INTENT_FROM_GENERATOR = "from_generator"
NO_USER_REPLY = "No user is available to answer; continue on your own."


class EnvFleet(Protocol):
    def lease_group(self, request: LeaseGroupRequest) -> list[LeaseInfo]: ...

    def heartbeat(self, lease_id: str) -> LeaseInfo: ...

    def step(self, lease_id: str, action: ActionModel) -> StepResult: ...

    def fork(self, lease_id: str, count: int) -> list[LeaseInfo]: ...

    def inject(self, lease_id: str, payload: dict[str, Any]) -> None: ...

    def close(self, lease_id: str) -> None: ...


class EpisodeFinisher(Protocol):
    def finish_episode(
        self, context: EpisodeContext, outcome: EpisodeOutcome, writer: TrajectoryWriter | None = None
    ) -> RewardRecord: ...


class EntitlementSource(Protocol):
    def acquire(self, request: EntitlementRequest) -> EntitlementLease: ...

    def renew(self, lease_id: str) -> EntitlementLease: ...

    def release(self, lease_id: str, action: str = "release") -> bool: ...


class EnvironmentFailure(Exception):
    """The env, the policy endpoint or the user simulator failed; the episode becomes an env error."""


class TakenOver(Exception):
    """An operator took the env over: the policy stops and the episode waits for the operator to hand back."""


@dataclass(frozen=True)
class TaskInstance:
    """One generated task over one world as the task registry hands it out; ground truth never enters the VM."""

    task_id: str
    template: str
    task: TaskSpec
    task_digest: str
    world_digest: str
    seed: int
    instruction: str
    snapshot: SnapshotSource
    initial_state: dict[str, Any]
    ground_truth: dict[str, Any]
    setup_payload: dict[str, Any] = field(default_factory=dict)
    hidden_intent: str = ""
    personas: tuple[str, ...] = ()
    secrets: tuple[str, ...] = ()
    world_events: tuple[ScheduledDelivery, ...] = ()
    npcs: tuple[NonPlayer, ...] = ()


@dataclass(frozen=True)
class EpisodeSettings:
    run_id: str
    run_type: Literal["rl", "eval", "data"]
    os_family: str
    apps: tuple[str, ...]
    history: Literal["append_only", "sliding_window"] = "append_only"


@dataclass(frozen=True)
class EpisodePlan:
    episode_id: str
    instance: TaskInstance
    policy_version: int
    group_id: str | None = None
    sample_index: int = 0


@dataclass(frozen=True)
class TurnResult:
    step: StepRecord
    next_observation: Observation | None = None
    reply: str = ""
    final_action: str | None = None
    answer_text: str | None = None
    agent_error: str | None = None
    env_error: str | None = None

    @property
    def ends_episode(self) -> bool:
        return bool(self.final_action or self.agent_error or self.env_error)


def describe(error: Exception) -> str:
    return f"{type(error).__name__}: {error}"


class StepTimingTotals:
    """Model, env and settle time summed over every step the runner wrote, for the run dashboard's latency split."""

    def __init__(self) -> None:
        self.steps = 0
        self.model_seconds = 0.0
        self.env_seconds = 0.0
        self.settle_seconds = 0.0
        self.lock = threading.Lock()

    def add(self, timings: StepTimings) -> None:
        with self.lock:
            self.steps += 1
            self.model_seconds += timings.model_seconds
            self.env_seconds += timings.env_seconds
            self.settle_seconds += timings.settle_seconds

    def metrics(self) -> dict[str, float | int | None]:
        with self.lock:
            steps = self.steps
            return {
                "steps": steps,
                "mean_model_seconds": self.model_seconds / steps if steps else None,
                "mean_env_seconds": self.env_seconds / steps if steps else None,
                "mean_settle_seconds": self.settle_seconds / steps if steps else None,
            }


class LeaseKeeper:
    """Heartbeats an env lease and renews its entitlements while the episode runs."""

    def __init__(
        self,
        fleet: EnvFleet,
        lease_id: str,
        entitlements: EntitlementSource | None,
        entitlement_lease_id: str | None,
        interval_seconds: float,
        clock: Callable[[], float],
    ) -> None:
        self.fleet = fleet
        self.lease_id = lease_id
        self.entitlements = entitlements
        self.entitlement_lease_id = entitlement_lease_id
        self.interval_seconds = interval_seconds
        self.clock = clock
        self.renewed_at = clock()

    def keep_alive(self) -> None:
        if self.clock() - self.renewed_at < self.interval_seconds:
            return
        try:
            self.fleet.heartbeat(self.lease_id)
            if self.entitlements is not None and self.entitlement_lease_id is not None:
                self.entitlements.renew(self.entitlement_lease_id)
        except (*FLEET_FAILURES, KeyError) as error:
            raise EnvironmentFailure(f"lease renewal failed: {describe(error)}") from error
        self.renewed_at = self.clock()


def model_turn(response: ChatResponse, prompt_ref: str) -> ModelTurn:
    return ModelTurn(
        prompt_ref=prompt_ref,
        output_text=response.text,
        reasoning=response.reasoning,
        tool_calls=response.tool_calls,
        token_trace=response.token_trace,
        served_model=response.served_model,
        input_tokens=response.usage.input_tokens,
        output_tokens=response.usage.output_tokens,
        cost_usd=response.cost_usd,
    )


class EpisodeRunner:
    def __init__(
        self,
        fleet: EnvFleet,
        policy: PolicyClient,
        finisher: EpisodeFinisher,
        trajectory_store: TrajectoryStore,
        settings: EpisodeSettings,
        user_simulator: UserSimulator | None = None,
        entitlements: EntitlementSource | None = None,
        heartbeat_seconds: float = 60.0,
        clock: Callable[[], float] = time.monotonic,
        takeover_poll_seconds: float = 5.0,
        sleep: Callable[[float], None] = time.sleep,
        npc_simulator: NpcSimulator | None = None,
        observer: EpisodeObserver | None = None,
    ) -> None:
        self.fleet = fleet
        self.policy = policy
        self.finisher = finisher
        self.trajectory_store = trajectory_store
        self.settings = settings
        self.user_simulator = user_simulator
        self.entitlements = entitlements
        self.heartbeat_seconds = heartbeat_seconds
        self.clock = clock
        self.takeover_poll_seconds = takeover_poll_seconds
        self.sleep = sleep
        self.npc_simulator = npc_simulator
        self.observer = observer
        self.step_timings = StepTimingTotals()

    def episode_record(self, plan: EpisodePlan, lease_id: str | None = None) -> EpisodeRecord:
        instance = plan.instance
        return EpisodeRecord(
            episode_id=plan.episode_id,
            key=EpisodeKey(
                image_snapshot_digest=instance.snapshot.digest,
                world_digest=instance.world_digest,
                task_digest=instance.task_digest,
                run_id=self.settings.run_id,
                seed=instance.seed,
                policy_version=plan.policy_version,
            ),
            task_id=instance.task_id,
            task_template=instance.template,
            difficulty_key=difficulty_key(instance.task.difficulty),
            instruction=instance.instruction,
            group_id=plan.group_id,
            sample_index=plan.sample_index,
            run_type=self.settings.run_type,
            lease_id=lease_id,
            started_at=utc_timestamp(),
        )

    def record_env_error(self, plan: EpisodePlan, detail: str) -> EpisodeRecord:
        """An episode that never got a usable env, recorded so env error rates stay honest."""
        instance = plan.instance
        writer = self.trajectory_store.start_episode(self.episode_record(plan), instance.initial_state,
                                                     instance.ground_truth)
        if self.observer is not None:
            self.observer.episode_scored("env_error")
        return writer.finish(env_error_record(detail, {}, instance.ground_truth), {})

    def wait_for_takeover(self, lease_id: str, keeper: LeaseKeeper) -> str | None:
        """The operator drives until they hand back; heartbeats keep the lease and its entitlements alive meanwhile."""
        while True:
            try:
                taken_over = self.fleet.heartbeat(lease_id).taken_over
                keeper.keep_alive()
            except (*FLEET_FAILURES, EnvironmentFailure) as error:
                return f"lease lost during the takeover: {describe(error)}"
            if not taken_over:
                return None
            self.sleep(self.takeover_poll_seconds)

    def run(self, plan: EpisodePlan, lease: LeaseInfo, entitlement_lease_id: str | None = None) -> EpisodeRecord:
        """Plays the policy until the episode ends; after an operator takes over, their result is what gets scored."""
        instance = plan.instance
        writer = self.trajectory_store.start_episode(self.episode_record(plan, lease.lease_id), instance.initial_state,
                                                     instance.ground_truth)
        keeper = LeaseKeeper(self.fleet, lease.lease_id, self.entitlements, entitlement_lease_id,
                             self.heartbeat_seconds, self.clock)
        builder = PromptBuilder(self.settings.os_family, instance.instruction, self.policy.geometry,
                                self.policy.tool_definitions(instance.task.allowed_actions), self.settings.history)
        script = WorldScript(instance.world_events, instance.npcs, self.npc_simulator)
        past_turns: list[PastTurn] = []
        last_turn: TurnResult | None = None
        final_observation: Observation | None = None
        env_error = None
        try:
            observation = self.act(lease.lease_id, WaitAction(duration_ms=0)).observation
            script.observe(observation)
            while len(past_turns) < instance.task.max_steps:
                observation, delivered = self.deliver_world(lease.lease_id, script, len(past_turns), observation)
                last_turn = self.play_turn(plan, lease.lease_id, keeper, writer, builder, past_turns, observation,
                                           script, delivered)
                writer.write_step(last_turn.step)
                self.step_timings.add(last_turn.step.timings)
                final_observation = last_turn.next_observation or last_turn.step.observation
                if last_turn.ends_episode:
                    break
                past_turns.append(PastTurn(last_turn.step.observation, last_turn.reply))
                observation = last_turn.next_observation
        except TakenOver:
            writer.mark_human_intervened()
            env_error = self.wait_for_takeover(lease.lease_id, keeper)
        except EnvironmentFailure as error:
            env_error = str(error)
        except Exception as error:  # a bug must still end in a labeled episode and a closed lease
            env_error = f"unexpected {describe(error)}"
        outcome = EpisodeOutcome(
            steps_taken=writer.next_step_index,
            final_action=last_turn.final_action if last_turn else None,
            answer_text=last_turn.answer_text if last_turn else None,
            agent_error=last_turn.agent_error if last_turn else None,
            env_error=env_error or (last_turn.env_error if last_turn else None),
        )
        screenshot = final_observation.screenshot if final_observation else None
        context = EpisodeContext(
            lease_id=lease.lease_id,
            task=instance.task,
            apps=self.settings.apps,
            initial_state=instance.initial_state,
            ground_truth=instance.ground_truth,
            instruction=instance.instruction,
            screenshots=(screenshot.uri,) if screenshot else (),
            run_type=self.settings.run_type,
            world_paths=tuple(script.delivered_paths),
        )
        self.finisher.finish_episode(context, outcome, writer)
        return writer.episode

    def deliver_world(self, lease_id: str, script: WorldScript, turn_index: int,
                      observation: Observation) -> tuple[Observation, tuple[str, ...]]:
        """Events due at this turn and colleagues' replies land in the apps; the agent then sees the env anew."""
        try:
            deliveries = script.due(turn_index)
        except (MissingSimulatorError, GatewayError) as error:
            raise EnvironmentFailure(f"colleague simulator failed: {describe(error)}") from error
        if not deliveries:
            return observation, ()
        payload = {"deliveries": [{"app": delivery.app, "record": delivery.record} for delivery in deliveries]}
        try:
            self.fleet.inject(lease_id, payload)
        except TakenOverError as error:
            raise TakenOver(str(error)) from error
        except FLEET_FAILURES as error:
            raise EnvironmentFailure(f"delivering world events failed: {describe(error)}") from error
        script.record_delivered(deliveries)
        observed = self.act(lease_id, WaitAction(duration_ms=0)).observation
        script.observe(observed)
        return observed, tuple(delivery.label for delivery in deliveries)

    def play_turn(
        self,
        plan: EpisodePlan,
        lease_id: str,
        keeper: LeaseKeeper,
        writer: TrajectoryWriter,
        builder: PromptBuilder,
        past_turns: list[PastTurn],
        observation: Observation,
        script: WorldScript,
        delivered: tuple[str, ...] = (),
    ) -> TurnResult:
        turn_index = len(past_turns)
        observation = observation.model_copy(update={"step_index": turn_index})
        messages = builder.messages(past_turns, observation)
        keeper.keep_alive()
        try:
            response = self.policy.complete(messages, builder.tools, plan.sample_index)
        except GatewayError as error:
            return TurnResult(StepRecord(step_index=turn_index, observation=observation, vm_events=delivered),
                              env_error=f"policy call failed: {describe(error)}")
        turn = model_turn(response, writer.put_prompt(messages))
        try:
            actions = self.policy.parse(response)
            ensure_actions_allowed(actions, plan.instance.task.allowed_actions)
        except (ActionParseError, ActionNotAllowedError) as error:
            step = StepRecord(step_index=turn_index, observation=observation, model_turn=turn, parse_error=str(error),
                              timings=StepTimings(model_seconds=response.latency_seconds), vm_events=delivered)
            return TurnResult(step, agent_error=str(error))
        return self.play_actions(plan, lease_id, actions, observation, turn, response, script, delivered)

    def play_actions(
        self,
        plan: EpisodePlan,
        lease_id: str,
        actions: list[ActionModel],
        observation: Observation,
        turn: ModelTurn,
        response: ChatResponse,
        script: WorldScript,
        delivered: tuple[str, ...] = (),
    ) -> TurnResult:
        played: list[dict[str, Any]] = []
        user_replies: list[str] = []
        vm_events = list(delivered)
        next_observation = observation
        env_seconds = settle_seconds = 0.0
        final_action = answer_text = env_error = None
        try:
            for action in actions:
                played.append(action_payload(action))
                if action.ends_episode:
                    final_action, answer_text = action.name, getattr(action, "text", None)
                    break
                if isinstance(action, AskUserAction):
                    user_replies.append(self.ask_user(plan.instance, action.question))
                    continue
                result = self.act(lease_id, action)
                script.observe(result.observation)
                env_seconds += result.env_seconds
                settle_seconds += result.settle_seconds
                if result.settle_capped:
                    vm_events.append(f"settle_capped after {action.name}")
                next_observation = result.observation
        except EnvironmentFailure as error:
            env_error = str(error)
        if user_replies:
            next_observation = next_observation.model_copy(update={"user_reply": "\n".join(user_replies)})
        step = StepRecord(
            step_index=observation.step_index,
            observation=observation,
            model_turn=turn,
            actions=tuple(played),
            timings=StepTimings(model_seconds=response.latency_seconds, env_seconds=env_seconds,
                                settle_seconds=settle_seconds),
            vm_events=tuple(vm_events),
        )
        return TurnResult(step, next_observation, assistant_reply(response), final_action, answer_text,
                          env_error=env_error)

    def act(self, lease_id: str, action: ActionModel) -> StepResult:
        try:
            result = self.fleet.step(lease_id, action)
        except TakenOverError as error:
            raise TakenOver(str(error)) from error
        except FLEET_FAILURES as error:
            raise EnvironmentFailure(f"{action.name} step failed: {describe(error)}") from error
        if not isinstance(result.observation, Observation):
            raise EnvironmentFailure(f"{action.name} step returned a malformed observation")
        return result

    def ask_user(self, instance: TaskInstance, question: str) -> str:
        simulator_spec = instance.task.user_simulator
        if simulator_spec is None:
            return NO_USER_REPLY
        if self.user_simulator is None:
            raise EnvironmentFailure("the task has a user simulator but none is configured")
        hidden_intent = simulator_spec.hidden_intent
        if hidden_intent == HIDDEN_INTENT_FROM_GENERATOR:
            hidden_intent = instance.hidden_intent
        try:
            return self.user_simulator.reply(simulator_spec.persona, hidden_intent, instance.instruction, question)
        except GatewayError as error:
            raise EnvironmentFailure(f"user simulator failed: {describe(error)}") from error

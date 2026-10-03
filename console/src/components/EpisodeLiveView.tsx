// One episode as it plays: the latest frame at full resolution, every step so far and, once finished, the reward
// breakdown with the checks and provenance behind it (DESIGN.md 6.14, dry run and live wall).
import { Component, type ReactElement } from "react";

import type { ConsoleApi } from "../api/client";
import type { CheckResult, EpisodeRecord, Provenance, RewardRecord, StateChange, StepRecord } from "../api/types";
import { failureProblems, ProblemList } from "./common";
import type { EpisodeControls } from "./TakeoverView";

interface EpisodeLiveViewProps {
  api: ConsoleApi;
  runId: string;
  episodeId: string;
  pollMilliseconds?: number;
  controls?: EpisodeControls;
}

interface EpisodeLiveViewState {
  episode: EpisodeRecord | null;
  problems: string[];
}

export function actionSummary(action: Record<string, unknown>): string {
  const parts: string[] = [];
  for (const [key, value] of Object.entries(action)) {
    if (key !== "name") {
      parts.push(`${key}=${typeof value === "string" ? value : JSON.stringify(value)}`);
    }
  }
  return parts.length > 0 ? `${String(action.name)}(${parts.join(", ")})` : String(action.name);
}

function stepRow(step: StepRecord): ReactElement {
  const actions: string[] = [];
  for (const action of step.actions) {
    actions.push(actionSummary(action));
  }
  return (
    <tr key={step.step_index}>
      <td>{step.step_index}</td>
      <td>{actions.join("; ")}</td>
      <td className="model-output">{step.model_turn.output_text}</td>
      <td>{step.parse_error ?? step.vm_events.join(", ")}</td>
      <td>{step.timings.model_seconds.toFixed(1)} s / {step.timings.env_seconds.toFixed(1)} s</td>
    </tr>
  );
}

function checkRow(check: CheckResult): ReactElement {
  return (
    <tr key={`${check.tier}:${check.name}`} className={check.passed ? "check-passed" : "check-failed"}>
      <td>{check.tier}</td>
      <td>{check.name}</td>
      <td>{check.passed ? "passed" : "failed"}</td>
      <td>{check.detail}</td>
    </tr>
  );
}

function violationItem(change: StateChange): ReactElement {
  return <li key={change.path}>{change.kind} {change.path}</li>;
}

export function judgeText(provenance: Provenance): string {
  if (provenance.judge_model === null) {
    return provenance.judge_skipped ? "skipped, the outcome was already decided" : "none";
  }
  const cached = provenance.judge_cached ? ", cached" : "";
  return `${provenance.judge_model}, rubric ${provenance.rubric_version ?? "unversioned"}${cached}`;
}

export function RewardBreakdown(props: { reward: RewardRecord }): ReactElement {
  const { reward } = props;
  const components: string[] = [];
  for (const [name, value] of Object.entries(reward.components)) {
    components.push(`${name} ${value.toFixed(2)}`);
  }
  const checks: ReactElement[] = [];
  for (const check of reward.checks) {
    checks.push(checkRow(check));
  }
  const violations: ReactElement[] = [];
  for (const change of reward.side_effect_violations) {
    violations.push(violationItem(change));
  }
  const verifiers: string[] = [];
  for (const [name, version] of Object.entries(reward.provenance.verifier_versions)) {
    verifiers.push(`${name}@${version}`);
  }
  return (
    <section className="reward" aria-label="reward breakdown">
      <h3>Reward {reward.total.toFixed(2)}: {reward.termination}{reward.counts_for_training ? "" : ", excluded"}</h3>
      <p>{components.join(", ")}</p>
      {reward.error_detail ? <p className="error-detail">{reward.error_detail}</p> : null}
      {checks.length > 0
        ? <table aria-label="checks"><tbody>{checks}</tbody></table>
        : null}
      {violations.length > 0 ? <ul aria-label="side effect violations">{violations}</ul> : null}
      <dl className="provenance">
        <dt>Verifiers</dt><dd>{verifiers.join(", ") || "none"}</dd>
        <dt>Judge</dt><dd>{judgeText(reward.provenance)}</dd>
        <dt>State hash</dt><dd><code>{reward.provenance.state_hash}</code></dd>
      </dl>
    </section>
  );
}

export class EpisodeLiveView extends Component<EpisodeLiveViewProps, EpisodeLiveViewState> {
  private timer: ReturnType<typeof setInterval> | null = null;
  private polling = false;
  private steps: StepRecord[] = [];

  constructor(props: EpisodeLiveViewProps) {
    super(props);
    this.state = { episode: null, problems: [] };
    this.poll = this.poll.bind(this);
  }

  componentDidMount(): void {
    this.timer = setInterval(this.poll, this.props.pollMilliseconds ?? 500);
    void this.poll();
  }

  componentWillUnmount(): void {
    this.stopPolling();
  }

  stopPolling(): void {
    if (this.timer !== null) {
      clearInterval(this.timer);
      this.timer = null;
    }
  }

  // Asks only for steps not seen yet; stops once the episode is finished and scored.
  async poll(): Promise<void> {
    if (this.polling || this.timer === null) {
      return;
    }
    this.polling = true;
    try {
      const update = await this.props.api.getEpisode(this.props.runId, this.props.episodeId, this.steps.length);
      this.steps = [...this.steps, ...update.steps];
      this.setState({ episode: { ...update, steps: this.steps }, problems: [] });
      if (update.finished_at !== null) {
        this.stopPolling();
      }
    } catch (error) {
      this.setState({ problems: this.state.episode === null ? [] : failureProblems(error) });
    } finally {
      this.polling = false;
    }
  }

  render(): ReactElement {
    const episode = this.state.episode;
    if (episode === null) {
      return <p className="empty">Waiting for episode {this.props.episodeId} to start.</p>;
    }
    const steps: ReactElement[] = [];
    for (const step of episode.steps) {
      steps.push(stepRow(step));
    }
    const latest = episode.steps[episode.steps.length - 1];
    const screenshot = latest?.observation.screenshot ?? null;
    return (
      <section className="episode-live" aria-label={`episode ${episode.episode_id}`}>
        <h2>{episode.task_id} <small>{episode.task_template}, sample {episode.sample_index}</small></h2>
        <p>{episode.instruction}</p>
        <p role="status">{episode.finished_at === null ? `playing, step ${steps.length}` : "finished"}</p>
        <ProblemList title="Episode" problems={this.state.problems} />
        {this.props.controls?.render(episode) ?? null}
        {screenshot !== null
          ? <img className="frame" alt="latest frame" src={this.props.api.frameUrl(screenshot.sha256, "full")}
                 width={screenshot.width} height={screenshot.height} />
          : null}
        {episode.reward !== null ? <RewardBreakdown reward={episode.reward} /> : null}
        <table aria-label="steps">
          <thead><tr><th>Step</th><th>Actions</th><th>Model output</th><th>Notes</th><th>Model / env</th></tr></thead>
          <tbody>{steps}</tbody>
        </table>
      </section>
    );
  }
}

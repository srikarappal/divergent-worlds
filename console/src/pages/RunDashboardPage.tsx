// The run dashboard (DESIGN.md 6.14, Watch live): metrics over server sent events, cost burn against budget, alerts
// and the live controls. Controls touch only operational knobs; changing tasks, endpoints or rewards is a new run.
import { Component, type ChangeEvent, type MouseEvent, type ReactElement } from "react";
import { Link } from "react-router-dom";

import type { ConsoleApi, RunEventListener } from "../api/client";
import type { Alert, ExperimentRecord, RunEvent, RunRecord } from "../api/types";
import { AlertList } from "../components/AlertList";
import { failureProblems, formatPercent, ParityPanel, ProblemList } from "../components/common";
import { formatUsd } from "./LaunchPage";

const RUN_CHANGING_EVENTS = new Set(["control", "completed", "failed", "launched", "budget_exhausted"]);
const PRIORITIES = ["interactive", "eval", "training"];
const KEPT_EVENTS = 200;
const SPARKLINE_WIDTH = 160;
const SPARKLINE_HEIGHT = 36;

interface RunDashboardProps {
  api: ConsoleApi;
  runId: string;
}

interface RunDashboardState {
  run: RunRecord | null;
  experiment: ExperimentRecord | null;
  spentUsd: number | null;
  events: RunEvent[];
  metrics: RunEvent[];
  alerts: Alert[];
  concurrencyText: string;
  priority: string;
  budgetExtensionText: string;
  problems: string[];
}

export function metricValue(event: RunEvent | undefined, key: string): number | null {
  const value = event?.detail[key];
  return typeof value === "number" ? value : null;
}

export function episodesPerSecond(metrics: readonly RunEvent[]): number | null {
  if (metrics.length < 2) {
    return null;
  }
  const previous = metrics[metrics.length - 2];
  const latest = metrics[metrics.length - 1];
  const episodes = (metricValue(latest, "episodes") ?? 0) - (metricValue(previous, "episodes") ?? 0);
  const seconds = latest.at - previous.at;
  return seconds > 0 ? episodes / seconds : null;
}

export function sparklinePoints(values: readonly number[], width: number, height: number): string {
  if (values.length === 0) {
    return "";
  }
  const highest = Math.max(...values);
  const lowest = Math.min(...values);
  const span = highest - lowest || 1;
  const points: string[] = [];
  for (const [index, value] of values.entries()) {
    const x = values.length === 1 ? width : (index / (values.length - 1)) * width;
    const y = height - ((value - lowest) / span) * height;
    points.push(`${x.toFixed(1)},${y.toFixed(1)}`);
  }
  return points.join(" ");
}

function series(metrics: readonly RunEvent[], key: string): number[] {
  const values: number[] = [];
  for (const event of metrics) {
    const value = metricValue(event, key);
    if (value !== null) {
      values.push(value);
    }
  }
  return values;
}

function Sparkline(props: { label: string; values: readonly number[] }): ReactElement {
  return (
    <figure className="sparkline">
      <svg width={SPARKLINE_WIDTH} height={SPARKLINE_HEIGHT} role="img" aria-label={`${props.label} over time`}>
        <polyline fill="none" stroke="#2457c5" strokeWidth="1.5"
                  points={sparklinePoints(props.values, SPARKLINE_WIDTH, SPARKLINE_HEIGHT)} />
      </svg>
      <figcaption>{props.label}</figcaption>
    </figure>
  );
}

function seconds(value: number | null): string {
  return value === null ? "" : `${value.toFixed(2)} s`;
}

function eventRow(event: RunEvent): ReactElement {
  return (
    <tr key={event.sequence}>
      <td>{event.sequence}</td>
      <td>{new Date(event.at * 1000).toLocaleTimeString()}</td>
      <td>{event.kind}</td>
      <td><code>{JSON.stringify(event.detail)}</code></td>
    </tr>
  );
}

export class RunDashboardPage extends Component<RunDashboardProps, RunDashboardState> implements RunEventListener {
  private abortController: AbortController | null = null;
  // Several events can arrive in one tick, before React applies state, so they accumulate here first.
  private receivedEvents: RunEvent[] = [];
  private receivedMetrics: RunEvent[] = [];
  private spentUsd: number | null = null;

  constructor(props: RunDashboardProps) {
    super(props);
    this.state = {
      run: null, experiment: null, spentUsd: null, events: [], metrics: [], alerts: [], concurrencyText: "",
      priority: "", budgetExtensionText: "", problems: [],
    };
    this.handleAcknowledge = this.handleAcknowledge.bind(this);
    this.handleCommand = this.handleCommand.bind(this);
    this.handleConcurrencyText = this.handleConcurrencyText.bind(this);
    this.handleApplyConcurrency = this.handleApplyConcurrency.bind(this);
    this.handlePriority = this.handlePriority.bind(this);
    this.handleBudgetText = this.handleBudgetText.bind(this);
    this.handleExtendBudget = this.handleExtendBudget.bind(this);
  }

  async componentDidMount(): Promise<void> {
    await this.refresh();
    await this.loadAlerts();
    await this.follow();
  }

  componentWillUnmount(): void {
    this.abortController?.abort();
  }

  async refresh(): Promise<void> {
    try {
      const run = await this.props.api.getRun(this.props.runId);
      const experiment = await this.props.api.getExperiment(run.experiment_id);
      // Spend only grows; a refresh that started before a spend event must not roll it back.
      this.spentUsd = Math.max(this.spentUsd ?? 0, experiment.spent_usd);
      this.setState({ run, experiment, spentUsd: this.spentUsd, concurrencyText: String(run.desired_concurrency),
                      priority: run.priority });
    } catch (error) {
      this.setState({ problems: failureProblems(error) });
    }
  }

  async loadAlerts(): Promise<void> {
    try {
      this.setState({ alerts: await this.props.api.listAlerts({ run_id: this.props.runId }) });
    } catch (error) {
      this.setState({ problems: failureProblems(error) });
    }
  }

  async handleAcknowledge(alertId: string): Promise<void> {
    try {
      await this.props.api.acknowledgeAlert(alertId);
      await this.loadAlerts();
    } catch (error) {
      this.setState({ problems: failureProblems(error) });
    }
  }

  async follow(): Promise<void> {
    this.abortController = new AbortController();
    try {
      await this.props.api.runEvents(this.props.runId, this, true, 0, this.abortController.signal);
    } catch (error) {
      if (!this.abortController.signal.aborted) {
        this.setState({ problems: failureProblems(error) });
      }
    }
  }

  onRunEvent(event: RunEvent): void {
    this.receivedEvents = [...this.receivedEvents, event].slice(-KEPT_EVENTS);
    if (event.kind === "metrics") {
      this.receivedMetrics = [...this.receivedMetrics, event];
    }
    const spent = event.kind === "spend" ? event.detail.experiment_spent_usd : undefined;
    if (typeof spent === "number") {
      this.spentUsd = spent;
    }
    this.setState({ events: this.receivedEvents, metrics: this.receivedMetrics, spentUsd: this.spentUsd });
    if (RUN_CHANGING_EVENTS.has(event.kind)) {
      void this.refresh();
    }
    if (event.kind === "alert") {
      void this.loadAlerts();
    }
  }

  async control(command: string, value?: unknown): Promise<void> {
    try {
      const run = await this.props.api.controlRun(this.props.runId, command, value);
      this.setState({ run, problems: [] });
      if (command === "extend_budget") {
        await this.refresh();
      }
    } catch (error) {
      this.setState({ problems: failureProblems(error) });
    }
  }

  async handleCommand(event: MouseEvent<HTMLButtonElement>): Promise<void> {
    await this.control(event.currentTarget.dataset.command ?? "");
  }

  handleConcurrencyText(event: ChangeEvent<HTMLInputElement>): void {
    this.setState({ concurrencyText: event.target.value });
  }

  async handleApplyConcurrency(): Promise<void> {
    await this.control("set_concurrency", Number(this.state.concurrencyText));
  }

  async handlePriority(event: ChangeEvent<HTMLSelectElement>): Promise<void> {
    this.setState({ priority: event.target.value });
    await this.control("set_priority", event.target.value);
  }

  handleBudgetText(event: ChangeEvent<HTMLInputElement>): void {
    this.setState({ budgetExtensionText: event.target.value });
  }

  async handleExtendBudget(): Promise<void> {
    await this.control("extend_budget", Number(this.state.budgetExtensionText));
    this.setState({ budgetExtensionText: "" });
  }

  renderControls(run: RunRecord): ReactElement {
    const priorities: ReactElement[] = [];
    for (const priority of PRIORITIES) {
      priorities.push(<option key={priority} value={priority}>{priority}</option>);
    }
    const ended = run.state === "stopped" || run.state === "completed" || run.state === "failed";
    return (
      <fieldset className="controls" disabled={ended}>
        <legend>Live controls</legend>
        <button type="button" data-command="pause" disabled={run.state !== "running"} onClick={this.handleCommand}>
          Pause</button>
        <button type="button" data-command="resume" disabled={run.state !== "paused"} onClick={this.handleCommand}>
          Resume</button>
        <button type="button" data-command="stop" onClick={this.handleCommand}>Stop</button>
        <label>Concurrency <input type="number" value={this.state.concurrencyText}
                                  onChange={this.handleConcurrencyText} /></label>
        <button type="button" onClick={this.handleApplyConcurrency}>Set concurrency</button>
        <label>Priority <select value={this.state.priority} onChange={this.handlePriority}>{priorities}</select></label>
        <label>Extend budget (USD) <input type="number" value={this.state.budgetExtensionText}
                                          onChange={this.handleBudgetText} /></label>
        <button type="button" onClick={this.handleExtendBudget}>Extend budget</button>
      </fieldset>
    );
  }

  renderMetrics(): ReactElement {
    const metrics = this.state.metrics;
    const latest = metrics[metrics.length - 1];
    const rate = episodesPerSecond(metrics);
    const trackerUrl = latest?.detail.tracker_url;
    const gpuUtilization = metricValue(latest, "trainer_gpu_utilization");
    return (
      <section className="metrics" aria-label="run metrics">
        <dl>
          <dt>Episodes</dt><dd>{metricValue(latest, "episodes") ?? 0}</dd>
          <dt>Episodes per second</dt><dd>{rate === null ? "" : rate.toFixed(2)}</dd>
          <dt>Success rate</dt><dd>{formatPercent(metricValue(latest, "success_rate"))}</dd>
          <dt>Mean reward</dt><dd>{metricValue(latest, "mean_reward")?.toFixed(3) ?? ""}</dd>
          <dt>Env error rate</dt><dd>{formatPercent(metricValue(latest, "env_error_rate"))}</dd>
          <dt>Model per step</dt><dd>{seconds(metricValue(latest, "mean_model_seconds"))}</dd>
          <dt>Env per step</dt><dd>{seconds(metricValue(latest, "mean_env_seconds"))}</dd>
          <dt>Settle per step</dt><dd>{seconds(metricValue(latest, "mean_settle_seconds"))}</dd>
          <dt>Rate limit waits</dt><dd>{seconds(metricValue(latest, "rate_limit_wait_seconds"))}</dd>
          {gpuUtilization !== null ? <><dt>Trainer GPUs busy</dt><dd>{formatPercent(gpuUtilization)}</dd></> : null}
        </dl>
        <div className="sparklines">
          <Sparkline label="Success rate" values={series(metrics, "success_rate")} />
          <Sparkline label="Mean reward" values={series(metrics, "mean_reward")} />
          <Sparkline label="Env error rate" values={series(metrics, "env_error_rate")} />
        </div>
        {typeof trackerUrl === "string" ? <a href={trackerUrl}>Training curves in the experiment tracker</a> : null}
      </section>
    );
  }

  render(): ReactElement {
    const { run, experiment, spentUsd } = this.state;
    const events: ReactElement[] = [];
    for (const event of [...this.state.events].reverse()) {
      events.push(eventRow(event));
    }
    const burn = experiment !== null && spentUsd !== null && experiment.budget_usd > 0
      ? spentUsd / experiment.budget_usd : null;
    return (
      <div className="run-dashboard">
        <h1>{run?.run_name ?? this.props.runId} <small>{run?.run_id}</small></h1>
        <ProblemList title="Run" problems={this.state.problems} />
        {run !== null
          ? (
            <p role="status">
              {run.state}, {run.run_type}, priority {run.priority}, concurrency {run.desired_concurrency}.{" "}
              <Link to={`/runs/${run.run_id}/live`}>Live wall</Link>{" "}
              <Link to={`/runs/${run.run_id}/review`}>Review</Link>
            </p>
          )
          : null}
        {experiment !== null
          ? (
            <section className="budget" aria-label="budget">
              Spent {formatUsd(spentUsd)} of {formatUsd(experiment.budget_usd)}
              {burn !== null ? <meter min={0} max={1} value={Math.min(burn, 1)}>{formatPercent(burn)}</meter> : null}
            </section>
          )
          : null}
        {run !== null ? this.renderControls(run) : null}
        <section aria-label="run alerts">
          <h2>Alerts</h2>
          <AlertList alerts={this.state.alerts} showRun={false} onAcknowledge={this.handleAcknowledge} />
        </section>
        {this.renderMetrics()}
        <table aria-label="events">
          <thead><tr><th>#</th><th>Time</th><th>Event</th><th>Detail</th></tr></thead>
          <tbody>{events}</tbody>
        </table>
        <ParityPanel items={[
          { name: "run_events", values: { run_id: this.props.runId } },
          { name: "control_run", values: { run_id: this.props.runId, command: "pause" } },
          { name: "list_alerts", values: { run_id: this.props.runId } },
        ]} />
      </div>
    );
  }
}

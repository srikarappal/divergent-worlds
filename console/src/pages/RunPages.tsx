// Runs and experiments: every run, one experiment's sweep points with its approval plus the live wall
// (DESIGN.md 6.14, Launch and control, Watch live).
import { Component, type ReactElement } from "react";
import { Link } from "react-router-dom";

import type { ConsoleApi } from "../api/client";
import type { ExperimentRecord, LiveTile, RunRecord, SavedRun } from "../api/types";
import { failureProblems, ParityPanel, ProblemList } from "../components/common";
import { formatUsd } from "./LaunchPage";

function assignmentsText(assignments: Record<string, unknown>): string {
  const parts: string[] = [];
  for (const [path, value] of Object.entries(assignments)) {
    parts.push(`${path}=${JSON.stringify(value)}`);
  }
  return parts.join(", ");
}

function runRow(run: RunRecord): ReactElement {
  return (
    <tr key={run.run_id}>
      <td><Link to={`/runs/${run.run_id}`}>{run.run_id}</Link></td>
      <td>{run.run_type}</td>
      <td>{run.state}</td>
      <td>{run.desired_concurrency}</td>
      <td>{run.priority}</td>
      <td>{assignmentsText(run.assignments)}</td>
      <td><Link to={`/experiments/${run.experiment_id}`}>{run.experiment_id}</Link></td>
    </tr>
  );
}

function runsTable(runs: readonly RunRecord[]): ReactElement {
  const rows: ReactElement[] = [];
  for (const run of runs) {
    rows.push(runRow(run));
  }
  return (
    <table aria-label="runs">
      <thead>
        <tr><th>Run</th><th>Type</th><th>State</th><th>Concurrency</th><th>Priority</th><th>Sweep point</th>
          <th>Experiment</th></tr>
      </thead>
      <tbody>{rows}</tbody>
    </table>
  );
}

function savedRunRow(savedRun: SavedRun): ReactElement {
  const episodes: ReactElement[] = [];
  for (const episodeId of savedRun.episode_ids) {
    const episodePath = `/runs/${savedRun.run_id}/episodes/${episodeId}`;
    episodes.push(
      <li key={episodeId}>
        <Link to={`${episodePath}/review`}>{episodeId}</Link> (<Link to={episodePath}>live</Link>)
      </li>,
    );
  }
  return (
    <tr key={savedRun.run_id}>
      <td>{savedRun.run_id}</td>
      <td><ul className="saved-episodes">{episodes}</ul></td>
    </tr>
  );
}

// Dry runs and runs from before an experimentd restart have trajectories but no run record.
function savedRunsTable(savedRuns: readonly SavedRun[]): ReactElement | null {
  if (savedRuns.length === 0) {
    return null;
  }
  const rows: ReactElement[] = [];
  for (const savedRun of savedRuns) {
    rows.push(savedRunRow(savedRun));
  }
  return (
    <>
      <h2>Saved runs</h2>
      <table aria-label="saved runs">
        <thead><tr><th>Run</th><th>Episodes</th></tr></thead>
        <tbody>{rows}</tbody>
      </table>
    </>
  );
}

interface RunsPageState {
  runs: RunRecord[];
  savedRuns: SavedRun[];
  problems: string[];
}

export class RunsPage extends Component<{ api: ConsoleApi }, RunsPageState> {
  constructor(props: { api: ConsoleApi }) {
    super(props);
    this.state = { runs: [], savedRuns: [], problems: [] };
  }

  async componentDidMount(): Promise<void> {
    try {
      const listing = await this.props.api.listRuns();
      this.setState({ runs: listing.runs, savedRuns: listing.saved_runs });
    } catch (error) {
      this.setState({ problems: failureProblems(error) });
    }
  }

  render(): ReactElement {
    return (
      <div className="runs">
        <h1>Runs</h1>
        <ProblemList title="Runs" problems={this.state.problems} />
        {runsTable(this.state.runs)}
        {savedRunsTable(this.state.savedRuns)}
        <ParityPanel items={[{ name: "list_runs" }]} />
      </div>
    );
  }
}

interface ExperimentPageProps {
  api: ConsoleApi;
  experimentId: string;
}

interface ExperimentPageState {
  experiment: ExperimentRecord | null;
  runs: RunRecord[];
  problems: string[];
}

export class ExperimentPage extends Component<ExperimentPageProps, ExperimentPageState> {
  constructor(props: ExperimentPageProps) {
    super(props);
    this.state = { experiment: null, runs: [], problems: [] };
    this.handleApprove = this.handleApprove.bind(this);
  }

  async componentDidMount(): Promise<void> {
    await this.refresh();
  }

  async refresh(): Promise<void> {
    try {
      this.setState({
        experiment: await this.props.api.getExperiment(this.props.experimentId),
        runs: (await this.props.api.listRuns(this.props.experimentId)).runs,
      });
    } catch (error) {
      this.setState({ problems: failureProblems(error) });
    }
  }

  async handleApprove(): Promise<void> {
    try {
      await this.props.api.approveExperiment(this.props.experimentId);
      await this.refresh();
    } catch (error) {
      this.setState({ problems: failureProblems(error) });
    }
  }

  render(): ReactElement {
    const experiment = this.state.experiment;
    return (
      <div className="experiment">
        <h1>{experiment?.name ?? this.props.experimentId}</h1>
        <ProblemList title="Experiment" problems={this.state.problems} />
        {experiment !== null
          ? (
            <p role="status">
              Spent {formatUsd(experiment.spent_usd)} of {formatUsd(experiment.budget_usd)}, estimated{" "}
              {formatUsd(experiment.estimated_cost_usd)}.{" "}
              {experiment.approved ? "Approved." : "Waiting for approval."}
            </p>
          )
          : null}
        {experiment !== null && !experiment.approved
          ? <button type="button" className="primary" onClick={this.handleApprove}>Approve launch</button>
          : null}
        {runsTable(this.state.runs)}
        <ParityPanel items={[
          { name: "get_experiment", values: { experiment_id: this.props.experimentId } },
          { name: "approve_experiment", values: { experiment_id: this.props.experimentId } },
          { name: "list_runs", values: { experiment_id: this.props.experimentId } },
        ]} />
      </div>
    );
  }
}

interface LiveWallProps {
  api: ConsoleApi;
  runId: string;
  limit?: number;
  pollMilliseconds?: number;
}

interface LiveWallState {
  tiles: LiveTile[];
  problems: string[];
}

// Sampled thumbnails from running envs, about one frame per second; a tile opens the episode at full rate.
export class LiveWallPage extends Component<LiveWallProps, LiveWallState> {
  private timer: ReturnType<typeof setInterval> | null = null;

  constructor(props: LiveWallProps) {
    super(props);
    this.state = { tiles: [], problems: [] };
    this.refresh = this.refresh.bind(this);
  }

  componentDidMount(): void {
    this.timer = setInterval(this.refresh, this.props.pollMilliseconds ?? 1000);
    void this.refresh();
  }

  componentWillUnmount(): void {
    if (this.timer !== null) {
      clearInterval(this.timer);
    }
  }

  async refresh(): Promise<void> {
    try {
      this.setState({ tiles: await this.props.api.liveWall(this.props.runId, this.props.limit ?? 24), problems: [] });
    } catch (error) {
      this.setState({ problems: failureProblems(error) });
    }
  }

  renderTile(tile: LiveTile): ReactElement {
    return (
      <Link key={tile.episode_id} className="tile" to={`/runs/${this.props.runId}/episodes/${tile.episode_id}`}>
        {tile.frame !== null
          ? <img alt={`${tile.episode_id} frame`} src={this.props.api.frameUrl(tile.frame, "thumbnail")} />
          : <span className="tile-starting">starting</span>}
        <span className="tile-caption">{tile.task_id}, sample {tile.sample_index}, step {tile.step_index ?? 0}</span>
      </Link>
    );
  }

  render(): ReactElement {
    const tiles: ReactElement[] = [];
    for (const tile of this.state.tiles) {
      tiles.push(this.renderTile(tile));
    }
    return (
      <div className="live-wall">
        <h1>Live wall <code>{this.props.runId}</code></h1>
        <ProblemList title="Live wall" problems={this.state.problems} />
        {tiles.length === 0 ? <p className="empty">No episode is running.</p> : null}
        <div className="tiles">{tiles}</div>
        <ParityPanel items={[
          { name: "live_wall", values: { run_id: this.props.runId, limit: this.props.limit ?? 24 } },
        ]} />
      </div>
    );
  }
}

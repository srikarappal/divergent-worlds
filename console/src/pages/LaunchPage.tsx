// Preflight, dry run and launch for one saved spec (DESIGN.md 6.14): validation, endpoint checks, the Little's law
// estimate and trap warnings come first, one episode can be watched end to end and only then is budget committed.
import { Component, type ChangeEvent, type ReactElement } from "react";

import type { ConsoleApi } from "../api/client";
import type { DryRun, ExperimentPreflight, PreflightIssue, PreflightReport } from "../api/types";
import { failureProblems, ParityPanel, ProblemList } from "../components/common";
import { EpisodeLiveView } from "../components/EpisodeLiveView";

export interface Navigate {
  (to: string): void;
}

interface LaunchPageProps {
  api: ConsoleApi;
  digest: string;
  navigate: Navigate;
  pollMilliseconds?: number;
}

interface LaunchPageState {
  report: ExperimentPreflight | null;
  budgetText: string;
  dryRun: DryRun | null;
  launching: boolean;
  problems: string[];
}

export function formatUsd(value: number | null | undefined): string {
  return value === null || value === undefined ? "unknown" : `${value.toLocaleString("en-US", {
    maximumFractionDigits: 2 })} USD`;
}

function issueItem(issue: PreflightIssue): ReactElement {
  return (
    <li key={`${issue.severity}:${issue.code}:${issue.message}`} className={`issue-${issue.severity}`}>
      <strong>{issue.severity}</strong> <code>{issue.code}</code> {issue.message}
    </li>
  );
}

function hostsText(hostsByPool: Record<string, number>): string {
  const parts: string[] = [];
  for (const [pool, hosts] of Object.entries(hostsByPool)) {
    parts.push(`${hosts} ${pool}`);
  }
  return parts.join(", ") || "none";
}

function pointSection(pointName: string, report: PreflightReport): ReactElement {
  const issues: ReactElement[] = [];
  for (const issue of report.issues) {
    issues.push(issueItem(issue));
  }
  return (
    <section key={pointName} className="preflight-point" aria-label={`preflight ${pointName}`}>
      <h3>{pointName} {report.is_valid ? "passes" : "is blocked"}</h3>
      <dl className="estimate">
        <dt>Concurrent envs</dt><dd>{report.estimated_concurrent_envs}</dd>
        <dt>Hosts</dt><dd>{hostsText(report.estimated_hosts_by_pool)}</dd>
        <dt>Policy GPUs</dt><dd>{report.estimated_policy_gpus}</dd>
        <dt>Judge tokens</dt><dd>{report.estimated_judge_tokens.toLocaleString("en-US")}</dd>
        <dt>Cost</dt><dd>{formatUsd(report.estimated_cost_usd)}</dd>
        <dt>Wall clock</dt><dd>{report.estimated_wall_clock_hours.toFixed(1)} h</dd>
      </dl>
      {issues.length > 0 ? <ul className="issues">{issues}</ul> : null}
    </section>
  );
}

export class LaunchPage extends Component<LaunchPageProps, LaunchPageState> {
  constructor(props: LaunchPageProps) {
    super(props);
    this.state = { report: null, budgetText: "", dryRun: null, launching: false, problems: [] };
    this.handleBudget = this.handleBudget.bind(this);
    this.handleDryRun = this.handleDryRun.bind(this);
    this.handleLaunch = this.handleLaunch.bind(this);
  }

  async componentDidMount(): Promise<void> {
    try {
      const report = await this.props.api.preflight(this.props.digest);
      this.setState({ report, budgetText: String(report.budget_usd) });
    } catch (error) {
      this.setState({ problems: failureProblems(error) });
    }
  }

  handleBudget(event: ChangeEvent<HTMLInputElement>): void {
    this.setState({ budgetText: event.target.value });
  }

  async handleDryRun(): Promise<void> {
    try {
      this.setState({ dryRun: await this.props.api.dryRun(this.props.digest), problems: [] });
    } catch (error) {
      this.setState({ problems: failureProblems(error) });
    }
  }

  async handleLaunch(): Promise<void> {
    this.setState({ launching: true, problems: [] });
    try {
      const experiment = await this.props.api.launch(this.props.digest, Number(this.state.budgetText));
      this.props.navigate(`/experiments/${experiment.experiment_id}`);
    } catch (error) {
      this.setState({ launching: false, problems: failureProblems(error) });
    }
  }

  render(): ReactElement {
    const report = this.state.report;
    const points: ReactElement[] = [];
    for (const [pointName, pointReport] of Object.entries(report?.points ?? {})) {
      points.push(pointSection(pointName, pointReport));
    }
    const budget = Number(this.state.budgetText);
    const canLaunch = report !== null && report.is_valid && budget > 0 && !this.state.launching;
    return (
      <div className="launch">
        <h1>Preflight <code>{this.props.digest}</code></h1>
        <ProblemList title="Launch" problems={this.state.problems} />
        {report === null
          ? <p className="empty">Running preflight.</p>
          : (
            <section aria-label="preflight summary">
              <p role="status">{report.is_valid ? "Preflight passed" : "Preflight blocked the launch"}:{" "}
                estimated {formatUsd(report.estimated_cost_usd)} against a budget of {formatUsd(report.budget_usd)}
              </p>
              <ProblemList title="Preflight" problems={report.errors} />
              {points}
            </section>
          )}
        <div className="toolbar">
          <button type="button" onClick={this.handleDryRun}>Dry run one episode</button>
          <label>Budget (USD) <input type="number" value={this.state.budgetText} onChange={this.handleBudget} /></label>
          <button type="button" className="primary" disabled={!canLaunch} onClick={this.handleLaunch}>Launch</button>
        </div>
        {this.state.dryRun !== null
          ? <EpisodeLiveView api={this.props.api} runId={this.state.dryRun.run_id}
                             episodeId={this.state.dryRun.episode_id} pollMilliseconds={this.props.pollMilliseconds} />
          : null}
        <ParityPanel items={[
          { name: "preflight", values: { digest: this.props.digest } },
          { name: "dry_run", values: { digest: this.props.digest } },
          { name: "launch", values: { digest: this.props.digest, budget_usd: this.state.budgetText || undefined } },
        ]} />
      </div>
    );
  }
}

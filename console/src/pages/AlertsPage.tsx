// Alerts across runs (DESIGN.md 6.14, Watch live): reward spikes, judge and verifier disagreement, env error rate
// jumps, budget thresholds and run completions, the same alerts Slack and email receive.
import { Component, type ChangeEvent, type ReactElement } from "react";

import type { ConsoleApi } from "../api/client";
import type { Alert } from "../api/types";
import { AlertList } from "../components/AlertList";
import { failureProblems, ParityPanel, ProblemList } from "../components/common";

const STATUSES = ["open", "acknowledged"];

interface AlertsState {
  status: string;
  alerts: Alert[];
  problems: string[];
}

export class AlertsPage extends Component<{ api: ConsoleApi }, AlertsState> {
  constructor(props: { api: ConsoleApi }) {
    super(props);
    this.state = { status: "open", alerts: [], problems: [] };
    this.handleStatus = this.handleStatus.bind(this);
    this.handleAcknowledge = this.handleAcknowledge.bind(this);
  }

  async componentDidMount(): Promise<void> {
    await this.load(this.state.status);
  }

  async load(status: string): Promise<void> {
    this.setState({ status, problems: [] });
    try {
      const alerts = await this.props.api.listAlerts({ status });
      alerts.sort(byNewest);
      this.setState({ alerts });
    } catch (error) {
      this.setState({ alerts: [], problems: failureProblems(error) });
    }
  }

  async handleStatus(event: ChangeEvent<HTMLSelectElement>): Promise<void> {
    await this.load(event.target.value);
  }

  async handleAcknowledge(alertId: string): Promise<void> {
    try {
      await this.props.api.acknowledgeAlert(alertId);
      await this.load(this.state.status);
    } catch (error) {
      this.setState({ problems: failureProblems(error) });
    }
  }

  render(): ReactElement {
    const statuses: ReactElement[] = [];
    for (const status of STATUSES) {
      statuses.push(<option key={status} value={status}>{status}</option>);
    }
    return (
      <div className="alerts-page">
        <h1>Alerts</h1>
        <label>Status <select value={this.state.status} onChange={this.handleStatus}>{statuses}</select></label>
        <ProblemList title="Alerts" problems={this.state.problems} />
        <AlertList alerts={this.state.alerts} showRun onAcknowledge={this.handleAcknowledge} />
        <ParityPanel items={[
          { name: "list_alerts", values: { run_id: "<run_id>" } },
          { name: "acknowledge_alert", values: { alert_id: "<alert_id>" } },
        ]} />
      </div>
    );
  }
}

function byNewest(first: Alert, second: Alert): number {
  return second.raised_at - first.raised_at;
}

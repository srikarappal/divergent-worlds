// Alerts on runs (DESIGN.md 6.9 and 6.14): reward spikes, judge and verifier disagreement, env error rate jumps,
// budget thresholds and run completions, open until someone acknowledges them.
import { Component, type MouseEvent, type ReactElement } from "react";
import { Link } from "react-router-dom";

import type { Alert } from "../api/types";

interface AlertListProps {
  alerts: readonly Alert[];
  showRun: boolean;
  onAcknowledge(alertId: string): void;
}

export function alertKindLabel(alert: Alert): string {
  return alert.kind.split("_").join(" ");
}

export class AlertList extends Component<AlertListProps> {
  constructor(props: AlertListProps) {
    super(props);
    this.handleAcknowledge = this.handleAcknowledge.bind(this);
  }

  handleAcknowledge(event: MouseEvent<HTMLButtonElement>): void {
    this.props.onAcknowledge(event.currentTarget.dataset.alert ?? "");
  }

  renderRow(alert: Alert): ReactElement {
    const scope = alert.run_id
      ? <Link to={`/runs/${alert.run_id}`}>{alert.run_id}</Link>
      : <Link to={`/experiments/${alert.experiment_id}`}>{alert.experiment_id}</Link>;
    return (
      <tr key={alert.alert_id} className={`alert alert-${alert.severity}`}>
        <td>{alert.severity}</td>
        <td>{alertKindLabel(alert)}</td>
        <td>{alert.title}</td>
        {this.props.showRun ? <td>{scope}</td> : null}
        <td>{alert.review_item_id !== null
          ? <Link to="/queues">{alert.episode_ids.length} episodes in the review queue</Link>
          : null}</td>
        <td>{alert.status === "open"
          ? <button type="button" data-alert={alert.alert_id} onClick={this.handleAcknowledge}>Acknowledge</button>
          : `acknowledged by ${alert.acknowledged_by}`}</td>
      </tr>
    );
  }

  render(): ReactElement {
    if (this.props.alerts.length === 0) {
      return <p>No alerts.</p>;
    }
    const rows: ReactElement[] = [];
    for (const alert of this.props.alerts) {
      rows.push(this.renderRow(alert));
    }
    return (
      <table aria-label="alerts">
        <thead>
          <tr><th>Severity</th><th>Kind</th><th>Alert</th>{this.props.showRun ? <th>Scope</th> : null}
            <th>Review</th><th>Status</th></tr>
        </thead>
        <tbody>{rows}</tbody>
      </table>
    );
  }
}

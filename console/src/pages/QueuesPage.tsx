// Human queues (DESIGN.md 6.14): labeling for verifier and judge calibration sets, review of gate failures, flagged
// episodes and audit samples plus the bug queues failure clustering feeds.
import { Component, type ChangeEvent, type MouseEvent, type ReactElement } from "react";
import { Link } from "react-router-dom";

import type { ConsoleApi } from "../api/client";
import type { CalibrationReport, ReviewItem } from "../api/types";
import { failureProblems, formatPercent, ParityPanel, ProblemList } from "../components/common";

export const QUEUES: ReadonlyArray<{ queue: string; label: string }> = [
  { queue: "labeling", label: "Labeling" },
  { queue: "review", label: "Review" },
  { queue: "audit", label: "Audit samples" },
  { queue: "verifier", label: "Verifier bugs" },
  { queue: "adapter", label: "Adapter bugs" },
  { queue: "env", label: "Env bugs" },
  { queue: "task", label: "Task bugs" },
  { queue: "factory", label: "Task drafts" },
];
const STATUSES = ["open", "resolved", "dismissed"];

interface LabelDraft {
  verdict: string;
  items: Record<string, boolean>;
}

interface QueuesState {
  queue: string;
  status: string;
  items: ReviewItem[];
  notes: Record<string, string>;
  labels: Record<string, LabelDraft>;
  rubricItems: Record<string, string[]>;
  calibration: CalibrationReport | null;
  problems: string[];
}

function itemId(event: { currentTarget: { dataset: DOMStringMap } }): string {
  return event.currentTarget.dataset.item ?? "";
}

function CalibrationSummary(props: { report: CalibrationReport }): ReactElement {
  const rows: ReactElement[] = [];
  for (const [rubricItem, stats] of Object.entries(props.report.judge)) {
    rows.push(<tr key={rubricItem}><td>{rubricItem}</td><td>{stats.label_count}</td><td>{stats.kappa.toFixed(2)}</td>
      <td>{stats.false_passes}</td></tr>);
  }
  const verifier = props.report.verifier;
  return (
    <section aria-label="calibration">
      <h2>Calibration</h2>
      <p>Verifiers against {verifier.labels} human verdicts: precision {formatPercent(verifier.precision)}, recall{" "}
        {formatPercent(verifier.recall)}</p>
      {rows.length > 0
        ? <table><thead><tr><th>Rubric item</th><th>Labels</th><th>Kappa</th><th>False passes</th></tr></thead>
          <tbody>{rows}</tbody></table>
        : null}
      <ProblemList title="Judge calibration" problems={props.report.judge_problems} />
    </section>
  );
}

export class QueuesPage extends Component<{ api: ConsoleApi }, QueuesState> {
  constructor(props: { api: ConsoleApi }) {
    super(props);
    this.state = { queue: "labeling", status: "open", items: [], notes: {}, labels: {}, rubricItems: {},
                   calibration: null, problems: [] };
    this.handleQueue = this.handleQueue.bind(this);
    this.handleStatus = this.handleStatus.bind(this);
    this.handleNote = this.handleNote.bind(this);
    this.handleResolve = this.handleResolve.bind(this);
    this.handleDismiss = this.handleDismiss.bind(this);
    this.handleLoadRubric = this.handleLoadRubric.bind(this);
    this.handleVerdict = this.handleVerdict.bind(this);
    this.handleRubricItem = this.handleRubricItem.bind(this);
    this.handleSaveLabel = this.handleSaveLabel.bind(this);
  }

  async componentDidMount(): Promise<void> {
    await this.load(this.state.queue, this.state.status);
  }

  async load(queue: string, status: string): Promise<void> {
    this.setState({ queue, status, problems: [] });
    try {
      const items = await this.props.api.listReviewItems({ queue, status });
      const calibration = queue === "labeling" ? await this.props.api.calibration() : null;
      this.setState({ items, calibration });
    } catch (error) {
      this.setState({ items: [], problems: failureProblems(error) });
    }
  }

  async handleQueue(event: MouseEvent<HTMLButtonElement>): Promise<void> {
    await this.load(event.currentTarget.dataset.queue ?? "review", this.state.status);
  }

  async handleStatus(event: ChangeEvent<HTMLSelectElement>): Promise<void> {
    await this.load(this.state.queue, event.target.value);
  }

  handleNote(event: ChangeEvent<HTMLInputElement>): void {
    this.setState({ notes: { ...this.state.notes, [itemId(event)]: event.target.value } });
  }

  async settle(id: string, status: "resolved" | "dismissed", label?: Record<string, unknown>): Promise<void> {
    try {
      await this.props.api.updateReviewItem(id, { status, note: this.state.notes[id] || undefined, label });
      await this.load(this.state.queue, this.state.status);
    } catch (error) {
      this.setState({ problems: failureProblems(error) });
    }
  }

  async handleResolve(event: MouseEvent<HTMLButtonElement>): Promise<void> {
    await this.settle(itemId(event), "resolved");
  }

  async handleDismiss(event: MouseEvent<HTMLButtonElement>): Promise<void> {
    await this.settle(itemId(event), "dismissed");
  }

  // The rubric items a labeler judges are the judge checks the episode was scored with.
  async handleLoadRubric(event: MouseEvent<HTMLButtonElement>): Promise<void> {
    const id = itemId(event);
    const item = this.state.items.find(hasItemId, id);
    if (item === undefined || item.episode_ids.length === 0) {
      return;
    }
    try {
      const provenance = await this.props.api.getProvenance(item.run_id, item.episode_ids[0]);
      const names: string[] = [];
      for (const check of provenance.reward?.checks ?? []) {
        if (check.tier === "judge") {
          names.push(check.name);
        }
      }
      this.setState({ rubricItems: { ...this.state.rubricItems, [id]: names } });
    } catch (error) {
      this.setState({ problems: failureProblems(error) });
    }
  }

  draft(id: string): LabelDraft {
    return this.state.labels[id] ?? { verdict: "", items: {} };
  }

  handleVerdict(event: ChangeEvent<HTMLSelectElement>): void {
    const id = itemId(event);
    this.setState({ labels: { ...this.state.labels, [id]: { ...this.draft(id), verdict: event.target.value } } });
  }

  handleRubricItem(event: ChangeEvent<HTMLInputElement>): void {
    const id = itemId(event);
    const rubricItem = event.currentTarget.dataset.rubricItem ?? "";
    const draft = this.draft(id);
    this.setState({ labels: { ...this.state.labels,
                              [id]: { ...draft, items: { ...draft.items, [rubricItem]: event.target.checked } } } });
  }

  async handleSaveLabel(event: MouseEvent<HTMLButtonElement>): Promise<void> {
    const id = itemId(event);
    const draft = this.draft(id);
    const label: Record<string, unknown> = { items: draft.items };
    if (draft.verdict) {
      label.verdict = draft.verdict;
    }
    await this.settle(id, "resolved", label);
  }

  renderLabeling(item: ReviewItem): ReactElement {
    const draft = this.draft(item.item_id);
    const rubricItems = this.state.rubricItems[item.item_id];
    const boxes: ReactElement[] = [];
    for (const rubricItem of rubricItems ?? []) {
      boxes.push(<label key={rubricItem}><input type="checkbox" data-item={item.item_id} data-rubric-item={rubricItem}
                                                checked={draft.items[rubricItem] === true}
                                                onChange={this.handleRubricItem} /> {rubricItem} passes</label>);
    }
    return (
      <div className="label-editor">
        <label>Verdict
          <select data-item={item.item_id} value={draft.verdict} onChange={this.handleVerdict}>
            <option value="">(none)</option><option value="success">success</option>
            <option value="failure">failure</option>
          </select>
        </label>
        {rubricItems === undefined
          ? <button type="button" data-item={item.item_id} onClick={this.handleLoadRubric}>Judge rubric items</button>
          : boxes}
        <button type="button" className="primary" data-item={item.item_id} onClick={this.handleSaveLabel}>
          Save label</button>
      </div>
    );
  }

  renderItem(item: ReviewItem): ReactElement {
    const episodes: ReactElement[] = [];
    for (const episodeId of item.episode_ids) {
      episodes.push(<Link key={episodeId} to={`/runs/${item.run_id}/episodes/${episodeId}/review`}>{episodeId}</Link>);
    }
    const open = item.status === "open";
    return (
      <tr key={item.item_id}>
        <td>{item.title}<div className="item-meta">{item.source}, by {item.created_by || "unknown"}</div></td>
        <td>{episodes}</td>
        <td>{item.note}</td>
        <td>
          {open && this.state.queue === "labeling" ? this.renderLabeling(item) : null}
          {open
            ? (
              <div className="toolbar">
                <input aria-label={`${item.item_id} note`} data-item={item.item_id}
                       value={this.state.notes[item.item_id] ?? ""} onChange={this.handleNote} />
                <button type="button" data-item={item.item_id} onClick={this.handleResolve}>Resolve</button>
                <button type="button" data-item={item.item_id} onClick={this.handleDismiss}>Dismiss</button>
              </div>
            )
            : `${item.status} by ${item.resolved_by}`}
        </td>
      </tr>
    );
  }

  render(): ReactElement {
    const tabs: ReactElement[] = [];
    for (const entry of QUEUES) {
      tabs.push(<button key={entry.queue} type="button" data-queue={entry.queue}
                        aria-pressed={entry.queue === this.state.queue} onClick={this.handleQueue}>
        {entry.label}</button>);
    }
    const statuses: ReactElement[] = [];
    for (const status of STATUSES) {
      statuses.push(<option key={status} value={status}>{status}</option>);
    }
    const rows: ReactElement[] = [];
    for (const item of this.state.items) {
      rows.push(this.renderItem(item));
    }
    return (
      <div className="queues">
        <nav className="tabs" aria-label="queues">{tabs}
          <label>Status <select value={this.state.status} onChange={this.handleStatus}>{statuses}</select></label>
        </nav>
        <ProblemList title="Queue" problems={this.state.problems} />
        {this.state.calibration !== null ? <CalibrationSummary report={this.state.calibration} /> : null}
        <table aria-label={`${this.state.queue} queue`}>
          <thead><tr><th>Item</th><th>Episodes</th><th>Note</th><th>Action</th></tr></thead>
          <tbody>{rows}</tbody>
        </table>
        {rows.length === 0 ? <p className="empty">Nothing {this.state.status} in this queue.</p> : null}
        <ParityPanel items={[
          { name: "list_review_items", values: { queue: this.state.queue, status: this.state.status } },
          { name: "update_review_item", values: { item_status: "resolved" } },
        ]} />
      </div>
    );
  }
}

function hasItemId(this: string, item: ReviewItem): boolean {
  return item.item_id === this;
}

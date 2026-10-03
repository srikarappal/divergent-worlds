// Review (DESIGN.md 6.11 and 6.14): the trajectory scrubber with reward provenance and one click actions, the
// cohort view, a run's report with failure clusters and policy versions plus run against run comparisons.
import { Component, type ChangeEvent, type MouseEvent, type ReactElement } from "react";
import { Link } from "react-router-dom";

import type { ConsoleApi } from "../api/client";
import type {
  CohortEpisode,
  Comparison,
  EpisodeFilter,
  EpisodeProvenance,
  EpisodeRecord,
  EvalBreakdown,
  EvalReport,
  FailureAnalysis,
  FailureCluster,
  ReviewAction,
  ReviewItem,
  StateChange,
  StepRecord,
} from "../api/types";
import { failureProblems, formatPercent, ParityPanel, ProblemList } from "../components/common";
import { actionSummary, RewardBreakdown } from "../components/EpisodeLiveView";

export const FAILURE_CATEGORIES = ["grounding", "planning", "wrong_state", "gave_up", "env_issue", "adapter_parse",
                                   "verifier_false_negative", "task_issue", "other"];
export const TERMINATIONS = ["success", "failure", "max_steps", "agent_error", "env_error"];
const REVIEW_ACTIONS: ReadonlyArray<{ action: ReviewAction; label: string }> = [
  { action: "flag", label: "Flag" },
  { action: "label", label: "Save label" },
  { action: "verifier_bug", label: "File verifier bug" },
  { action: "task_draft", label: "Turn into task draft" },
  { action: "audit_sample", label: "Add to audit sample" },
];
export const COHORT_FIELDS = ["termination", "category", "task_id", "template", "difficulty_key", "policy_version",
                              "limit"];

export interface NavigateTo {
  (to: string, options?: { replace?: boolean }): void;
}

function optionElements(values: readonly string[], emptyLabel: string): ReactElement[] {
  const options = [<option key="" value="">{emptyLabel}</option>];
  for (const value of values) {
    options.push(<option key={value} value={value}>{value}</option>);
  }
  return options;
}

export function breakdownText(breakdown: EvalBreakdown): string {
  return `${formatPercent(breakdown.success_rate)} [${formatPercent(breakdown.interval_low)}, ${
    formatPercent(breakdown.interval_high)}] of ${breakdown.episodes}`;
}

function json(value: unknown): string {
  return JSON.stringify(value, null, 2);
}

interface Marker {
  key: string;
  left: string;
  top: string;
  label: string;
}

// Where each action of a step landed on its frame, as percentages so the overlay scales with the image.
export function actionMarkers(step: StepRecord): Marker[] {
  const screenshot = step.observation.screenshot;
  const markers: Marker[] = [];
  if (screenshot === null) {
    return markers;
  }
  for (const [index, action] of step.actions.entries()) {
    const points: Array<{ x: unknown; y: unknown }> = Array.isArray(action.path)
      ? action.path as Array<{ x: unknown; y: unknown }> : [{ x: action.x, y: action.y }];
    for (const [pointIndex, point] of points.entries()) {
      if (typeof point.x === "number" && typeof point.y === "number") {
        markers.push({
          key: `${index}_${pointIndex}`,
          left: `${(point.x / screenshot.width) * 100}%`,
          top: `${(point.y / screenshot.height) * 100}%`,
          label: String(action.name),
        });
      }
    }
  }
  return markers;
}

function changeRow(change: StateChange): ReactElement {
  return (
    <tr key={change.path}>
      <td><code>{change.path}</code></td>
      <td>{change.kind}</td>
      <td><code>{change.before === undefined ? "" : JSON.stringify(change.before)}</code></td>
      <td><code>{change.after === undefined ? "" : JSON.stringify(change.after)}</code></td>
    </tr>
  );
}

function changesTable(label: string, changes: readonly StateChange[]): ReactElement | null {
  if (changes.length === 0) {
    return null;
  }
  const rows: ReactElement[] = [];
  for (const change of changes) {
    rows.push(changeRow(change));
  }
  return (
    <table aria-label={label}>
      <thead><tr><th>Path</th><th>Change</th><th>Before</th><th>After</th></tr></thead>
      <tbody>{rows}</tbody>
    </table>
  );
}

function itemRow(item: ReviewItem): ReactElement {
  return <li key={item.item_id}>{item.queue}: {item.title}{item.note ? `, ${item.note}` : ""} ({item.status})</li>;
}

interface EpisodeReviewProps {
  api: ConsoleApi;
  runId: string;
  episodeId: string;
  initialStep: number;
  navigate: NavigateTo;
}

interface EpisodeReviewState {
  episode: EpisodeRecord | null;
  provenance: EpisodeProvenance | null;
  step: number;
  prompts: Record<number, Record<string, unknown>[]>;
  showPrompt: boolean;
  items: ReviewItem[];
  note: string;
  verdict: string;
  category: string;
  problems: string[];
  message: string | null;
}

// The trajectory scrubber: frames with actions overlaid, model input and output, state changes, reward
// provenance and one click actions, deep linked by step.
export class EpisodeReviewPage extends Component<EpisodeReviewProps, EpisodeReviewState> {
  constructor(props: EpisodeReviewProps) {
    super(props);
    this.state = { episode: null, provenance: null, step: Math.max(0, props.initialStep), prompts: {},
                   showPrompt: false, items: [], note: "", verdict: "", category: "", problems: [], message: null };
    this.handleSlider = this.handleSlider.bind(this);
    this.handlePrevious = this.handlePrevious.bind(this);
    this.handleNext = this.handleNext.bind(this);
    this.handleShowPrompt = this.handleShowPrompt.bind(this);
    this.handleNote = this.handleNote.bind(this);
    this.handleVerdict = this.handleVerdict.bind(this);
    this.handleCategory = this.handleCategory.bind(this);
    this.handleAction = this.handleAction.bind(this);
    this.handleRerun = this.handleRerun.bind(this);
    this.handleClone = this.handleClone.bind(this);
  }

  async componentDidMount(): Promise<void> {
    const { api, runId, episodeId } = this.props;
    try {
      const episode = await api.getEpisode(runId, episodeId, 0);
      this.setState({
        episode,
        step: Math.min(this.state.step, Math.max(0, episode.steps.length - 1)),
        provenance: await api.getProvenance(runId, episodeId),
        items: await api.listReviewItems({ episode_id: episodeId }),
      });
    } catch (error) {
      this.setState({ problems: failureProblems(error) });
    }
  }

  setStep(step: number): void {
    const last = Math.max(0, (this.state.episode?.steps.length ?? 1) - 1);
    const clamped = Math.min(Math.max(0, step), last);
    this.setState({ step: clamped, showPrompt: false });
    this.props.navigate(`?step=${clamped}`, { replace: true });
  }

  handleSlider(event: ChangeEvent<HTMLInputElement>): void {
    this.setStep(Number(event.target.value));
  }

  handlePrevious(): void {
    this.setStep(this.state.step - 1);
  }

  handleNext(): void {
    this.setStep(this.state.step + 1);
  }

  async handleShowPrompt(): Promise<void> {
    const step = this.state.step;
    if (this.state.prompts[step] === undefined) {
      try {
        const messages = await this.props.api.getStepPrompt(this.props.runId, this.props.episodeId, step);
        this.setState({ prompts: { ...this.state.prompts, [step]: messages } });
      } catch (error) {
        this.setState({ problems: failureProblems(error) });
        return;
      }
    }
    this.setState({ showPrompt: !this.state.showPrompt });
  }

  handleNote(event: ChangeEvent<HTMLTextAreaElement>): void {
    this.setState({ note: event.target.value });
  }

  handleVerdict(event: ChangeEvent<HTMLSelectElement>): void {
    this.setState({ verdict: event.target.value });
  }

  handleCategory(event: ChangeEvent<HTMLSelectElement>): void {
    this.setState({ category: event.target.value });
  }

  labelPayload(): Record<string, unknown> | undefined {
    const label: Record<string, unknown> = {};
    if (this.state.verdict) {
      label.verdict = this.state.verdict;
    }
    if (this.state.category) {
      label.category = this.state.category;
    }
    return Object.keys(label).length > 0 ? label : undefined;
  }

  async handleAction(event: MouseEvent<HTMLButtonElement>): Promise<void> {
    const action = event.currentTarget.dataset.action as ReviewAction;
    try {
      const item = await this.props.api.reviewEpisode(this.props.runId, this.props.episodeId, action, this.state.note,
                                                      action === "label" ? this.labelPayload() : undefined);
      this.setState({ items: [...this.state.items, item], note: "", problems: [],
                      message: `filed in the ${item.queue} queue` });
    } catch (error) {
      this.setState({ problems: failureProblems(error) });
    }
  }

  async handleRerun(): Promise<void> {
    try {
      const digest = await this.props.api.saveSpec(await this.props.api.getRunSpec(this.props.runId));
      this.props.navigate(`/launch/${digest}`);
    } catch (error) {
      this.setState({ problems: failureProblems(error) });
    }
  }

  handleClone(): void {
    this.props.navigate(`/configure?clone=${this.props.runId}`);
  }

  renderStep(step: StepRecord): ReactElement {
    const screenshot = step.observation.screenshot;
    const markers: ReactElement[] = [];
    for (const marker of actionMarkers(step)) {
      markers.push(<span key={marker.key} className="action-marker" style={{ left: marker.left, top: marker.top }}
                         title={marker.label}>{marker.label}</span>);
    }
    const actions: string[] = [];
    for (const action of step.actions) {
      actions.push(actionSummary(action));
    }
    const prompt = this.state.prompts[step.step_index];
    return (
      <section className="scrubbed-step" aria-label={`step ${step.step_index}`}>
        {screenshot !== null
          ? (
            <div className="frame-stage">
              <img alt={`step ${step.step_index} frame`} src={this.props.api.frameUrl(screenshot.sha256, "full")} />
              {markers}
            </div>
          )
          : null}
        <dl className="step-details">
          <dt>Actions</dt><dd>{actions.join("; ") || "none"}</dd>
          <dt>Model output</dt><dd className="model-output">{step.model_turn.output_text}</dd>
          {step.parse_error ? <><dt>Parse error</dt><dd>{step.parse_error}</dd></> : null}
          {step.observation.user_reply !== null ? <><dt>User reply</dt><dd>{step.observation.user_reply}</dd></> : null}
          {step.observation.terminal !== null
            ? <><dt>Terminal</dt><dd><pre>{step.observation.terminal.stdout}</pre></dd></> : null}
          {step.vm_events.length > 0 ? <><dt>VM events</dt><dd>{step.vm_events.join(", ")}</dd></> : null}
          <dt>Timings</dt>
          <dd>model {step.timings.model_seconds.toFixed(2)} s, env {step.timings.env_seconds.toFixed(2)} s, settle{" "}
            {step.timings.settle_seconds.toFixed(2)} s</dd>
        </dl>
        {changesTable("step state changes", step.state_changes)}
        <button type="button" onClick={this.handleShowPrompt}>
          {this.state.showPrompt ? "Hide model input" : "Show model input"}</button>
        {this.state.showPrompt && prompt !== undefined
          ? <pre className="prompt" aria-label="model input">{json(prompt)}</pre> : null}
      </section>
    );
  }

  renderActions(): ReactElement {
    const buttons: ReactElement[] = [];
    for (const entry of REVIEW_ACTIONS) {
      buttons.push(<button key={entry.action} type="button" data-action={entry.action}
                           onClick={this.handleAction}>{entry.label}</button>);
    }
    const items: ReactElement[] = [];
    for (const item of this.state.items) {
      items.push(itemRow(item));
    }
    return (
      <section className="review-actions" aria-label="review actions">
        <label>Note <textarea value={this.state.note} onChange={this.handleNote} rows={2} /></label>
        <label>Verdict
          <select value={this.state.verdict} onChange={this.handleVerdict}>
            {optionElements(["success", "failure"], "(none)")}</select>
        </label>
        <label>Failure category
          <select value={this.state.category} onChange={this.handleCategory}>
            {optionElements(FAILURE_CATEGORIES, "(none)")}</select>
        </label>
        <div className="toolbar">{buttons}</div>
        {this.state.message ? <p role="status">{this.state.message}</p> : null}
        <ul aria-label="review items">{items}</ul>
      </section>
    );
  }

  render(): ReactElement {
    const { episode, provenance, step } = this.state;
    if (episode === null) {
      return <div><ProblemList title="Episode" problems={this.state.problems} />
        <p className="empty">Loading {this.props.episodeId}.</p></div>;
    }
    const current = episode.steps[step];
    return (
      <div className="episode-review">
        <h1>{episode.episode_id} <small>{episode.task_id}, {episode.task_template}</small></h1>
        <p>{episode.instruction}</p>
        <p role="status">{episode.reward?.termination ?? "unscored"}
          {episode.human_intervened ? ", human intervened" : ""}, policy version {episode.key.policy_version}</p>
        <ProblemList title="Episode" problems={this.state.problems} />
        <div className="scrubber">
          <button type="button" onClick={this.handlePrevious} disabled={step === 0}>Previous</button>
          <input type="range" aria-label="step" min={0} max={Math.max(0, episode.steps.length - 1)} value={step}
                 onChange={this.handleSlider} />
          <button type="button" onClick={this.handleNext} disabled={step >= episode.steps.length - 1}>Next</button>
          <span>Step {step} of {episode.steps.length}</span>
        </div>
        {current !== undefined ? this.renderStep(current) : <p className="empty">No steps were recorded.</p>}
        {provenance?.reward ? <RewardBreakdown reward={provenance.reward} /> : null}
        {provenance !== null
          ? (
            <section className="provenance-drill" aria-label="reward provenance">
              {changesTable("world diff", provenance.world_diff)}
              <details><summary>Extracted final state</summary><pre>{json(provenance.final_state)}</pre></details>
              <details><summary>Initial state</summary><pre>{json(provenance.initial_state)}</pre></details>
            </section>
          )
          : null}
        {this.renderActions()}
        <div className="toolbar">
          <button type="button" onClick={this.handleRerun}>Rerun the same spec</button>
          <button type="button" onClick={this.handleClone}>Clone and edit</button>
        </div>
        <ParityPanel items={[
          { name: "get_episode",
            values: { run_id: this.props.runId, episode_id: this.props.episodeId, first_step: 0 } },
          { name: "get_provenance", values: { run_id: this.props.runId, episode_id: this.props.episodeId } },
          { name: "review_episode", values: { run_id: this.props.runId, episode_id: this.props.episodeId,
                                              action: "flag", note: "" } },
        ]} />
      </div>
    );
  }
}

interface CohortProps {
  api: ConsoleApi;
  runId: string;
  initialFilter: Record<string, string>;
}

interface CohortState {
  filter: Record<string, string>;
  members: CohortEpisode[];
  step: number;
  problems: string[];
  loaded: boolean;
}

export function episodeFilter(fields: Record<string, string>): EpisodeFilter {
  const filter: EpisodeFilter = {};
  for (const [name, value] of Object.entries(fields)) {
    if (value === "") {
      continue;
    }
    if (name === "policy_version" || name === "limit") {
      filter[name] = Number(value);
    } else if (name === "termination" || name === "category" || name === "task_id" || name === "template"
               || name === "difficulty_key") {
      filter[name] = value;
    }
  }
  return filter;
}

// Up to 50 finished episodes side by side, all scrubbed to the same step.
export class CohortPage extends Component<CohortProps, CohortState> {
  constructor(props: CohortProps) {
    super(props);
    this.state = { filter: { ...props.initialFilter }, members: [], step: 0, problems: [], loaded: false };
    this.handleFilter = this.handleFilter.bind(this);
    this.handleApply = this.handleApply.bind(this);
    this.handleStep = this.handleStep.bind(this);
  }

  async componentDidMount(): Promise<void> {
    await this.load();
  }

  async load(): Promise<void> {
    try {
      const members = await this.props.api.cohort(this.props.runId, episodeFilter(this.state.filter));
      this.setState({ members, step: 0, problems: [], loaded: true });
    } catch (error) {
      this.setState({ problems: failureProblems(error), loaded: true });
    }
  }

  handleFilter(event: ChangeEvent<HTMLInputElement | HTMLSelectElement>): void {
    this.setState({ filter: { ...this.state.filter, [event.target.name]: event.target.value } });
  }

  async handleApply(): Promise<void> {
    await this.load();
  }

  handleStep(event: ChangeEvent<HTMLInputElement>): void {
    this.setState({ step: Number(event.target.value) });
  }

  renderMember(member: CohortEpisode): ReactElement {
    const frameIndex = Math.min(this.state.step, member.frames.length - 1);
    const frame = frameIndex >= 0 ? member.frames[frameIndex] : null;
    const episode = member.episode;
    return (
      <Link key={episode.episode_id} className="tile"
            to={`/runs/${this.props.runId}/episodes/${episode.episode_id}/review?step=${Math.max(0, frameIndex)}`}>
        {frame ? <img alt={`${episode.episode_id} at step ${frameIndex}`}
                      src={this.props.api.frameUrl(frame, "thumbnail")} />
          : <span className="tile-starting">no frame</span>}
        <span className="tile-caption">{episode.episode_id}, {episode.task_id}, {episode.reward?.termination}
          {member.category ? `, ${member.category}` : ""}</span>
      </Link>
    );
  }

  render(): ReactElement {
    const filter = this.state.filter;
    let longest = 0;
    const tiles: ReactElement[] = [];
    for (const member of this.state.members) {
      longest = Math.max(longest, member.frames.length);
      tiles.push(this.renderMember(member));
    }
    return (
      <div className="cohort">
        <h1>Cohort <code>{this.props.runId}</code></h1>
        <form className="toolbar" aria-label="cohort filters">
          <label>Termination <select name="termination" value={filter.termination ?? ""} onChange={this.handleFilter}>
            {optionElements(TERMINATIONS, "any")}</select></label>
          <label>Failure category <select name="category" value={filter.category ?? ""} onChange={this.handleFilter}>
            {optionElements(FAILURE_CATEGORIES, "any")}</select></label>
          <label>Task <input name="task_id" value={filter.task_id ?? ""} onChange={this.handleFilter} /></label>
          <label>Difficulty <input name="difficulty_key" value={filter.difficulty_key ?? ""}
                                   onChange={this.handleFilter} /></label>
          <label>Policy version <input name="policy_version" type="number" value={filter.policy_version ?? ""}
                                       onChange={this.handleFilter} /></label>
          <button type="button" className="primary" onClick={this.handleApply}>Apply</button>
        </form>
        <ProblemList title="Cohort" problems={this.state.problems} />
        <div className="scrubber">
          <input type="range" aria-label="cohort step" min={0} max={Math.max(0, longest - 1)} value={this.state.step}
                 onChange={this.handleStep} />
          <span>Step {this.state.step}, {tiles.length} episodes</span>
        </div>
        {this.state.loaded && tiles.length === 0 ? <p className="empty">No episode matches.</p> : null}
        <div className="tiles">{tiles}</div>
        <ParityPanel items={[
          { name: "cohort", values: { run_id: this.props.runId, termination: filter.termination } },
        ]} />
      </div>
    );
  }
}

function breakdownRows(label: string, breakdowns: Record<string, EvalBreakdown>): ReactElement[] {
  const rows: ReactElement[] = [];
  for (const [name, breakdown] of Object.entries(breakdowns)) {
    rows.push(<tr key={`${label}:${name}`}><td>{label}</td><td>{name}</td><td>{breakdownText(breakdown)}</td>
      <td>{breakdown.mean_total.toFixed(2)}</td></tr>);
  }
  return rows;
}

export function ReportTable(props: { report: EvalReport }): ReactElement {
  const { report } = props;
  const passAtK: string[] = [];
  for (const [k, value] of Object.entries(report.pass_at_k)) {
    passAtK.push(`pass@${k} ${formatPercent(value)}`);
  }
  return (
    <section aria-label="eval report">
      <p>Overall {breakdownText(report.overall)}; {passAtK.join(", ")}; excluded {report.excluded_env_errors} env
        errors and {report.excluded_human_intervened} human intervened episodes</p>
      <table>
        <thead><tr><th>By</th><th>Name</th><th>Success [95% interval] of episodes</th><th>Mean reward</th></tr></thead>
        <tbody>
          {breakdownRows("template", report.by_template)}
          {breakdownRows("difficulty", report.by_difficulty)}
        </tbody>
      </table>
    </section>
  );
}

export function SharedTasksTable(props: { comparison: Comparison }): ReactElement {
  const groups = Object.keys(props.comparison.reports);
  const header: ReactElement[] = [<th key="task">Task</th>, <th key="template">Template</th>];
  for (const group of groups) {
    header.push(<th key={group}>{group}</th>);
  }
  const rows: ReactElement[] = [];
  for (const row of props.comparison.shared_tasks) {
    const cells: ReactElement[] = [<td key="task">{row.task_id}</td>, <td key="template">{row.template}</td>];
    for (const group of groups) {
      cells.push(<td key={group}>{formatPercent(row.success_rates[group])} of {row.episodes[group]}</td>);
    }
    rows.push(<tr key={row.task_id}>{cells}</tr>);
  }
  return (
    <table aria-label="shared tasks">
      <thead><tr>{header}</tr></thead>
      <tbody>{rows}</tbody>
    </table>
  );
}

interface RunReviewProps {
  api: ConsoleApi;
  runId: string;
}

interface RunReviewState {
  report: EvalReport | null;
  analysis: FailureAnalysis | null;
  versions: Comparison | null;
  otherRunId: string;
  analyzing: boolean;
  problems: string[];
}

// A run's eval report, failure clusters per template and policy version plus its policy versions side by side.
export class RunReviewPage extends Component<RunReviewProps, RunReviewState> {
  constructor(props: RunReviewProps) {
    super(props);
    this.state = { report: null, analysis: null, versions: null, otherRunId: "", analyzing: false, problems: [] };
    this.handleAnalyze = this.handleAnalyze.bind(this);
    this.handleOtherRun = this.handleOtherRun.bind(this);
  }

  async componentDidMount(): Promise<void> {
    const { api, runId } = this.props;
    try {
      this.setState({ report: await api.runReport(runId), versions: await api.comparePolicyVersions(runId) });
    } catch (error) {
      this.setState({ problems: failureProblems(error) });
    }
    try {
      this.setState({ analysis: await api.getFailures(runId) });
    } catch {
      this.setState({ analysis: null });
    }
  }

  async handleAnalyze(): Promise<void> {
    this.setState({ analyzing: true });
    try {
      this.setState({ analysis: await this.props.api.analyzeFailures(this.props.runId), analyzing: false });
    } catch (error) {
      this.setState({ analyzing: false, problems: failureProblems(error) });
    }
  }

  handleOtherRun(event: ChangeEvent<HTMLInputElement>): void {
    this.setState({ otherRunId: event.target.value });
  }

  clusterRow(cluster: FailureCluster): ReactElement {
    const query = `category=${cluster.category}&template=${encodeURIComponent(cluster.template)}&policy_version=${
      cluster.policy_version}`;
    return (
      <tr key={`${cluster.template}:${cluster.policy_version}:${cluster.category}`}>
        <td>{cluster.template}</td>
        <td>{cluster.policy_version}</td>
        <td><Link to={`/runs/${this.props.runId}/cohort?${query}`}>{cluster.category}</Link></td>
        <td>{cluster.episode_ids.length}</td>
        <td>{formatPercent(cluster.share_of_failures)}</td>
        <td>{cluster.example_summaries.join("; ")}</td>
      </tr>
    );
  }

  render(): ReactElement {
    const { report, analysis, versions } = this.state;
    const clusters: ReactElement[] = [];
    for (const cluster of analysis?.clusters ?? []) {
      clusters.push(this.clusterRow(cluster));
    }
    const runId = this.props.runId;
    return (
      <div className="run-review">
        <h1>Review <code>{runId}</code></h1>
        <nav className="toolbar">
          <Link to={`/runs/${runId}/cohort?termination=failure`}>Failure cohort</Link>
          <Link to={`/runs/${runId}`}>Dashboard</Link>
          <Link to={`/runs/${runId}/live`}>Live wall</Link>
          <input aria-label="run to compare" placeholder="run id to compare" value={this.state.otherRunId}
                 onChange={this.handleOtherRun} />
          {this.state.otherRunId
            ? <Link to={`/compare?runs=${runId},${this.state.otherRunId}`}>Compare runs</Link> : null}
        </nav>
        <ProblemList title="Review" problems={this.state.problems} />
        {report !== null ? <ReportTable report={report} /> : null}
        <section aria-label="failure clusters">
          <h2>Failure clusters</h2>
          <button type="button" onClick={this.handleAnalyze} disabled={this.state.analyzing}>
            {analysis === null ? "Analyze failures" : "Analyze again"}</button>
          {analysis === null ? <p className="empty">Not analyzed yet.</p> : (
            <table>
              <thead><tr><th>Template</th><th>Policy version</th><th>Category</th><th>Episodes</th>
                <th>Share of failures</th><th>Examples</th></tr></thead>
              <tbody>{clusters}</tbody>
            </table>
          )}
        </section>
        {versions !== null && Object.keys(versions.reports).length > 1
          ? <section><h2>Policy versions on the same tasks</h2><SharedTasksTable comparison={versions} /></section>
          : null}
        <ParityPanel items={[
          { name: "run_report", values: { run_id: runId } },
          { name: "analyze_failures", values: { run_id: runId } },
          { name: "compare_policy_versions", values: { run_id: runId } },
        ]} />
      </div>
    );
  }
}

interface CompareProps {
  api: ConsoleApi;
  runIds: readonly string[];
}

interface CompareState {
  comparison: Comparison | null;
  problems: string[];
}

// Run against run on the tasks both attempted, with each run's report beside it.
export class ComparePage extends Component<CompareProps, CompareState> {
  constructor(props: CompareProps) {
    super(props);
    this.state = { comparison: null, problems: [] };
  }

  async componentDidMount(): Promise<void> {
    try {
      this.setState({ comparison: await this.props.api.compareRuns(this.props.runIds) });
    } catch (error) {
      this.setState({ problems: failureProblems(error) });
    }
  }

  render(): ReactElement {
    const comparison = this.state.comparison;
    const reports: ReactElement[] = [];
    for (const [runId, report] of Object.entries(comparison?.reports ?? {})) {
      reports.push(<section key={runId}><h2>{runId}</h2><ReportTable report={report} /></section>);
    }
    return (
      <div className="compare">
        <h1>Compare runs</h1>
        <ProblemList title="Compare" problems={this.state.problems} />
        {comparison !== null ? <SharedTasksTable comparison={comparison} /> : null}
        {reports}
        <ParityPanel items={[{ name: "compare_runs", values: { run_id: this.props.runIds[0],
                                                              other_run_id: this.props.runIds[1] } }]} />
      </div>
    );
  }
}

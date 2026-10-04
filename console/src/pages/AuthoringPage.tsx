// Task authoring (DESIGN.md 6.10 and 6.14, Human queues), the flow the CLI kit shares: start from a saved spec,
// record an oracle, alternative solutions and near misses by driving fresh dev envs through noVNC, run every gate in
// fresh envs, then submit the task with its gate report for review.
import {
  Component,
  createRef,
  type ChangeEvent,
  type FormEvent,
  type MouseEvent,
  type ReactElement,
  type RefObject,
} from "react";

import type { ConsoleApi } from "../api/client";
import type { AuthoringSession, GateOutcome, Recording, RecordingKind } from "../api/types";
import { failureProblems, ParityPanel, ProblemList } from "../components/common";
import { relayUrl, type ScreenAccess, type ScreenSession } from "../components/TakeoverView";
import type { NavigateTo } from "./ReviewPages";

const RECORDING_KINDS: readonly RecordingKind[] = ["oracle", "alternative", "near_miss"];
const KIND_LABELS: Record<RecordingKind, string> = {
  oracle: "oracle",
  alternative: "alternative solution",
  near_miss: "near miss",
};

function gateStatusText(session: AuthoringSession): string {
  switch (session.gate_status) {
    case "not_run":
      return "The gates have not run. They replay the oracle in fresh envs and grade alternatives and near misses.";
    case "running":
      return "The gates are running in fresh envs.";
    case "passed":
      return "Every gate passed.";
    case "failed":
      return "Some gates failed; reviewers see which and why.";
    default:
      return `The gates could not run: ${session.gate_error}`;
  }
}

function recordingNote(recording: Recording): string {
  if (recording.error) {
    return recording.error;
  }
  return recording.complete ? "" : `the recording stopped early: ${recording.lost_reason}`;
}

function gateRow(result: GateOutcome): ReactElement {
  return (
    <tr key={result.name} className={result.passed ? "gate-passed" : "gate-failed"}>
      <td>{result.name.split("_").join(" ")}</td>
      <td>{result.passed ? "passed" : "failed"}</td>
      <td>{result.detail}</td>
    </tr>
  );
}

interface AuthoringPageProps {
  api: ConsoleApi;
  navigate: NavigateTo;
  sessionId?: string;
  initialDigest?: string;
  screens?: ScreenAccess;
  pollMilliseconds?: number;
}

interface AuthoringPageState {
  digest: string;
  taskIndex: number;
  kind: RecordingKind;
  session: AuthoringSession | null;
  live: Recording | null;
  busy: boolean;
  problems: string[];
}

export class AuthoringPage extends Component<AuthoringPageProps, AuthoringPageState> {
  private readonly screenRef: RefObject<HTMLDivElement | null> = createRef();
  private screen: ScreenSession | null = null;
  // The relay URL carries the one time credential, so it waits here for the screen element and is then dropped.
  private pendingScreenUrl: string | null = null;
  private timer: ReturnType<typeof setInterval> | null = null;
  private polling = false;

  constructor(props: AuthoringPageProps) {
    super(props);
    this.state = { digest: props.initialDigest ?? "", taskIndex: 0, kind: "oracle", session: null, live: null,
                   busy: false, problems: [] };
    this.poll = this.poll.bind(this);
    this.handleDigest = this.handleDigest.bind(this);
    this.handleTaskIndex = this.handleTaskIndex.bind(this);
    this.handleKind = this.handleKind.bind(this);
    this.handleStart = this.handleStart.bind(this);
    this.handleRecord = this.handleRecord.bind(this);
    this.handleStopLive = this.handleStopLive.bind(this);
    this.handleStopRow = this.handleStopRow.bind(this);
    this.handleRunGates = this.handleRunGates.bind(this);
    this.handleSubmit = this.handleSubmit.bind(this);
  }

  async componentDidMount(): Promise<void> {
    if (this.props.sessionId === undefined) {
      return;
    }
    try {
      const session = await this.props.api.getAuthoring(this.props.sessionId);
      this.setState({ session });
      if (session.gate_status === "running") {
        this.startPolling();
      }
    } catch (error) {
      this.setState({ problems: failureProblems(error) });
    }
  }

  componentDidUpdate(): void {
    const target = this.screenRef.current;
    if (this.pendingScreenUrl !== null && this.props.screens !== undefined && target !== null) {
      this.screen = this.props.screens.connector.connect(target, this.pendingScreenUrl);
      this.pendingScreenUrl = null;
    }
  }

  componentWillUnmount(): void {
    this.stopPolling();
    this.screen?.disconnect();
  }

  startPolling(): void {
    if (this.timer === null) {
      this.timer = setInterval(this.poll, this.props.pollMilliseconds ?? 2000);
    }
  }

  stopPolling(): void {
    if (this.timer !== null) {
      clearInterval(this.timer);
      this.timer = null;
    }
  }

  // Reads the session until its gate run is done.
  async poll(): Promise<void> {
    const session = this.state.session;
    if (this.polling || session === null) {
      return;
    }
    this.polling = true;
    try {
      const update = await this.props.api.getAuthoring(session.session_id);
      this.setState({ session: update });
      if (update.gate_status !== "running") {
        this.stopPolling();
      }
    } catch (error) {
      this.setState({ problems: failureProblems(error) });
    } finally {
      this.polling = false;
    }
  }

  async refresh(sessionId: string): Promise<void> {
    this.setState({ session: await this.props.api.getAuthoring(sessionId) });
  }

  handleDigest(event: ChangeEvent<HTMLInputElement>): void {
    this.setState({ digest: event.target.value.trim() });
  }

  handleTaskIndex(event: ChangeEvent<HTMLInputElement>): void {
    this.setState({ taskIndex: Math.max(0, Math.floor(Number(event.target.value) || 0)) });
  }

  handleKind(event: ChangeEvent<HTMLSelectElement>): void {
    this.setState({ kind: event.target.value as RecordingKind });
  }

  async handleStart(event: FormEvent<HTMLFormElement>): Promise<void> {
    event.preventDefault();
    this.setState({ busy: true, problems: [] });
    try {
      const session = await this.props.api.startAuthoring(this.state.digest, this.state.taskIndex);
      this.props.navigate(`/authoring/${session.session_id}`);
    } catch (error) {
      this.setState({ busy: false, problems: failureProblems(error) });
    }
  }

  async handleRecord(): Promise<void> {
    const { session, kind } = this.state;
    const screens = this.props.screens;
    if (session === null || screens === undefined) {
      return;
    }
    this.setState({ busy: true, problems: [] });
    try {
      const started = await this.props.api.beginRecording(session.session_id, kind);
      this.pendingScreenUrl = relayUrl(screens.relayBase, started.relay_path);
      this.setState({ live: started.recording, busy: false });
      await this.refresh(session.session_id);
    } catch (error) {
      this.setState({ busy: false, problems: failureProblems(error) });
    }
  }

  async handleStopLive(): Promise<void> {
    const live = this.state.live;
    if (live === null) {
      return;
    }
    this.screen?.disconnect();
    this.screen = null;
    this.pendingScreenUrl = null;
    this.setState({ live: null });
    await this.stopRecording(live.recording_id);
  }

  // Recordings started elsewhere, such as from the CLI, can be stopped here too.
  async handleStopRow(event: MouseEvent<HTMLButtonElement>): Promise<void> {
    await this.stopRecording(event.currentTarget.dataset.recording ?? "");
  }

  async stopRecording(recordingId: string): Promise<void> {
    const session = this.state.session;
    if (session === null) {
      return;
    }
    this.setState({ busy: true, problems: [] });
    try {
      const ended = await this.props.api.endRecording(session.session_id, recordingId);
      this.setState({ busy: false, problems: ended.status === "failed" ? [ended.error] : [] });
      await this.refresh(session.session_id);
    } catch (error) {
      this.setState({ busy: false, problems: failureProblems(error) });
    }
  }

  async handleRunGates(): Promise<void> {
    const session = this.state.session;
    if (session === null) {
      return;
    }
    this.setState({ busy: true, problems: [] });
    try {
      const running = await this.props.api.runAuthoringGates(session.session_id);
      this.setState({ session: running, busy: false });
      this.startPolling();
    } catch (error) {
      this.setState({ busy: false, problems: failureProblems(error) });
    }
  }

  async handleSubmit(): Promise<void> {
    const session = this.state.session;
    if (session === null) {
      return;
    }
    this.setState({ busy: true, problems: [] });
    try {
      this.setState({ session: await this.props.api.submitAuthoring(session.session_id), busy: false });
    } catch (error) {
      this.setState({ busy: false, problems: failureProblems(error) });
    }
  }

  renderStart(): ReactElement {
    const { digest, taskIndex, busy } = this.state;
    return (
      <div className="authoring-page">
        <h1>Author a task</h1>
        <p>Start from a saved spec: its generator makes the task you solve in fresh dev envs.</p>
        <ProblemList title="Authoring" problems={this.state.problems} />
        <form aria-label="start authoring" onSubmit={this.handleStart}>
          <label>Spec digest <input value={digest} onChange={this.handleDigest} /></label>
          <label>Task <input type="number" min={0} value={taskIndex} onChange={this.handleTaskIndex} /></label>
          <button type="submit" className="primary" disabled={busy || digest === ""}>Start authoring</button>
        </form>
        <ParityPanel items={[{ name: "start_authoring",
                               values: { spec_digest: digest || undefined, task_index: taskIndex } }]} />
      </div>
    );
  }

  renderRecording(recording: Recording): ReactElement {
    const stoppable = recording.status === "recording" && this.state.live?.recording_id !== recording.recording_id;
    return (
      <tr key={recording.recording_id}>
        <td>{recording.recording_id}</td>
        <td>{KIND_LABELS[recording.kind]}</td>
        <td>{recording.status}</td>
        <td>{recording.actions.length} actions</td>
        <td>{recordingNote(recording)}</td>
        <td>{stoppable
          ? <button type="button" data-recording={recording.recording_id} onClick={this.handleStopRow}
                    disabled={this.state.busy}>Stop</button>
          : null}</td>
      </tr>
    );
  }

  renderRecordControls(submitted: boolean): ReactElement {
    const live = this.state.live;
    if (live !== null) {
      return (
        <div className="record-controls">
          <p>Recording the {KIND_LABELS[live.kind]}: you drive a fresh dev env at the task's start state.</p>
          <div className="takeover-screen" ref={this.screenRef} aria-label="author screen" />
          <button type="button" onClick={this.handleStopLive} disabled={this.state.busy}>Stop recording</button>
        </div>
      );
    }
    const kinds: ReactElement[] = [];
    for (const kind of RECORDING_KINDS) {
      kinds.push(<option key={kind} value={kind}>{KIND_LABELS[kind]}</option>);
    }
    return (
      <div className="record-controls">
        <label>Record <select value={this.state.kind} onChange={this.handleKind}>{kinds}</select></label>
        <button type="button" onClick={this.handleRecord}
                disabled={this.state.busy || submitted || this.props.screens === undefined}>
          Record in a fresh dev env
        </button>
        {this.props.screens === undefined
          ? <p className="item-meta">This console cannot reach fleetd's relay; record from the CLI.</p>
          : null}
      </div>
    );
  }

  renderSession(session: AuthoringSession): ReactElement {
    const { busy, live } = this.state;
    const submitted = session.review_item_id !== null;
    const graded = session.gate_status === "passed" || session.gate_status === "failed";
    const recordings: ReactElement[] = [];
    for (const recording of session.recordings) {
      recordings.push(this.renderRecording(recording));
    }
    const gates: ReactElement[] = [];
    for (const result of session.gate_results) {
      gates.push(gateRow(result));
    }
    const values = { session_id: session.session_id, spec_digest: session.spec_digest,
                     recording_kind: this.state.kind, recording_id: live?.recording_id };
    return (
      <div className="authoring-page">
        <h1>Author a task</h1>
        <ProblemList title="Authoring" problems={this.state.problems} />
        <section aria-label="task">
          <h2>{session.task_id} <small>{session.template}</small></h2>
          <p>{session.instruction}</p>
          {session.hidden_intent
            ? <p className="item-meta">Hidden intent, which only the user simulator knows: {session.hidden_intent}</p>
            : null}
        </section>
        <section aria-label="recordings">
          <h3>Recordings</h3>
          {recordings.length > 0
            ? <table aria-label="recordings">
              <thead>
                <tr><th>Recording</th><th>Kind</th><th>Status</th><th>Input</th><th>Note</th><th /></tr>
              </thead>
              <tbody>{recordings}</tbody>
            </table>
            : <p>No recordings yet. Without a recorded oracle the gates replay the template's scripted one.</p>}
          {this.renderRecordControls(submitted)}
        </section>
        <section aria-label="gates">
          <h3>Gates</h3>
          <p role="status">{gateStatusText(session)}</p>
          {gates.length > 0 ? <table aria-label="gate results"><tbody>{gates}</tbody></table> : null}
          <button type="button" onClick={this.handleRunGates}
                  disabled={busy || submitted || live !== null || session.gate_status === "running"}>
            Run every gate
          </button>
        </section>
        <section aria-label="review">
          <h3>Review</h3>
          {submitted
            ? <p>Submitted for review as {session.review_item_id}.</p>
            : <button type="button" className="primary" onClick={this.handleSubmit} disabled={busy || !graded}>
              Submit for review
            </button>}
        </section>
        <ParityPanel items={[
          { name: "get_authoring", values },
          { name: "begin_recording", values },
          { name: "end_recording", values },
          { name: "run_authoring_gates", values },
          { name: "submit_authoring", values },
        ]} />
      </div>
    );
  }

  render(): ReactElement {
    if (this.props.sessionId === undefined) {
      return this.renderStart();
    }
    if (this.state.session === null) {
      return (
        <div className="authoring-page">
          <ProblemList title="Authoring" problems={this.state.problems} />
          {this.state.problems.length === 0 ? <p className="empty">Loading {this.props.sessionId}.</p> : null}
        </div>
      );
    }
    return this.renderSession(this.state.session);
  }
}

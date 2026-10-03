// Takeover (DESIGN.md 6.14, Watch live): an operator drives a running episode's env through noVNC relayed by
// fleetd. The episode is marked human intervened and kept out of training and eval statistics; handing back keeps
// the operator's input as the episode's demonstration.
import { Component, createRef, type ReactElement, type RefObject } from "react";

import type { ConsoleApi } from "../api/client";
import type { Demonstration, EpisodeRecord, TakeoverSession } from "../api/types";
import { failureProblems, ParityPanel, ProblemList } from "./common";

export interface ScreenSession {
  disconnect(): void;
}

export interface ScreenConnector {
  connect(target: HTMLElement, url: string): ScreenSession;
}

// What the console needs to open an env's screen: the noVNC connector and where fleetd's relay is reachable.
export interface ScreenAccess {
  connector: ScreenConnector;
  relayBase: string;
}

// Controls an episode view renders with the episode it shows.
export interface EpisodeControls {
  render(episode: EpisodeRecord): ReactElement | null;
}

export function relayUrl(relayBase: string, relayPath: string): string {
  return `${relayBase.replace(/\/$/, "")}${relayPath}`;
}

interface TakeoverViewProps {
  api: ConsoleApi;
  runId: string;
  episodeId: string;
  episodeRunning: boolean;
  screens: ScreenAccess;
}

interface TakeoverViewState {
  session: TakeoverSession | null;
  demonstration: Demonstration | null;
  busy: boolean;
  problems: string[];
}

export class TakeoverView extends Component<TakeoverViewProps, TakeoverViewState> {
  private readonly screenRef: RefObject<HTMLDivElement | null> = createRef();
  private screen: ScreenSession | null = null;

  constructor(props: TakeoverViewProps) {
    super(props);
    this.state = { session: null, demonstration: null, busy: false, problems: [] };
    this.handleStart = this.handleStart.bind(this);
    this.handleHandBack = this.handleHandBack.bind(this);
  }

  componentDidUpdate(): void {
    const target = this.screenRef.current;
    if (this.state.session !== null && this.screen === null && target !== null) {
      this.screen = this.props.screens.connector.connect(target,
                                                         relayUrl(this.props.screens.relayBase,
                                                                  this.state.session.relay_path));
    }
  }

  componentWillUnmount(): void {
    this.screen?.disconnect();
  }

  async handleStart(): Promise<void> {
    this.setState({ busy: true, problems: [] });
    try {
      const session = await this.props.api.startTakeover(this.props.runId, this.props.episodeId);
      this.setState({ session, demonstration: null, busy: false });
    } catch (error) {
      this.setState({ busy: false, problems: failureProblems(error) });
    }
  }

  async handleHandBack(): Promise<void> {
    this.screen?.disconnect();
    this.screen = null;
    this.setState({ busy: true });
    try {
      const demonstration = await this.props.api.endTakeover(this.props.runId, this.props.episodeId);
      this.setState({ session: null, demonstration, busy: false });
    } catch (error) {
      this.setState({ busy: false, problems: failureProblems(error) });
    }
  }

  renderDemonstration(demonstration: Demonstration): ReactElement {
    return (
      <p role="status">
        Kept {demonstration.actions.length} actions as this episode's demonstration
        {demonstration.complete ? "." : `; the recording stopped early: ${demonstration.lost_reason}`}
      </p>
    );
  }

  render(): ReactElement | null {
    const { session, demonstration } = this.state;
    if (session === null && demonstration === null && !this.props.episodeRunning) {
      return null;
    }
    const values = { run_id: this.props.runId, episode_id: this.props.episodeId };
    return (
      <section className="takeover" aria-label="takeover">
        <h3>Takeover</h3>
        <ProblemList title="Takeover" problems={this.state.problems} />
        {session !== null
          ? <>
            <p>You drive this env now; the policy waits and the episode counts as human intervened.</p>
            <div className="takeover-screen" ref={this.screenRef} aria-label="operator screen" />
            <button type="button" onClick={this.handleHandBack} disabled={this.state.busy}>Hand back</button>
          </>
          : null}
        {session === null && this.props.episodeRunning
          ? <button type="button" onClick={this.handleStart} disabled={this.state.busy}>Take over</button>
          : null}
        {demonstration !== null ? this.renderDemonstration(demonstration) : null}
        <ParityPanel items={[{ name: "start_takeover", values }, { name: "end_takeover", values }]} />
      </section>
    );
  }
}

// The takeover panel for operators, shown with the episode it belongs to.
export class TakeoverControls implements EpisodeControls {
  private readonly api: ConsoleApi;
  private readonly runId: string;
  private readonly screens: ScreenAccess;

  constructor(api: ConsoleApi, runId: string, screens: ScreenAccess) {
    this.api = api;
    this.runId = runId;
    this.screens = screens;
  }

  render(episode: EpisodeRecord): ReactElement {
    return <TakeoverView api={this.api} runId={this.runId} episodeId={episode.episode_id}
                         episodeRunning={episode.finished_at === null} screens={this.screens} />;
  }
}

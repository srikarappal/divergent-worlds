// The console shell: one app for configure, launch, watch and review (DESIGN.md 6.14), behind platform sign in.
import { Component, type ReactElement } from "react";
import {
  BrowserRouter,
  Navigate,
  NavLink,
  Route,
  Routes,
  useNavigate,
  useParams,
  useSearchParams,
} from "react-router-dom";

import { CALLBACK_PATH, completeSignIn, SessionTokens, signInUrl } from "./api/auth";
import type { ConsoleApi, TokenSource } from "./api/client";
import type { AuthConfig, Principal } from "./api/types";
import { failureProblems, ProblemList } from "./components/common";
import { AdminPage } from "./pages/AdminPage";
import { QueuesPage } from "./pages/QueuesPage";
import type { NavigateTo } from "./pages/ReviewPages";
import { EpisodeLiveView } from "./components/EpisodeLiveView";
import { CatalogsPage } from "./pages/CatalogsPage";
import { LaunchPage } from "./pages/LaunchPage";
import { RunDashboardPage } from "./pages/RunDashboardPage";
import { ExperimentPage, LiveWallPage, RunsPage } from "./pages/RunPages";
import { COHORT_FIELDS, CohortPage, ComparePage, EpisodeReviewPage, RunReviewPage } from "./pages/ReviewPages";
import { SpecEditorPage } from "./pages/SpecEditorPage";

interface RouteProps {
  api: ConsoleApi;
}

const QUEUE_ROLES = ["researcher", "operator", "labeler", "admin"];
const ADMIN_ROLES = ["operator", "admin"];

function hasAnyRole(principal: Principal | null, roles: readonly string[]): boolean {
  if (principal === null) {
    return true;
  }
  for (const role of principal.roles) {
    if (roles.includes(role)) {
      return true;
    }
  }
  return false;
}

function ConfigureRoute(props: RouteProps): ReactElement {
  const cloneRunId = useSearchParams()[0].get("clone") ?? undefined;
  return <SpecEditorPage key={cloneRunId ?? ""} api={props.api} cloneRunId={cloneRunId} />;
}

function LaunchRoute(props: RouteProps): ReactElement {
  const params = useParams();
  const navigate = useNavigate();
  const digest = params.digest ?? "";
  return <LaunchPage key={digest} api={props.api} digest={digest} navigate={navigate} />;
}

function ExperimentRoute(props: RouteProps): ReactElement {
  const experimentId = useParams().experimentId ?? "";
  return <ExperimentPage key={experimentId} api={props.api} experimentId={experimentId} />;
}

function RunRoute(props: RouteProps): ReactElement {
  const runId = useParams().runId ?? "";
  return <RunDashboardPage key={runId} api={props.api} runId={runId} />;
}

function LiveWallRoute(props: RouteProps): ReactElement {
  const runId = useParams().runId ?? "";
  return <LiveWallPage key={runId} api={props.api} runId={runId} />;
}

function EpisodeRoute(props: RouteProps): ReactElement {
  const params = useParams();
  const runId = params.runId ?? "";
  const episodeId = params.episodeId ?? "";
  return <EpisodeLiveView key={`${runId}/${episodeId}`} api={props.api} runId={runId} episodeId={episodeId} />;
}

function EpisodeReviewRoute(props: RouteProps): ReactElement {
  const params = useParams();
  const navigate = useNavigate();
  const step = Number(useSearchParams()[0].get("step") ?? 0);
  const runId = params.runId ?? "";
  const episodeId = params.episodeId ?? "";
  return <EpisodeReviewPage key={`${runId}/${episodeId}`} api={props.api} runId={runId} episodeId={episodeId}
                            initialStep={Number.isFinite(step) ? step : 0} navigate={navigate} />;
}

function CohortRoute(props: RouteProps): ReactElement {
  const runId = useParams().runId ?? "";
  const searchParams = useSearchParams()[0];
  const filter: Record<string, string> = {};
  for (const field of COHORT_FIELDS) {
    const value = searchParams.get(field);
    if (value !== null) {
      filter[field] = value;
    }
  }
  return <CohortPage key={`${runId}?${searchParams.toString()}`} api={props.api} runId={runId}
                     initialFilter={filter} />;
}

function RunReviewRoute(props: RouteProps): ReactElement {
  const runId = useParams().runId ?? "";
  return <RunReviewPage key={runId} api={props.api} runId={runId} />;
}

function CompareRoute(props: RouteProps): ReactElement {
  const runs = useSearchParams()[0].get("runs") ?? "";
  return <ComparePage key={runs} api={props.api} runIds={runs.split(",")} />;
}

interface ConsoleRoutesProps extends RouteProps {
  principal?: Principal | null;
  onSignOut?(): void;
}

export function ConsoleRoutes(props: ConsoleRoutesProps): ReactElement {
  const principal = props.principal ?? null;
  return (
    <div className="console">
      <header className="console-header">
        <strong>RL World</strong>
        <nav>
          <NavLink to="/configure">Configure</NavLink>
          <NavLink to="/catalogs">Catalogs</NavLink>
          <NavLink to="/runs" end>Runs</NavLink>
          {hasAnyRole(principal, QUEUE_ROLES) ? <NavLink to="/queues">Queues</NavLink> : null}
          {hasAnyRole(principal, ADMIN_ROLES) ? <NavLink to="/admin">Admin</NavLink> : null}
        </nav>
        {principal !== null
          ? <span className="signed-in">{principal.email || principal.subject} ({principal.roles.join(", ")})
            {props.onSignOut ? <button type="button" onClick={props.onSignOut}>Sign out</button> : null}</span>
          : null}
      </header>
      <main>
        <Routes>
          <Route path="/" element={<Navigate to="/configure" replace />} />
          <Route path="/configure" element={<ConfigureRoute api={props.api} />} />
          <Route path="/catalogs" element={<CatalogsPage api={props.api} />} />
          <Route path="/launch/:digest" element={<LaunchRoute api={props.api} />} />
          <Route path="/experiments/:experimentId" element={<ExperimentRoute api={props.api} />} />
          <Route path="/runs" element={<RunsPage api={props.api} />} />
          <Route path="/runs/:runId" element={<RunRoute api={props.api} />} />
          <Route path="/runs/:runId/live" element={<LiveWallRoute api={props.api} />} />
          <Route path="/runs/:runId/episodes/:episodeId" element={<EpisodeRoute api={props.api} />} />
          <Route path="/runs/:runId/episodes/:episodeId/review" element={<EpisodeReviewRoute api={props.api} />} />
          <Route path="/runs/:runId/cohort" element={<CohortRoute api={props.api} />} />
          <Route path="/runs/:runId/review" element={<RunReviewRoute api={props.api} />} />
          <Route path="/compare" element={<CompareRoute api={props.api} />} />
          <Route path="/queues" element={<QueuesPage api={props.api} />} />
          <Route path="/admin" element={<AdminPage api={props.api} />} />
        </Routes>
      </main>
    </div>
  );
}

// How the shell signs users in; the browser version redirects to the IdP and back.
export interface SignInDriver {
  tokens: TokenSource;
  start(config: AuthConfig): Promise<void>;
  finish(config: AuthConfig, callbackUrl: string): Promise<void>;
  signOut(): void;
}

export class BrowserSignIn implements SignInDriver {
  readonly tokens: SessionTokens;

  constructor(tokens: SessionTokens) {
    this.tokens = tokens;
  }

  async start(config: AuthConfig): Promise<void> {
    window.location.assign(await signInUrl(config, window.location.origin, window.sessionStorage, window));
  }

  finish(config: AuthConfig, callbackUrl: string): Promise<void> {
    return completeSignIn(config, callbackUrl, window.location.origin, window.sessionStorage, this.tokens, window);
  }

  signOut(): void {
    this.tokens.clear();
  }
}

interface ShellProps extends RouteProps {
  signIn: SignInDriver;
  navigate: NavigateTo;
  grantRefreshMilliseconds?: number;
}

interface ShellState {
  phase: "loading" | "signed_out" | "ready";
  config: AuthConfig;
  principal: Principal | null;
  problems: string[];
}

export class ConsoleShell extends Component<ShellProps, ShellState> {
  private timer: ReturnType<typeof setInterval> | null = null;

  constructor(props: ShellProps) {
    super(props);
    this.state = { phase: "loading", config: { enabled: false }, principal: null, problems: [] };
    this.handleSignIn = this.handleSignIn.bind(this);
    this.handleSignOut = this.handleSignOut.bind(this);
    this.refreshGrant = this.refreshGrant.bind(this);
  }

  async componentDidMount(): Promise<void> {
    const { api, signIn } = this.props;
    try {
      const config = await api.authConfig();
      if (config.enabled && window.location.pathname === CALLBACK_PATH) {
        await signIn.finish(config, window.location.href);
        this.props.navigate("/", { replace: true });
      }
      if (config.enabled && signIn.tokens.accessToken() === null) {
        this.setState({ phase: "signed_out", config });
        return;
      }
      const principal = await api.me();
      await this.refreshGrant();
      this.timer = setInterval(this.refreshGrant, this.props.grantRefreshMilliseconds ?? 600_000);
      this.setState({ phase: "ready", config, principal });
    } catch (error) {
      this.setState({ phase: "signed_out", problems: failureProblems(error) });
    }
  }

  componentWillUnmount(): void {
    if (this.timer !== null) {
      clearInterval(this.timer);
    }
  }

  // Frames load through img elements, so they carry a short lived grant instead of the bearer token.
  async refreshGrant(): Promise<void> {
    this.props.api.setFrameGrant((await this.props.api.frameGrant()).grant);
  }

  async handleSignIn(): Promise<void> {
    try {
      await this.props.signIn.start(this.state.config);
    } catch (error) {
      this.setState({ problems: failureProblems(error) });
    }
  }

  handleSignOut(): void {
    this.props.signIn.signOut();
    this.setState({ phase: "signed_out", principal: null });
  }

  render(): ReactElement {
    if (this.state.phase === "loading") {
      return <p className="empty">Loading.</p>;
    }
    if (this.state.phase === "signed_out") {
      return (
        <div className="sign-in">
          <h1>RL World</h1>
          <ProblemList title="Sign in" problems={this.state.problems} />
          <button type="button" className="primary" onClick={this.handleSignIn}>Sign in</button>
        </div>
      );
    }
    return <ConsoleRoutes api={this.props.api} principal={this.state.principal}
                          onSignOut={this.state.config.enabled ? this.handleSignOut : undefined} />;
  }
}

function ShellRoute(props: { api: ConsoleApi; signIn: SignInDriver }): ReactElement {
  return <ConsoleShell api={props.api} signIn={props.signIn} navigate={useNavigate()} />;
}

export function App(props: { api: ConsoleApi; signIn: SignInDriver }): ReactElement {
  return (
    <BrowserRouter>
      <ShellRoute api={props.api} signIn={props.signIn} />
    </BrowserRouter>
  );
}

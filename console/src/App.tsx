// The console shell: one app for configure, launch, watch and review (DESIGN.md 6.14).
import type { ReactElement } from "react";
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

import type { ConsoleApi } from "./api/client";
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

export function ConsoleRoutes(props: RouteProps): ReactElement {
  return (
    <div className="console">
      <header className="console-header">
        <strong>RL World</strong>
        <nav>
          <NavLink to="/configure">Configure</NavLink>
          <NavLink to="/catalogs">Catalogs</NavLink>
          <NavLink to="/runs" end>Runs</NavLink>
        </nav>
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
        </Routes>
      </main>
    </div>
  );
}

export function App(props: RouteProps): ReactElement {
  return (
    <BrowserRouter>
      <ConsoleRoutes api={props.api} />
    </BrowserRouter>
  );
}

// The console shell: one app for configure, launch, watch and review (DESIGN.md 6.14).
import type { ReactElement } from "react";
import { BrowserRouter, Navigate, NavLink, Route, Routes, useNavigate, useParams } from "react-router-dom";

import type { ConsoleApi } from "./api/client";
import { EpisodeLiveView } from "./components/EpisodeLiveView";
import { CatalogsPage } from "./pages/CatalogsPage";
import { LaunchPage } from "./pages/LaunchPage";
import { RunDashboardPage } from "./pages/RunDashboardPage";
import { ExperimentPage, LiveWallPage, RunsPage } from "./pages/RunPages";
import { SpecEditorPage } from "./pages/SpecEditorPage";

interface RouteProps {
  api: ConsoleApi;
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
          <Route path="/configure" element={<SpecEditorPage api={props.api} />} />
          <Route path="/catalogs" element={<CatalogsPage api={props.api} />} />
          <Route path="/launch/:digest" element={<LaunchRoute api={props.api} />} />
          <Route path="/experiments/:experimentId" element={<ExperimentRoute api={props.api} />} />
          <Route path="/runs" element={<RunsPage api={props.api} />} />
          <Route path="/runs/:runId" element={<RunRoute api={props.api} />} />
          <Route path="/runs/:runId/live" element={<LiveWallRoute api={props.api} />} />
          <Route path="/runs/:runId/episodes/:episodeId" element={<EpisodeRoute api={props.api} />} />
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

// The console shell: one app for configure, launch, watch and review (DESIGN.md 6.14).
import type { ReactElement } from "react";
import { BrowserRouter, Navigate, NavLink, Route, Routes } from "react-router-dom";

import type { ConsoleApi } from "./api/client";
import { CatalogsPage } from "./pages/CatalogsPage";
import { SpecEditorPage } from "./pages/SpecEditorPage";

export function ConsoleRoutes(props: { api: ConsoleApi }): ReactElement {
  return (
    <div className="console">
      <header className="console-header">
        <strong>RL World</strong>
        <nav>
          <NavLink to="/configure">Configure</NavLink>
          <NavLink to="/catalogs">Catalogs</NavLink>
        </nav>
      </header>
      <main>
        <Routes>
          <Route path="/" element={<Navigate to="/configure" replace />} />
          <Route path="/configure" element={<SpecEditorPage api={props.api} />} />
          <Route path="/catalogs" element={<CatalogsPage api={props.api} />} />
        </Routes>
      </main>
    </div>
  );
}

export function App(props: { api: ConsoleApi }): ReactElement {
  return (
    <BrowserRouter>
      <ConsoleRoutes api={props.api} />
    </BrowserRouter>
  );
}

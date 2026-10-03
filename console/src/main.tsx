import { StrictMode } from "react";
import { createRoot } from "react-dom/client";

import { ApiClient } from "./api/client";
import { App } from "./App";
import "./styles.css";

const rootElement = document.getElementById("root");
if (rootElement !== null) {
  createRoot(rootElement).render(
    <StrictMode>
      <App api={new ApiClient(import.meta.env.VITE_API_BASE_URL ?? "/api")} />
    </StrictMode>,
  );
}

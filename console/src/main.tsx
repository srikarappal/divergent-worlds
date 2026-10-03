import { StrictMode } from "react";
import { createRoot } from "react-dom/client";

import { SessionTokens } from "./api/auth";
import { ApiClient } from "./api/client";
import { App, BrowserSignIn } from "./App";
import "./styles.css";

const rootElement = document.getElementById("root");
if (rootElement !== null) {
  const tokens = new SessionTokens(window.sessionStorage);
  createRoot(rootElement).render(
    <StrictMode>
      <App api={new ApiClient(import.meta.env.VITE_API_BASE_URL ?? "/api", window, tokens)}
           signIn={new BrowserSignIn(tokens)} />
    </StrictMode>,
  );
}

// Platform sign in (DESIGN.md 6.14, Admin 1): OIDC authorization code flow with PKCE against the company IdP.
// The access token lives in session storage for the tab's lifetime and travels as a bearer token.
import type { FetchTransport, TokenSource } from "./client";
import type { AuthConfig } from "./types";

export const CALLBACK_PATH = "/auth/callback";
const VERIFIER_KEY = "rlworld.pkce_verifier";
const STATE_KEY = "rlworld.sign_in_state";
const TOKEN_KEY = "rlworld.access_token";
const EXPIRY_KEY = "rlworld.access_token_expires_at";
const EXPIRY_MARGIN_MILLISECONDS = 30_000;

export interface OidcEndpoints {
  authorization_endpoint: string;
  token_endpoint: string;
}

export interface Clock {
  (): number;
}

export class SessionTokens implements TokenSource {
  private readonly storage: Storage;
  private readonly now: Clock;

  constructor(storage: Storage, now: Clock = Date.now) {
    this.storage = storage;
    this.now = now;
  }

  accessToken(): string | null {
    const expiresAt = Number(this.storage.getItem(EXPIRY_KEY) ?? 0);
    return expiresAt - EXPIRY_MARGIN_MILLISECONDS > this.now() ? this.storage.getItem(TOKEN_KEY) : null;
  }

  store(token: string, expiresInSeconds: number): void {
    this.storage.setItem(TOKEN_KEY, token);
    this.storage.setItem(EXPIRY_KEY, String(this.now() + expiresInSeconds * 1000));
  }

  clear(): void {
    this.storage.removeItem(TOKEN_KEY);
    this.storage.removeItem(EXPIRY_KEY);
  }
}

export function base64Url(bytes: Uint8Array): string {
  let binary = "";
  for (const byte of bytes) {
    binary += String.fromCharCode(byte);
  }
  return btoa(binary).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

export function randomString(byteCount: number): string {
  const bytes = new Uint8Array(byteCount);
  crypto.getRandomValues(bytes);
  return base64Url(bytes);
}

export async function codeChallenge(verifier: string): Promise<string> {
  const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(verifier));
  return base64Url(new Uint8Array(digest));
}

export async function discover(issuer: string, transport: FetchTransport): Promise<OidcEndpoints> {
  const response = await transport.fetch(`${issuer.replace(/\/$/, "")}/.well-known/openid-configuration`);
  if (!response.ok) {
    throw new Error(`the identity provider at ${issuer} did not answer discovery: ${response.status}`);
  }
  return response.json();
}

// Builds the authorize URL and remembers the PKCE verifier and state for the callback.
export async function signInUrl(config: AuthConfig, origin: string, storage: Storage,
                                transport: FetchTransport): Promise<string> {
  const endpoints = await discover(config.issuer ?? "", transport);
  const verifier = randomString(48);
  const state = randomString(16);
  storage.setItem(VERIFIER_KEY, verifier);
  storage.setItem(STATE_KEY, state);
  const query = new URLSearchParams({
    response_type: "code",
    client_id: config.client_id ?? "",
    redirect_uri: `${origin}${CALLBACK_PATH}`,
    scope: (config.scopes ?? ["openid"]).join(" "),
    state,
    code_challenge: await codeChallenge(verifier),
    code_challenge_method: "S256",
  });
  return `${endpoints.authorization_endpoint}?${query.toString()}`;
}

// Trades the authorization code for tokens; a state that does not match this tab's sign in is refused.
export async function completeSignIn(config: AuthConfig, callbackUrl: string, origin: string, storage: Storage,
                                     tokens: SessionTokens, transport: FetchTransport): Promise<void> {
  const parameters = new URL(callbackUrl).searchParams;
  const failure = parameters.get("error");
  if (failure !== null) {
    throw new Error(`sign in failed: ${parameters.get("error_description") ?? failure}`);
  }
  const code = parameters.get("code");
  if (code === null || parameters.get("state") !== storage.getItem(STATE_KEY)) {
    throw new Error("the sign in response does not belong to this browser session");
  }
  const endpoints = await discover(config.issuer ?? "", transport);
  const response = await transport.fetch(endpoints.token_endpoint, {
    method: "POST",
    headers: { "Content-Type": "application/x-www-form-urlencoded" },
    body: new URLSearchParams({
      grant_type: "authorization_code",
      code,
      redirect_uri: `${origin}${CALLBACK_PATH}`,
      client_id: config.client_id ?? "",
      code_verifier: storage.getItem(VERIFIER_KEY) ?? "",
    }).toString(),
  });
  if (!response.ok) {
    throw new Error(`the identity provider refused the sign in: ${response.status}`);
  }
  const payload: { access_token: string; expires_in?: number } = await response.json();
  tokens.store(payload.access_token, payload.expires_in ?? 300);
  storage.removeItem(VERIFIER_KEY);
  storage.removeItem(STATE_KEY);
}

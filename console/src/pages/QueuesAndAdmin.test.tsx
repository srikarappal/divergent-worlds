import { fireEvent, render, screen, within } from "@testing-library/react";
import type { ReactElement } from "react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, expect, test } from "vitest";

import { base64Url, codeChallenge, completeSignIn, SessionTokens, signInUrl } from "../api/auth";
import type { FetchTransport } from "../api/client";
import type { AuthConfig, ReviewItem } from "../api/types";
import { ConsoleShell, type SignInDriver } from "../App";
import { FakeApi, experimentRecord } from "../test/fakeApi";
import { AdminPage } from "./AdminPage";
import { QueuesPage } from "./QueuesPage";

const CONFIG: AuthConfig = { enabled: true, issuer: "https://sso.example.com/realms/platform", client_id: "console",
                             audience: "rlworld", scopes: ["openid", "email"] };
const navigations: string[] = [];

function recordNavigation(to: string): void {
  navigations.push(to);
}

function reset(): void {
  navigations.length = 0;
  window.sessionStorage.clear();
  window.history.replaceState(null, "", "/");
}

beforeEach(reset);
afterEach(reset);

function inRouter(page: ReactElement): ReactElement {
  return <MemoryRouter>{page}</MemoryRouter>;
}

class IdentityProvider implements FetchTransport {
  readonly requests: Array<{ url: string; init: RequestInit | undefined }> = [];

  async fetch(input: string, init?: RequestInit): Promise<Response> {
    this.requests.push({ url: input, init });
    if (input.endsWith("/.well-known/openid-configuration")) {
      return new Response(JSON.stringify({
        authorization_endpoint: "https://sso.example.com/auth", token_endpoint: "https://sso.example.com/token" }));
    }
    return new Response(JSON.stringify({ access_token: "token_from_idp", expires_in: 600 }));
  }
}

class FixedNow {
  readonly now = 1_000_000;

  time(): number {
    return this.now;
  }
}

async function signInUsesPkceAndChecksState(): Promise<void> {
  const provider = new IdentityProvider();
  const url = new URL(await signInUrl(CONFIG, "https://console.example.com", window.sessionStorage, provider));
  expect(url.origin + url.pathname).toBe("https://sso.example.com/auth");
  expect(url.searchParams.get("redirect_uri")).toBe("https://console.example.com/auth/callback");
  expect(url.searchParams.get("scope")).toBe("openid email");
  expect(url.searchParams.get("code_challenge_method")).toBe("S256");
  const verifier = window.sessionStorage.getItem("rlworld.pkce_verifier") ?? "";
  expect(url.searchParams.get("code_challenge")).toBe(await codeChallenge(verifier));
  const clock = new FixedNow();
  const tokens = new SessionTokens(window.sessionStorage, clock.time.bind(clock));
  await expect(completeSignIn(CONFIG, "https://console.example.com/auth/callback?code=abc&state=forged",
                              "https://console.example.com", window.sessionStorage, tokens, provider))
    .rejects.toThrow("does not belong to this browser session");
  const state = url.searchParams.get("state") ?? "";
  await completeSignIn(CONFIG, `https://console.example.com/auth/callback?code=abc&state=${state}`,
                       "https://console.example.com", window.sessionStorage, tokens, provider);
  const exchange = provider.requests[provider.requests.length - 1];
  const form = new URLSearchParams(String(exchange.init?.body));
  expect(form.get("grant_type")).toBe("authorization_code");
  expect(form.get("code_verifier")).toBe(verifier);
  expect(tokens.accessToken()).toBe("token_from_idp");
  expect(window.sessionStorage.getItem("rlworld.pkce_verifier")).toBeNull();
}

test("sign in uses PKCE and refuses a callback from another session", signInUsesPkceAndChecksState);

function tokensExpireBeforeTheirDeadline(): void {
  const clock = new FixedNow();
  const tokens = new SessionTokens(window.sessionStorage, clock.time.bind(clock));
  tokens.store("short_lived", 20);
  expect(tokens.accessToken()).toBeNull();
  tokens.store("long_lived", 600);
  expect(tokens.accessToken()).toBe("long_lived");
  tokens.clear();
  expect(tokens.accessToken()).toBeNull();
  expect(base64Url(new Uint8Array([251, 255]))).toBe("-_8");
}

test("tokens are dropped shortly before they expire", tokensExpireBeforeTheirDeadline);

class MutableToken {
  value: string | null = null;

  accessToken(): string | null {
    return this.value;
  }
}

class FakeSignIn implements SignInDriver {
  readonly tokens = new MutableToken();
  readonly calls: string[] = [];

  async start(): Promise<void> {
    this.calls.push("start");
  }

  async finish(_config: AuthConfig, callbackUrl: string): Promise<void> {
    this.calls.push(`finish ${new URL(callbackUrl).pathname}`);
    this.tokens.value = "token_after_callback";
  }

  signOut(): void {
    this.calls.push("sign out");
    this.tokens.value = null;
  }
}

function shell(api: FakeApi, signIn: FakeSignIn): ReactElement {
  return inRouter(<ConsoleShell api={api} signIn={signIn} navigate={recordNavigation} />);
}

async function theShellSignsUsersInAndShowsTheirNavigation(): Promise<void> {
  const api = new FakeApi();
  api.authSettings = CONFIG;
  const signIn = new FakeSignIn();
  const view = render(shell(api, signIn));
  fireEvent.click(await screen.findByRole("button", { name: "Sign in" }));
  expect(signIn.calls).toEqual(["start"]);
  view.unmount();
  window.history.replaceState(null, "", "/auth/callback?code=abc&state=xyz");
  api.principal = { subject: "user_kim", email: "kim@example.com", tenant: "default", roles: ["labeler"] };
  render(shell(api, signIn));
  expect(await screen.findByRole("link", { name: "Queues" })).toBeInTheDocument();
  expect(signIn.calls).toEqual(["start", "finish /auth/callback"]);
  expect(navigations).toEqual(["/"]);
  expect(screen.queryByRole("link", { name: "Admin" })).toBeNull();
  expect(screen.queryByRole("link", { name: "Author" })).toBeNull();
  expect(api.grant).toBe("grant.1.sig");
  fireEvent.click(screen.getByRole("button", { name: "Sign out" }));
  expect(await screen.findByRole("button", { name: "Sign in" })).toBeInTheDocument();
}

test("the shell signs users in, refreshes the frame grant and shows their navigation",
     theShellSignsUsersInAndShowsTheirNavigation);

async function withoutSignInEverythingIsOpen(): Promise<void> {
  render(shell(new FakeApi(), new FakeSignIn()));
  expect(await screen.findByRole("link", { name: "Admin" })).toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "Sign out" })).toBeNull();
}

test("with sign in off the console opens straight away", withoutSignInEverythingIsOpen);

function labelingItem(): ReviewItem {
  return {
    item_id: "review_000001", action: "label", queue: "labeling", run_id: "run_1", episode_ids: ["episode_b"],
    title: "label task_000", note: "", label: null, payload: {}, status: "open", created_by: "user_ada",
    created_at: 0, resolved_by: "", resolved_at: null, source: "console",
  };
}

async function labelersResolveItemsWithVerdictsAndRubricLabels(): Promise<void> {
  const api = new FakeApi();
  api.reviewItems = [labelingItem(), { ...labelingItem(), item_id: "review_000002", queue: "verifier",
                                       title: "verifier false negative suspected", action: "verifier_bug" }];
  api.provenance = {
    episode_id: "episode_b", initial_state: {}, final_state: {}, world_diff: [],
    reward: { total: 0, components: {}, termination: "failure", counts_for_training: true, error_detail: null,
              side_effect_violations: [],
              checks: [{ name: "tone", tier: "judge", passed: false, weight: 1, detail: "", expected: null,
                         actual: null }],
              provenance: { verifier_versions: {}, judge_model: "j", rubric_version: "1", judge_skipped: false,
                            judge_cached: false, state_hash: "s", ground_truth_digest: "g", reward_policy: {} } },
  };
  api.calibrationReport = { verifier: { labels: 40, precision: 0.97, recall: 0.9, false_passes: 1, false_fails: 4 },
                            judge: { tone: { label_count: 40, accuracy: 0.9, kappa: 0.72, false_passes: 2,
                                             false_fails: 2 } },
                            judge_problems: ["tone: kappa 0.72 is below 0.8"] };
  render(inRouter(<QueuesPage api={api} />));
  const queue = await screen.findByRole("table", { name: "labeling queue" });
  expect(screen.getByLabelText("calibration")).toHaveTextContent("precision 97%, recall 90%");
  expect(screen.getByLabelText("Judge calibration problems")).toHaveTextContent("kappa 0.72");
  fireEvent.click(within(queue).getByRole("button", { name: "Judge rubric items" }));
  fireEvent.click(await within(queue).findByLabelText("tone passes"));
  fireEvent.change(within(queue).getByLabelText("Verdict"), { target: { value: "success" } });
  fireEvent.click(within(queue).getByRole("button", { name: "Save label" }));
  expect(await screen.findByText("Nothing open in this queue.")).toBeInTheDocument();
  expect(api.callsTo("updateReviewItem")[0]).toEqual(["review_000001", {
    status: "resolved", note: undefined, label: { items: { tone: true }, verdict: "success" } }]);
  fireEvent.click(screen.getByRole("button", { name: "Verifier bugs" }));
  const bugs = await screen.findByRole("table", { name: "verifier queue" });
  fireEvent.change(within(bugs).getByLabelText("review_000002 note"), { target: { value: "fixed in v3" } });
  fireEvent.click(within(bugs).getByRole("button", { name: "Dismiss" }));
  await screen.findByText("Nothing open in this queue.");
  expect(api.callsTo("updateReviewItem")[1]).toEqual(["review_000002", { status: "dismissed", note: "fixed in v3",
                                                                         label: undefined }]);
}

test("labelers resolve items with verdicts and rubric labels; bug queues resolve with notes",
     labelersResolveItemsWithVerdictsAndRubricLabels);

async function adminsManageRolesQuotasPoliciesAndAudits(): Promise<void> {
  const api = new FakeApi();
  api.experiments.experiment_000001 = experimentRecord({ tenant: "finance_lab", spent_usd: 120 });
  api.pools = { office_ltsc: 7 };
  api.auditRecords = [{ sequence: 1, at: 0, actor: "user_ada", tenant: "default", action: "launch",
                        target: { digest: "sha256:a" }, detail: { budget_usd: 900 } }];
  render(inRouter(<AdminPage api={api} />));
  const roles = await screen.findByLabelText("roles");
  fireEvent.change(within(roles).getByLabelText("User"), { target: { value: "user_kim" } });
  fireEvent.click(within(roles).getByLabelText("labeler"));
  fireEvent.click(within(roles).getByRole("button", { name: "Save roles" }));
  expect(await within(roles).findByText("labeler")).toBeInTheDocument();
  const quotas = screen.getByLabelText("tenants and quotas");
  fireEvent.change(within(quotas).getByLabelText("Tenant"), { target: { value: "finance_lab" } });
  fireEvent.change(within(quotas).getByLabelText("Quota"), { target: { value: "200" } });
  fireEvent.click(within(quotas).getByRole("button", { name: "Set quota" }));
  expect(await within(quotas).findByText("200")).toBeInTheDocument();
  expect(screen.getByLabelText("budgets")).toHaveTextContent("finance_lab");
  const pools = screen.getByLabelText("license and identity pools");
  expect(pools).toHaveTextContent("office_ltsc7");
  fireEvent.click(within(pools).getByRole("button", { name: "Apply retention now" }));
  expect(await within(pools).findByRole("status")).toHaveTextContent("Deleted 1 episodes and 2 blobs");
  const policies = screen.getByLabelText("data policies");
  fireEvent.change(within(policies).getByLabelText("Tenant"), { target: { value: "finance_lab" } });
  fireEvent.change(within(policies).getByLabelText("Screenshot endpoints"),
                   { target: { value: "policy_vllm, judge" } });
  fireEvent.click(within(policies).getByLabelText("sft exports"));
  fireEvent.click(within(policies).getByRole("button", { name: "Save policy" }));
  expect(await within(policies).findByText("policy_vllm, judge")).toBeInTheDocument();
  expect(api.callsTo("setDataPolicy")[0]).toEqual(["finance_lab", { screenshot_endpoints: ["policy_vllm", "judge"],
    trajectory_days: 365, full_frame_days: 30, exports: ["preferences"] }]);
  const audit = screen.getByLabelText("audit log");
  expect(audit).toHaveTextContent("user_ada");
  fireEvent.change(within(audit).getByLabelText("Action"), { target: { value: "launch" } });
  fireEvent.click(within(audit).getByRole("button", { name: "Filter" }));
  await screen.findByText(/sha256:a/);
  expect(api.callsTo("auditLog")[1]).toEqual([{ actor: undefined, action: "launch" }]);
}

test("admins manage roles, quotas, data policies and read the audit log", adminsManageRolesQuotasPoliciesAndAudits);

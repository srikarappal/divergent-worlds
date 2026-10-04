import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, expect, test } from "vitest";

import { ApiError } from "../api/client";
import type { Principal, Recording } from "../api/types";
import { ConsoleRoutes } from "../App";
import { authoringSession, FakeApi, gateOutcome } from "../test/fakeApi";
import { FakeConnector } from "../test/fakeScreens";
import { AuthoringPage } from "./AuthoringPage";

const RELAY_BASE = "wss://console.example/fleet";
const SESSION_ID = "authoring_000001";
const navigations: string[] = [];

function recordNavigation(to: string): void {
  navigations.push(to);
}

function reset(): void {
  navigations.length = 0;
}

beforeEach(reset);

function apiWithSession(extra: Parameters<typeof authoringSession>[1] = {}): FakeApi {
  const api = new FakeApi();
  api.authoringSessions[SESSION_ID] = authoringSession(SESSION_ID, extra);
  return api;
}

function recordingFromTheCli(): Recording {
  return { recording_id: "recording_000009", kind: "oracle", status: "recording", recorded_by: "ada",
           lease_id: "lease_9", actions: [], complete: true, lost_reason: "", error: "" };
}

function principalWith(roles: string[]): Principal {
  return { subject: "user_ada", email: "ada@example.com", tenant: "finance_lab", roles };
}

async function anAuthorStartsFromASavedSpec(): Promise<void> {
  const api = new FakeApi();
  render(<AuthoringPage api={api} navigate={recordNavigation} initialDigest="sha256:abc123" />);
  fireEvent.change(screen.getByLabelText("Task"), { target: { value: "2" } });
  expect(screen.getByLabelText("start_authoring cli")).toHaveTextContent(
    "rlworld authoring start sha256:abc123 --task-index 2");
  fireEvent.click(screen.getByRole("button", { name: "Start authoring" }));
  await waitFor(navigatedToTheNewSession);
  expect(api.callsTo("startAuthoring")).toEqual([["sha256:abc123", 2]]);
}

function navigatedToTheNewSession(): void {
  expect(navigations).toEqual(["/authoring/authoring_000001"]);
}

test("an author starts a session from a saved spec", anAuthorStartsFromASavedSpec);

function startingNeedsADigest(): void {
  render(<AuthoringPage api={new FakeApi()} navigate={recordNavigation} />);
  expect(screen.getByRole("button", { name: "Start authoring" })).toBeDisabled();
  fireEvent.change(screen.getByLabelText("Spec digest"), { target: { value: " sha256:abc123 " } });
  expect(screen.getByRole("button", { name: "Start authoring" })).toBeEnabled();
  expect(screen.getByLabelText("start_authoring cli")).toHaveTextContent("rlworld authoring start sha256:abc123");
}

test("starting needs a spec digest", startingNeedsADigest);

async function anAuthorRecordsThroughNoVncAndStops(): Promise<void> {
  const api = apiWithSession({ hidden_intent: "Only invoices issued in March 2026 count." });
  const connector = new FakeConnector();
  const view = render(<AuthoringPage api={api} navigate={recordNavigation} sessionId={SESSION_ID}
                                     screens={{ connector, relayBase: RELAY_BASE }} />);
  expect(await screen.findByRole("heading", { name: /task_000/ })).toBeInTheDocument();
  expect(screen.getByText(/only the user simulator knows: Only invoices issued in March 2026 count\./))
    .toBeInTheDocument();
  fireEvent.change(screen.getByLabelText("Record"), { target: { value: "near_miss" } });
  expect(screen.getByLabelText("begin_recording cli")).toHaveTextContent(
    `rlworld authoring record ${SESSION_ID} --kind near_miss`);
  fireEvent.click(screen.getByRole("button", { name: "Record in a fresh dev env" }));
  const authorScreen = await screen.findByLabelText("author screen");
  expect(api.callsTo("beginRecording")).toEqual([[SESSION_ID, "near_miss"]]);
  expect(connector.connections).toEqual([
    { target: authorScreen, url: "wss://console.example/fleet/v1/takeover/credential_1" }]);
  expect(screen.getByText(/Recording the near miss/)).toBeInTheDocument();
  const recordings = await screen.findByRole("table", { name: "recordings" });
  expect(within(recordings).getByText("recording")).toBeInTheDocument();
  expect(within(recordings).queryByRole("button", { name: "Stop" })).toBeNull();
  expect(screen.getByRole("button", { name: "Run every gate" })).toBeDisabled();
  expect(view.container.innerHTML).not.toContain("credential_1");
  fireEvent.click(screen.getByRole("button", { name: "Stop recording" }));
  expect(await within(recordings).findByText("recorded")).toBeInTheDocument();
  expect(within(recordings).getByText("2 actions")).toBeInTheDocument();
  expect(connector.screen.disconnects).toBe(1);
  expect(api.callsTo("endRecording")).toEqual([[SESSION_ID, "recording_000001"]]);
  expect(screen.queryByLabelText("author screen")).toBeNull();
  expect(screen.getByRole("button", { name: "Run every gate" })).toBeEnabled();
}

test("an author records through noVNC, then stops and keeps the recording", anAuthorRecordsThroughNoVncAndStops);

async function recordingsStartedElsewhereStopFromTheirRow(): Promise<void> {
  const api = apiWithSession({ recordings: [recordingFromTheCli()] });
  render(<AuthoringPage api={api} navigate={recordNavigation} sessionId={SESSION_ID}
                        screens={{ connector: new FakeConnector(), relayBase: RELAY_BASE }} />);
  fireEvent.click(await screen.findByRole("button", { name: "Stop" }));
  expect(await screen.findByText("recorded")).toBeInTheDocument();
  expect(api.callsTo("endRecording")).toEqual([[SESSION_ID, "recording_000009"]]);
  expect(screen.queryByRole("button", { name: "Stop" })).toBeNull();
}

test("a recording started from the CLI can be stopped from its row", recordingsStartedElsewhereStopFromTheirRow);

async function gatesRunThenTheTaskGoesToReview(): Promise<void> {
  const api = apiWithSession();
  api.gateReport = { status: "failed", error: "", results: [
    gateOutcome("null_action", true), gateOutcome("oracle", true),
    gateOutcome("near_miss", false, "no near miss was provided, so false positives go unseen")] };
  render(<AuthoringPage api={api} navigate={recordNavigation} sessionId={SESSION_ID} pollMilliseconds={5}
                        screens={{ connector: new FakeConnector(), relayBase: RELAY_BASE }} />);
  expect(await screen.findByRole("status")).toHaveTextContent("The gates have not run.");
  expect(screen.getByRole("button", { name: "Submit for review" })).toBeDisabled();
  fireEvent.click(screen.getByRole("button", { name: "Run every gate" }));
  expect(await screen.findByText("Some gates failed; reviewers see which and why.")).toBeInTheDocument();
  expect(api.callsTo("runAuthoringGates")).toEqual([[SESSION_ID]]);
  const results = screen.getByRole("table", { name: "gate results" });
  expect(within(results).getByText("near miss")).toBeInTheDocument();
  expect(within(results).getByText("no near miss was provided, so false positives go unseen")).toBeInTheDocument();
  expect(within(results).getAllByText("passed")).toHaveLength(2);
  fireEvent.click(screen.getByRole("button", { name: "Submit for review" }));
  expect(await screen.findByText("Submitted for review as review_000001.")).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "Run every gate" })).toBeDisabled();
  expect(screen.getByRole("button", { name: "Record in a fresh dev env" })).toBeDisabled();
  expect(screen.getByLabelText("submit_authoring cli")).toHaveTextContent(`rlworld authoring submit ${SESSION_ID}`);
}

test("gates run in fresh envs, then the task goes to review with its report", gatesRunThenTheTaskGoesToReview);

async function aRunningGateRunIsFollowedOnLoad(): Promise<void> {
  const api = apiWithSession({ gate_status: "running" });
  api.gateReport = { status: "error", results: [], error: "RuntimeError: the strong agent's endpoint is down" };
  render(<AuthoringPage api={api} navigate={recordNavigation} sessionId={SESSION_ID} pollMilliseconds={5} />);
  expect(await screen.findByText("The gates could not run: RuntimeError: the strong agent's endpoint is down"))
    .toBeInTheDocument();
  expect(screen.getByRole("button", { name: "Submit for review" })).toBeDisabled();
  expect(screen.getByRole("button", { name: "Run every gate" })).toBeEnabled();
}

test("a gate run in progress is followed until it ends", aRunningGateRunIsFollowedOnLoad);

async function refusalsShowTheirProblems(): Promise<void> {
  const api = apiWithSession();
  api.recordingFailure = new ApiError(409, "authoring_refused", "rejected", ["no dev env: 503 no_capacity"]);
  const connector = new FakeConnector();
  render(<AuthoringPage api={api} navigate={recordNavigation} sessionId={SESSION_ID}
                        screens={{ connector, relayBase: RELAY_BASE }} />);
  fireEvent.click(await screen.findByRole("button", { name: "Record in a fresh dev env" }));
  expect(await screen.findByText("no dev env: 503 no_capacity")).toBeInTheDocument();
  expect(connector.connections).toEqual([]);
  expect(screen.queryByLabelText("author screen")).toBeNull();
}

test("a refused recording shows why and opens no screen", refusalsShowTheirProblems);

async function unknownSessionsSayNotFound(): Promise<void> {
  render(<AuthoringPage api={new FakeApi()} navigate={recordNavigation} sessionId="authoring_404" />);
  expect(await screen.findByLabelText("Authoring problems")).toHaveTextContent("authoring_404");
}

test("an unknown session says so", unknownSessionsSayNotFound);

async function withoutTheRelayTheConsolePointsToTheCli(): Promise<void> {
  render(<AuthoringPage api={apiWithSession()} navigate={recordNavigation} sessionId={SESSION_ID} />);
  expect(await screen.findByRole("button", { name: "Record in a fresh dev env" })).toBeDisabled();
  expect(screen.getByText("This console cannot reach fleetd's relay; record from the CLI.")).toBeInTheDocument();
}

test("without fleetd's relay the console points authors to the CLI", withoutTheRelayTheConsolePointsToTheCli);

async function buildersFindAuthoringInTheNavigation(): Promise<void> {
  const api = apiWithSession();
  const viewer = render(<MemoryRouter initialEntries={["/runs"]}>
    <ConsoleRoutes api={api} principal={principalWith(["viewer", "labeler"])} />
  </MemoryRouter>);
  expect(screen.queryByRole("link", { name: "Author" })).toBeNull();
  viewer.unmount();
  render(<MemoryRouter initialEntries={[`/authoring/${SESSION_ID}`]}>
    <ConsoleRoutes api={api} principal={principalWith(["researcher"])} />
  </MemoryRouter>);
  expect(screen.getByRole("link", { name: "Author" })).toBeInTheDocument();
  expect(await screen.findByRole("heading", { name: /task_000/ })).toBeInTheDocument();
  expect(api.callsTo("getAuthoring")).toEqual([[SESSION_ID]]);
}

test("researchers, operators and admins find authoring in the navigation", buildersFindAuthoringInTheNavigation);

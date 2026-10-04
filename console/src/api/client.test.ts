import { expect, test } from "vitest";

import { ApiClient, ApiError, type FetchTransport, type RunEventListener } from "./client";
import { cliCommand, fillPath, fillTemplate, methodName, operation, OPERATIONS, sdkSnippet } from "./operations";
import type { RunEvent } from "./types";

const PATH_VALUES: Record<string, string> = {
  digest: "sha256:abc",
  experiment_id: "experiment_000001",
  run_id: "experiment_000001_run_000",
  episode_id: "b00000_g00000_s000_a0",
  step_index: "2",
  frame_digest: "ab12",
  kind: "images",
  name: "byo_judge",
  item_id: "review_000001",
  subject: "user_ada",
  tenant: "finance_lab",
  alert_id: "alert_000001",
  session_id: "authoring_000001",
  recording_id: "recording_000001",
};
const EVENT_STREAM = [
  'id: 1\nevent: launched\ndata: {"sequence": 1, "at": 1.0, "kind": "launched", "detail": {}}\n\n',
  'id: 2\nevent: metrics\ndata: {"sequence": 2, "at": 2.0, "kind": "metrics", "detail": {"episodes": 8}}\n\n',
].join("");

class RecordingTransport implements FetchTransport {
  readonly requests: Array<{ url: string; init: RequestInit | undefined }> = [];
  private readonly responses: Response[];

  constructor(responses: Response[] = []) {
    this.responses = responses;
  }

  async fetch(input: string, init?: RequestInit): Promise<Response> {
    this.requests.push({ url: input, init });
    return this.responses.shift() ?? new Response(JSON.stringify({ digest: "sha256:abc", yaml: "", runs: [],
                                                                   entries: [], endpoints: [], episodes: [],
                                                                   tiles: [], items: [], messages: [],
                                                                   experiments: [], rows: [], bindings: {},
                                                                   policies: [], records: [], pools: {},
                                                                   episode_id: "e" }));
  }
}

class EventCollector implements RunEventListener {
  readonly events: RunEvent[] = [];

  onRunEvent(event: RunEvent): void {
    this.events.push(event);
  }
}

function sampleArguments(operationName: string): unknown[] {
  switch (operationName) {
    case "save_spec":
      return ["experiment: {}"];
    case "launch":
      return [PATH_VALUES.digest, 250];
    case "control_run":
      return [PATH_VALUES.run_id, "pause"];
    case "run_events":
      return [PATH_VALUES.run_id, new EventCollector()];
    case "register_endpoint":
      return [{ name: "byo_judge" }];
    case "set_endpoint_key":
      return [PATH_VALUES.name, "sk_123"];
    case "get_episode":
    case "get_step_prompt":
      return [PATH_VALUES.run_id, PATH_VALUES.episode_id, 2];
    case "get_provenance":
      return [PATH_VALUES.run_id, PATH_VALUES.episode_id];
    case "review_episode":
      return [PATH_VALUES.run_id, PATH_VALUES.episode_id, "flag"];
    case "start_takeover":
    case "end_takeover":
      return [PATH_VALUES.run_id, PATH_VALUES.episode_id];
    case "start_authoring":
      return [PATH_VALUES.digest, 2];
    case "begin_recording":
      return [PATH_VALUES.session_id, "near_miss"];
    case "end_recording":
      return [PATH_VALUES.session_id, PATH_VALUES.recording_id];
    case "compare_runs":
      return [[PATH_VALUES.run_id, "experiment_000001_run_001"]];
    case "list_review_items":
    case "auth_config":
    case "me":
    case "frame_grant":
    case "list_experiments":
    case "calibration":
    case "list_roles":
    case "list_data_policies":
    case "audit_log":
    case "pool_status":
    case "sweep_retention":
    case "list_alerts":
    case "fleet_admission":
      return [];
    case "file_review_item":
      return [{ source: "gates", title: "task_017 fails a gate" }];
    case "update_review_item":
      return [PATH_VALUES.item_id, { status: "resolved" }];
    case "export_run":
      return [PATH_VALUES.run_id, PATH_VALUES.kind];
    case "set_roles":
      return [PATH_VALUES.subject, ["labeler"]];
    case "set_data_policy":
      return [PATH_VALUES.tenant, { screenshot_endpoints: null, trajectory_days: 30, full_frame_days: 7,
                                    exports: [] }];
    case "set_tenant_quota":
      return [PATH_VALUES.tenant, 40];
    case "clone_fidelity":
      return ["crm", "run_clone", "run_real"];
    case "list_runs":
    case "list_endpoints":
      return [];
    default:
      return [operationPathValue(operationName)];
  }
}

function operationPathValue(operationName: string): string {
  const placeholder = /\{(\w+)\}/.exec(operation(operationName).path);
  return placeholder ? PATH_VALUES[placeholder[1]] : "";
}

const OPERATION_CASES: Array<[string]> = [];
for (const listed of OPERATIONS) {
  OPERATION_CASES.push([listed.name]);
}

async function callsTheManifestRoute(operationName: string): Promise<void> {
  const transport = new RecordingTransport();
  const client = new ApiClient("/api/", transport);
  const method = (client as unknown as Record<string, (...args: unknown[]) => Promise<unknown>>)[
    methodName(operationName)];
  expect(typeof method).toBe("function");
  await method.apply(client, sampleArguments(operationName));
  const listed = operation(operationName);
  const request = transport.requests[0];
  expect(request.init?.method).toBe(listed.method);
  expect(request.url.split("?")[0]).toBe(`/api${fillPath(listed.path, PATH_VALUES)}`);
}

test.each(OPERATION_CASES)("%s is a client method on the manifest route", callsTheManifestRoute);

async function requestBodiesFollowTheManifest(): Promise<void> {
  const transport = new RecordingTransport();
  const client = new ApiClient("/api", transport);
  await client.saveSpec("experiment:\n  name: x\n");
  await client.listRuns("experiment_000001");
  await client.controlRun("run_1", "set_concurrency", 100);
  expect(transport.requests[0].init?.headers).toEqual({ "Content-Type": "application/yaml" });
  expect(transport.requests[0].init?.body).toBe("experiment:\n  name: x\n");
  expect(transport.requests[1].url).toBe("/api/v1/runs?experiment_id=experiment_000001");
  expect(JSON.parse(String(transport.requests[2].init?.body))).toEqual({ command: "set_concurrency", value: 100 });
}

test("request bodies and queries follow the manifest", requestBodiesFollowTheManifest);

async function authoringRequestsCarryTheSpecAndTheRecordingKind(): Promise<void> {
  const transport = new RecordingTransport();
  const client = new ApiClient("/api", transport);
  await client.startAuthoring("sha256:abc");
  await client.beginRecording("authoring_000001");
  await client.beginRecording("authoring_000001", "near_miss");
  expect(transport.requests[0].url).toBe("/api/v1/authoring");
  expect(JSON.parse(String(transport.requests[0].init?.body))).toEqual({ spec_digest: "sha256:abc", task_index: 0 });
  expect(transport.requests[1].url).toBe("/api/v1/authoring/authoring_000001/recordings");
  expect(JSON.parse(String(transport.requests[1].init?.body))).toEqual({ kind: "oracle" });
  expect(JSON.parse(String(transport.requests[2].init?.body))).toEqual({ kind: "near_miss" });
}

test("authoring requests carry the spec and the recording kind", authoringRequestsCarryTheSpecAndTheRecordingKind);

async function errorsCarryTheProblemList(): Promise<void> {
  const problems = ["task: live internet is for eval runs only"];
  const transport = new RecordingTransport([
    new Response(JSON.stringify({ code: "invalid_spec", problems }), { status: 400 }),
  ]);
  const error = await new ApiClient("/api", transport).saveSpec("x").catch(caught);
  expect(error).toBeInstanceOf(ApiError);
  expect((error as ApiError).status).toBe(400);
  expect((error as ApiError).problems).toEqual(problems);
}

function caught(error: unknown): unknown {
  return error;
}

test("errors carry the problem list", errorsCarryTheProblemList);

async function runEventsArriveInOrder(): Promise<void> {
  const transport = new RecordingTransport([new Response(EVENT_STREAM)]);
  const collector = new EventCollector();
  await new ApiClient("/api", transport).runEvents("run_1", collector, true, 0);
  expect(transport.requests[0].url).toBe("/api/v1/runs/run_1/events?follow=true&after=0");
  expect(collector.events.map(eventKind)).toEqual(["launched", "metrics"]);
  expect(collector.events[1].detail).toEqual({ episodes: 8 });
}

function eventKind(event: RunEvent): string {
  return event.kind;
}

test("run events arrive in order from the server sent event stream", runEventsArriveInOrder);

async function episodeReadsAskOnlyForNewSteps(): Promise<void> {
  const transport = new RecordingTransport();
  const client = new ApiClient("/api", transport);
  await client.getEpisode("run_1", "episode_1", 3);
  await client.listEpisodes("run_1", "running");
  await client.liveWall("run_1", 12);
  expect(transport.requests[0].url).toBe("/api/v1/runs/run_1/episodes/episode_1?first_step=3");
  expect(transport.requests[1].url).toBe("/api/v1/runs/run_1/episodes?status=running");
  expect(transport.requests[2].url).toBe("/api/v1/runs/run_1/live?limit=12");
  expect(client.frameUrl("ab12", "thumbnail")).toBe("/api/v1/frames/ab12?size=thumbnail");
}

test("episode reads ask only for new steps and frames have stable URLs", episodeReadsAskOnlyForNewSteps);

async function bearerTokensAndFrameGrantsTravel(): Promise<void> {
  const transport = new RecordingTransport();
  const client = new ApiClient("/api", transport, new FixedToken());
  await client.me();
  expect(transport.requests[0].init?.headers).toEqual({ Authorization: "Bearer token_abc" });
  client.setFrameGrant("grant.1.sig");
  expect(client.frameUrl("ab12", "full")).toBe("/api/v1/frames/ab12?size=full&grant=grant.1.sig");
}

class FixedToken {
  accessToken(): string | null {
    return "token_abc";
  }
}

test("bearer tokens and frame grants travel with every request", bearerTokensAndFrameGrantsTravel);

function templatesShowWhatIsStillMissing(): void {
  expect(fillTemplate("rlworld specs launch {digest} --budget-usd {budget_usd}", { digest: "sha256:abc" }))
    .toBe("rlworld specs launch sha256:abc --budget-usd {budget_usd}");
  expect(cliCommand("list_catalog", { kind: "rubrics" })).toBe("rlworld catalogs list rubrics");
  expect(sdkSnippet("preflight", { digest: "sha256:abc" })).toBe('client.preflight("sha256:abc")');
  expect(methodName("approve_experiment")).toBe("approveExperiment");
}

test("templates show what is still missing", templatesShowWhatIsStillMissing);

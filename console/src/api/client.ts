// Typed client for the public experiment API; every method is one operation from the shared manifest.
import { fillPath, operation } from "./operations";
import type {
  CatalogEntry,
  EndpointRegistration,
  EndpointView,
  ExperimentPreflight,
  ExperimentRecord,
  ProbeResult,
  RunEvent,
  RunRecord,
} from "./types";

export interface FetchTransport {
  fetch(input: string, init?: RequestInit): Promise<Response>;
}

export interface RunEventListener {
  onRunEvent(event: RunEvent): void;
}

export interface CallOptions {
  path?: Record<string, string>;
  query?: Record<string, string | number | boolean | null | undefined>;
  body?: unknown;
  signal?: AbortSignal;
}

export interface ConsoleApi {
  saveSpec(specYaml: string): Promise<string>;
  getSpec(digest: string): Promise<string>;
  preflight(digest: string): Promise<ExperimentPreflight>;
  dryRun(digest: string): Promise<string>;
  launch(digest: string, budgetUsd?: number): Promise<ExperimentRecord>;
  approveExperiment(experimentId: string): Promise<ExperimentRecord>;
  listRuns(experimentId?: string): Promise<RunRecord[]>;
  getRun(runId: string): Promise<RunRecord>;
  getRunSpec(runId: string): Promise<string>;
  controlRun(runId: string, command: string, value?: unknown): Promise<RunRecord>;
  runEvents(runId: string, listener: RunEventListener, follow?: boolean, after?: number,
            signal?: AbortSignal): Promise<void>;
  listCatalog(kind: string): Promise<CatalogEntry[]>;
  listEndpoints(): Promise<EndpointView[]>;
  registerEndpoint(endpoint: EndpointRegistration): Promise<EndpointView>;
  testEndpoint(name: string): Promise<ProbeResult>;
  setEndpointKey(name: string, key: string): Promise<void>;
}

const EVENT_DATA_PREFIX = "data: ";

export class ApiError extends Error {
  readonly status: number;
  readonly code: string;
  readonly problems: string[];

  constructor(status: number, code: string, message: string, problems: string[]) {
    super(`${status} ${code}: ${message}`);
    this.status = status;
    this.code = code;
    this.problems = problems;
  }
}

export async function apiError(response: Response): Promise<ApiError> {
  const text = await response.text();
  let payload: { code?: string; message?: string; problems?: string[] } = {};
  try {
    payload = JSON.parse(text);
  } catch {
    payload = { message: text.slice(0, 200) };
  }
  const problems = payload.problems ?? [];
  return new ApiError(response.status, payload.code ?? "unknown", payload.message ?? problems.join("; "), problems);
}

export function parseEventFrame(frame: string): RunEvent | null {
  for (const line of frame.split("\n")) {
    if (line.startsWith(EVENT_DATA_PREFIX)) {
      return JSON.parse(line.slice(EVENT_DATA_PREFIX.length)) as RunEvent;
    }
  }
  return null;
}

function requestInit(bodyKind: string | undefined, method: string, options: CallOptions): RequestInit {
  if (bodyKind === "yaml") {
    return { method, body: String(options.body ?? ""), headers: { "Content-Type": "application/yaml" },
             signal: options.signal };
  }
  if (bodyKind === "json") {
    return { method, body: JSON.stringify(options.body ?? {}), headers: { "Content-Type": "application/json" },
             signal: options.signal };
  }
  return { method, signal: options.signal };
}

export class ApiClient implements ConsoleApi {
  private readonly baseUrl: string;
  private readonly transport: FetchTransport;

  constructor(baseUrl: string, transport: FetchTransport = window) {
    this.baseUrl = baseUrl.replace(/\/$/, "");
    this.transport = transport;
  }

  async send(name: string, options: CallOptions = {}): Promise<Response> {
    const listed = operation(name);
    const query = new URLSearchParams();
    for (const key of listed.query ?? []) {
      const value = options.query?.[key];
      if (value !== undefined && value !== null && value !== "") {
        query.set(key, String(value));
      }
    }
    const queryText = query.toString();
    const url = `${this.baseUrl}${fillPath(listed.path, options.path ?? {})}${queryText ? `?${queryText}` : ""}`;
    const response = await this.transport.fetch(url, requestInit(listed.body, listed.method, options));
    if (!response.ok) {
      throw await apiError(response);
    }
    return response;
  }

  async call(name: string, options: CallOptions = {}): Promise<any> {
    const response = await this.send(name, options);
    const text = await response.text();
    return text ? JSON.parse(text) : null;
  }

  async saveSpec(specYaml: string): Promise<string> {
    return (await this.call("save_spec", { body: specYaml })).digest;
  }

  async getSpec(digest: string): Promise<string> {
    return (await this.call("get_spec", { path: { digest } })).yaml;
  }

  preflight(digest: string): Promise<ExperimentPreflight> {
    return this.call("preflight", { path: { digest } });
  }

  async dryRun(digest: string): Promise<string> {
    return (await this.call("dry_run", { path: { digest } })).episode_id;
  }

  launch(digest: string, budgetUsd?: number): Promise<ExperimentRecord> {
    return this.call("launch", { path: { digest }, body: budgetUsd === undefined ? {} : { budget_usd: budgetUsd } });
  }

  approveExperiment(experimentId: string): Promise<ExperimentRecord> {
    return this.call("approve_experiment", { path: { experiment_id: experimentId } });
  }

  async listRuns(experimentId?: string): Promise<RunRecord[]> {
    return (await this.call("list_runs", { query: { experiment_id: experimentId } })).runs;
  }

  getRun(runId: string): Promise<RunRecord> {
    return this.call("get_run", { path: { run_id: runId } });
  }

  async getRunSpec(runId: string): Promise<string> {
    return (await this.call("get_run_spec", { path: { run_id: runId } })).yaml;
  }

  controlRun(runId: string, command: string, value?: unknown): Promise<RunRecord> {
    return this.call("control_run", { path: { run_id: runId }, body: { command, value: value ?? null } });
  }

  // Reads the server sent event stream and hands each event to the listener until the stream ends.
  async runEvents(runId: string, listener: RunEventListener, follow = false, after = 0,
                  signal?: AbortSignal): Promise<void> {
    const response = await this.send("run_events", { path: { run_id: runId }, query: { follow, after }, signal });
    if (response.body === null) {
      return;
    }
    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffered = "";
    for (let chunk = await reader.read(); !chunk.done; chunk = await reader.read()) {
      buffered += decoder.decode(chunk.value, { stream: true });
      const frames = buffered.split("\n\n");
      buffered = frames.pop() ?? "";
      for (const frame of frames) {
        const event = parseEventFrame(frame);
        if (event !== null) {
          listener.onRunEvent(event);
        }
      }
    }
  }

  async listCatalog(kind: string): Promise<CatalogEntry[]> {
    return (await this.call("list_catalog", { path: { kind } })).entries;
  }

  async listEndpoints(): Promise<EndpointView[]> {
    return (await this.call("list_endpoints")).endpoints;
  }

  registerEndpoint(endpoint: EndpointRegistration): Promise<EndpointView> {
    return this.call("register_endpoint", { body: endpoint });
  }

  testEndpoint(name: string): Promise<ProbeResult> {
    return this.call("test_endpoint", { path: { name } });
  }

  async setEndpointKey(name: string, key: string): Promise<void> {
    await this.call("set_endpoint_key", { path: { name }, body: { key } });
  }
}

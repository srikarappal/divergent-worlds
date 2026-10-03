// Typed client for the public experiment API; every method is one operation from the shared manifest.
import { fillPath, operation } from "./operations";
import type {
  AdmissionStatus,
  AuditFilter,
  AuditRecord,
  AuthConfig,
  CalibrationReport,
  CatalogEntry,
  CohortEpisode,
  Comparison,
  DataPolicy,
  DryRun,
  EndpointRegistration,
  EndpointView,
  EpisodeFilter,
  EpisodeProvenance,
  EpisodeRecord,
  EvalReport,
  ExperimentPreflight,
  ExperimentRecord,
  FailureAnalysis,
  FiledReviewItem,
  FrameGrant,
  FrameSize,
  LiveTile,
  Principal,
  ProbeResult,
  RetentionReport,
  ReviewAction,
  ReviewItem,
  ReviewItemFilter,
  ReviewUpdate,
  RunEvent,
  RunRecord,
} from "./types";

export interface FetchTransport {
  fetch(input: string, init?: RequestInit): Promise<Response>;
}

export interface RunEventListener {
  onRunEvent(event: RunEvent): void;
}

// Where the signed in user's OIDC access token comes from; null before sign in or when sign in is off.
export interface TokenSource {
  accessToken(): string | null;
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
  dryRun(digest: string): Promise<DryRun>;
  launch(digest: string, budgetUsd?: number): Promise<ExperimentRecord>;
  getExperiment(experimentId: string): Promise<ExperimentRecord>;
  approveExperiment(experimentId: string): Promise<ExperimentRecord>;
  listRuns(experimentId?: string): Promise<RunRecord[]>;
  getRun(runId: string): Promise<RunRecord>;
  getRunSpec(runId: string): Promise<string>;
  controlRun(runId: string, command: string, value?: unknown): Promise<RunRecord>;
  runEvents(runId: string, listener: RunEventListener, follow?: boolean, after?: number,
            signal?: AbortSignal): Promise<void>;
  listEpisodes(runId: string, status?: "running" | "finished"): Promise<EpisodeRecord[]>;
  getEpisode(runId: string, episodeId: string, firstStep?: number): Promise<EpisodeRecord>;
  liveWall(runId: string, limit?: number): Promise<LiveTile[]>;
  getFrame(frameDigest: string, size?: FrameSize): Promise<Blob>;
  frameUrl(frameDigest: string, size: FrameSize): string;
  cohort(runId: string, filter?: EpisodeFilter): Promise<CohortEpisode[]>;
  getProvenance(runId: string, episodeId: string): Promise<EpisodeProvenance>;
  getStepPrompt(runId: string, episodeId: string, stepIndex: number): Promise<Record<string, unknown>[]>;
  reviewEpisode(runId: string, episodeId: string, action: ReviewAction, note?: string,
                label?: Record<string, unknown>): Promise<ReviewItem>;
  analyzeFailures(runId: string): Promise<FailureAnalysis>;
  getFailures(runId: string): Promise<FailureAnalysis>;
  runReport(runId: string): Promise<EvalReport>;
  compareRuns(runIds: readonly string[]): Promise<Comparison>;
  comparePolicyVersions(runId: string): Promise<Comparison>;
  listReviewItems(filter?: ReviewItemFilter): Promise<ReviewItem[]>;
  setFrameGrant(grant: string): void;
  authConfig(): Promise<AuthConfig>;
  me(): Promise<Principal>;
  frameGrant(): Promise<FrameGrant>;
  listExperiments(tenant?: string): Promise<ExperimentRecord[]>;
  fileReviewItem(item: FiledReviewItem): Promise<ReviewItem>;
  updateReviewItem(itemId: string, update: ReviewUpdate): Promise<ReviewItem>;
  calibration(runId?: string): Promise<CalibrationReport>;
  exportRun(runId: string, kind: "sft" | "preferences"): Promise<Record<string, unknown>[]>;
  listRoles(): Promise<Record<string, string[]>>;
  setRoles(subject: string, roles: readonly string[]): Promise<{ subject: string; roles: string[] }>;
  listDataPolicies(): Promise<DataPolicy[]>;
  setDataPolicy(tenant: string, policy: Omit<DataPolicy, "tenant">): Promise<DataPolicy>;
  auditLog(filter?: AuditFilter): Promise<AuditRecord[]>;
  poolStatus(): Promise<Record<string, number>>;
  sweepRetention(): Promise<RetentionReport>;
  fleetAdmission(): Promise<AdmissionStatus>;
  setTenantQuota(tenant: string, quota: number): Promise<AdmissionStatus>;
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

function requestInit(bodyKind: string | undefined, method: string, options: CallOptions,
                     token: string | null): RequestInit {
  const headers: Record<string, string> = token ? { Authorization: `Bearer ${token}` } : {};
  if (bodyKind === "yaml") {
    headers["Content-Type"] = "application/yaml";
    return { method, body: String(options.body ?? ""), headers, signal: options.signal };
  }
  if (bodyKind === "json") {
    headers["Content-Type"] = "application/json";
    return { method, body: JSON.stringify(options.body ?? {}), headers, signal: options.signal };
  }
  return { method, headers, signal: options.signal };
}

export class ApiClient implements ConsoleApi {
  private readonly baseUrl: string;
  private readonly transport: FetchTransport;
  private readonly tokens: TokenSource | null;
  private grant = "";

  constructor(baseUrl: string, transport: FetchTransport = window, tokens: TokenSource | null = null) {
    this.baseUrl = baseUrl.replace(/\/$/, "");
    this.transport = transport;
    this.tokens = tokens;
  }

  setFrameGrant(grant: string): void {
    this.grant = grant;
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
    const token = this.tokens?.accessToken() ?? null;
    const response = await this.transport.fetch(url, requestInit(listed.body, listed.method, options, token));
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

  dryRun(digest: string): Promise<DryRun> {
    return this.call("dry_run", { path: { digest } });
  }

  launch(digest: string, budgetUsd?: number): Promise<ExperimentRecord> {
    return this.call("launch", { path: { digest }, body: budgetUsd === undefined ? {} : { budget_usd: budgetUsd } });
  }

  getExperiment(experimentId: string): Promise<ExperimentRecord> {
    return this.call("get_experiment", { path: { experiment_id: experimentId } });
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

  async listEpisodes(runId: string, status?: "running" | "finished"): Promise<EpisodeRecord[]> {
    return (await this.call("list_episodes", { path: { run_id: runId }, query: { status } })).episodes;
  }

  getEpisode(runId: string, episodeId: string, firstStep = 0): Promise<EpisodeRecord> {
    return this.call("get_episode", { path: { run_id: runId, episode_id: episodeId },
                                      query: { first_step: firstStep } });
  }

  async liveWall(runId: string, limit = 24): Promise<LiveTile[]> {
    return (await this.call("live_wall", { path: { run_id: runId }, query: { limit } })).tiles;
  }

  async getFrame(frameDigest: string, size: FrameSize = "full"): Promise<Blob> {
    const response = await this.send("get_frame", { path: { frame_digest: frameDigest }, query: { size } });
    return response.blob();
  }

  // Frames go straight into img elements; content addressed, so browsers cache them for good.
  frameUrl(frameDigest: string, size: FrameSize): string {
    const grant = this.grant ? `&grant=${encodeURIComponent(this.grant)}` : "";
    const path = fillPath(operation("get_frame").path, { frame_digest: frameDigest });
    return `${this.baseUrl}${path}?size=${size}${grant}`;
  }

  authConfig(): Promise<AuthConfig> {
    return this.call("auth_config");
  }

  me(): Promise<Principal> {
    return this.call("me");
  }

  frameGrant(): Promise<FrameGrant> {
    return this.call("frame_grant");
  }

  async listExperiments(tenant?: string): Promise<ExperimentRecord[]> {
    return (await this.call("list_experiments", { query: { tenant } })).experiments;
  }

  fileReviewItem(item: FiledReviewItem): Promise<ReviewItem> {
    return this.call("file_review_item", { body: item });
  }

  updateReviewItem(itemId: string, update: ReviewUpdate): Promise<ReviewItem> {
    return this.call("update_review_item", { path: { item_id: itemId }, body: update });
  }

  calibration(runId?: string): Promise<CalibrationReport> {
    return this.call("calibration", { query: { run_id: runId } });
  }

  async exportRun(runId: string, kind: "sft" | "preferences"): Promise<Record<string, unknown>[]> {
    return (await this.call("export_run", { path: { run_id: runId, kind } })).rows;
  }

  async listRoles(): Promise<Record<string, string[]>> {
    return (await this.call("list_roles")).bindings;
  }

  setRoles(subject: string, roles: readonly string[]): Promise<{ subject: string; roles: string[] }> {
    return this.call("set_roles", { path: { subject }, body: { roles } });
  }

  async listDataPolicies(): Promise<DataPolicy[]> {
    return (await this.call("list_data_policies")).policies;
  }

  setDataPolicy(tenant: string, policy: Omit<DataPolicy, "tenant">): Promise<DataPolicy> {
    return this.call("set_data_policy", { path: { tenant }, body: policy });
  }

  async auditLog(filter: AuditFilter = {}): Promise<AuditRecord[]> {
    return (await this.call("audit_log", { query: filter })).records;
  }

  async poolStatus(): Promise<Record<string, number>> {
    return (await this.call("pool_status")).pools;
  }

  sweepRetention(): Promise<RetentionReport> {
    return this.call("sweep_retention");
  }

  fleetAdmission(): Promise<AdmissionStatus> {
    return this.call("fleet_admission");
  }

  setTenantQuota(tenant: string, quota: number): Promise<AdmissionStatus> {
    return this.call("set_tenant_quota", { path: { tenant }, body: { quota } });
  }

  async cohort(runId: string, filter: EpisodeFilter = {}): Promise<CohortEpisode[]> {
    return (await this.call("cohort", { path: { run_id: runId }, query: filter })).episodes;
  }

  getProvenance(runId: string, episodeId: string): Promise<EpisodeProvenance> {
    return this.call("get_provenance", { path: { run_id: runId, episode_id: episodeId } });
  }

  async getStepPrompt(runId: string, episodeId: string, stepIndex: number): Promise<Record<string, unknown>[]> {
    const path = { run_id: runId, episode_id: episodeId, step_index: String(stepIndex) };
    return (await this.call("get_step_prompt", { path })).messages;
  }

  reviewEpisode(runId: string, episodeId: string, action: ReviewAction, note = "",
                label?: Record<string, unknown>): Promise<ReviewItem> {
    return this.call("review_episode", { path: { run_id: runId, episode_id: episodeId },
                                         body: { action, note, label: label ?? null } });
  }

  analyzeFailures(runId: string): Promise<FailureAnalysis> {
    return this.call("analyze_failures", { path: { run_id: runId } });
  }

  getFailures(runId: string): Promise<FailureAnalysis> {
    return this.call("get_failures", { path: { run_id: runId } });
  }

  runReport(runId: string): Promise<EvalReport> {
    return this.call("run_report", { path: { run_id: runId } });
  }

  compareRuns(runIds: readonly string[]): Promise<Comparison> {
    return this.call("compare_runs", { query: { run_ids: runIds.join(",") } });
  }

  comparePolicyVersions(runId: string): Promise<Comparison> {
    return this.call("compare_policy_versions", { path: { run_id: runId } });
  }

  async listReviewItems(filter: ReviewItemFilter = {}): Promise<ReviewItem[]> {
    return (await this.call("list_review_items", { query: filter })).items;
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

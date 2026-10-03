// An in memory ConsoleApi for page tests: canned answers and a log of every call.
import { ApiError, type ConsoleApi, type RunEventListener } from "../api/client";
import type {
  CatalogEntry,
  DryRun,
  EndpointRegistration,
  EndpointView,
  EpisodeRecord,
  ExperimentPreflight,
  ExperimentRecord,
  FrameSize,
  LiveTile,
  ProbeResult,
  RunEvent,
  RunRecord,
} from "../api/types";

export function catalogEntry(kind: CatalogEntry["kind"], name: string,
                             extra: Partial<CatalogEntry> = {}): CatalogEntry {
  return {
    kind, name, version: "1", description: "", gate_status: null, clone_fidelity: null, pass_rates: {},
    details: {}, usage_runs: 0, ...extra,
  };
}

export function runRecord(runId: string, extra: Partial<RunRecord> = {}): RunRecord {
  return {
    run_id: runId, experiment_id: "experiment_000001", run_name: "point_000", spec_digest: "sha256:spec",
    run_digest: "sha256:run", run_type: "rl", assignments: {}, state: "running", desired_concurrency: 4000,
    priority: "training", job_id: null, created_at: 0, ...extra,
  };
}

export function experimentRecord(extra: Partial<ExperimentRecord> = {}): ExperimentRecord {
  return {
    experiment_id: "experiment_000001", spec_digest: "sha256:spec", name: "recon_difficulty_sweep",
    budget_usd: 2500, spent_usd: 0, estimated_cost_usd: 1200, run_ids: ["experiment_000001_run_000"],
    approved: true, ...extra,
  };
}

export function episodeRecord(episodeId: string, extra: Partial<EpisodeRecord> = {}): EpisodeRecord {
  return {
    episode_id: episodeId,
    key: { image_snapshot_digest: "sha256:img", world_digest: "sha256:world", task_digest: "sha256:task",
           run_id: "run_1", seed: 7, policy_version: 0 },
    task_id: "task_000", task_template: "tasks.finance.reconcile_invoices", difficulty_key: "easy",
    instruction: "Enter the reconciled invoice total in cell B2.", group_id: "b00000_g00000", sample_index: 0,
    run_type: "eval", final_state_ref: null, reward: null, human_intervened: false,
    started_at: "2026-10-03T10:00:00.000+00:00", finished_at: null, steps: [], ...extra,
  };
}

export class FakeApi implements ConsoleApi {
  readonly calls: Array<[string, unknown[]]> = [];
  catalogs: Record<string, CatalogEntry[]> = {};
  endpoints: EndpointView[] = [];
  runSpecs: Record<string, string> = {};
  runs: Record<string, RunRecord> = {};
  experiments: Record<string, ExperimentRecord> = {};
  events: RunEvent[] = [];
  episodeUpdates: EpisodeRecord[] = [];
  liveTiles: LiveTile[] = [];
  preflightReport: ExperimentPreflight = { is_valid: true, points: {}, estimated_cost_usd: null, budget_usd: 2500,
                                           errors: [] };
  saveFailure: ApiError | null = null;
  launchFailure: ApiError | null = null;
  digest = "sha256:abc123";

  record(name: string, ...args: unknown[]): void {
    this.calls.push([name, args]);
  }

  callsTo(name: string): unknown[][] {
    const matching: unknown[][] = [];
    for (const [called, args] of this.calls) {
      if (called === name) {
        matching.push(args);
      }
    }
    return matching;
  }

  async saveSpec(specYaml: string): Promise<string> {
    this.record("saveSpec", specYaml);
    if (this.saveFailure !== null) {
      throw this.saveFailure;
    }
    return this.digest;
  }

  async getSpec(digest: string): Promise<string> {
    this.record("getSpec", digest);
    return "";
  }

  async preflight(digest: string): Promise<ExperimentPreflight> {
    this.record("preflight", digest);
    return this.preflightReport;
  }

  async dryRun(digest: string): Promise<DryRun> {
    this.record("dryRun", digest);
    return { run_id: "dry_run_000001", episode_id: "dry_run_000001_s000_a0" };
  }

  async launch(digest: string, budgetUsd?: number): Promise<ExperimentRecord> {
    this.record("launch", digest, budgetUsd);
    if (this.launchFailure !== null) {
      throw this.launchFailure;
    }
    return experimentRecord({ spec_digest: digest, budget_usd: budgetUsd ?? 0 });
  }

  async getExperiment(experimentId: string): Promise<ExperimentRecord> {
    this.record("getExperiment", experimentId);
    return this.experiments[experimentId] ?? experimentRecord({ experiment_id: experimentId });
  }

  async approveExperiment(experimentId: string): Promise<ExperimentRecord> {
    this.record("approveExperiment", experimentId);
    const approved = { ...(await this.getExperiment(experimentId)), approved: true };
    this.experiments[experimentId] = approved;
    return approved;
  }

  async listRuns(experimentId?: string): Promise<RunRecord[]> {
    this.record("listRuns", experimentId);
    const runs: RunRecord[] = [];
    for (const run of Object.values(this.runs)) {
      if (experimentId === undefined || run.experiment_id === experimentId) {
        runs.push(run);
      }
    }
    return runs;
  }

  async getRun(runId: string): Promise<RunRecord> {
    this.record("getRun", runId);
    return this.runs[runId] ?? runRecord(runId);
  }

  async getRunSpec(runId: string): Promise<string> {
    this.record("getRunSpec", runId);
    const spec = this.runSpecs[runId];
    if (spec === undefined) {
      throw new ApiError(404, "not_found", runId, []);
    }
    return spec;
  }

  async controlRun(runId: string, command: string, value?: unknown): Promise<RunRecord> {
    this.record("controlRun", runId, command, value);
    const current = this.runs[runId] ?? runRecord(runId);
    const updated = command === "pause" ? { ...current, state: "paused" as const }
      : command === "set_concurrency" ? { ...current, desired_concurrency: Number(value) } : current;
    this.runs[runId] = updated;
    return updated;
  }

  async runEvents(runId: string, listener: RunEventListener, follow?: boolean, after?: number): Promise<void> {
    this.record("runEvents", runId, follow, after);
    for (const event of this.events) {
      listener.onRunEvent(event);
    }
  }

  async listEpisodes(runId: string, status?: "running" | "finished"): Promise<EpisodeRecord[]> {
    this.record("listEpisodes", runId, status);
    return [];
  }

  // Serves the queued updates in order, one per poll, then repeats the last one.
  async getEpisode(runId: string, episodeId: string, firstStep?: number): Promise<EpisodeRecord> {
    this.record("getEpisode", runId, episodeId, firstStep);
    const update = this.episodeUpdates.length > 1 ? this.episodeUpdates.shift() : this.episodeUpdates[0];
    if (update === undefined) {
      throw new ApiError(404, "not_found", episodeId, []);
    }
    return update;
  }

  async liveWall(runId: string, limit?: number): Promise<LiveTile[]> {
    this.record("liveWall", runId, limit);
    return this.liveTiles;
  }

  async getFrame(frameDigest: string, size?: FrameSize): Promise<Blob> {
    this.record("getFrame", frameDigest, size);
    return new Blob([]);
  }

  frameUrl(frameDigest: string, size: FrameSize): string {
    return `/frames/${frameDigest}?size=${size}`;
  }

  async listCatalog(kind: string): Promise<CatalogEntry[]> {
    this.record("listCatalog", kind);
    return this.catalogs[kind] ?? [];
  }

  async listEndpoints(): Promise<EndpointView[]> {
    this.record("listEndpoints");
    return this.endpoints;
  }

  async registerEndpoint(endpoint: EndpointRegistration): Promise<EndpointView> {
    this.record("registerEndpoint", endpoint);
    const view = { ...endpoint, max_concurrency: 64, has_key: false };
    this.endpoints = [...this.endpoints, view];
    return view;
  }

  async testEndpoint(name: string): Promise<ProbeResult> {
    this.record("testEndpoint", name);
    return { reachable: true, latency_seconds: 0.25, returns_token_ids: true, returns_logprobs: true,
             rate_limit_tokens_per_minute: null, provider_kind: "user", error: null };
  }

  async setEndpointKey(name: string, key: string): Promise<void> {
    this.record("setEndpointKey", name, key);
    const updated: EndpointView[] = [];
    for (const endpoint of this.endpoints) {
      updated.push(endpoint.name === name ? { ...endpoint, has_key: true } : endpoint);
    }
    this.endpoints = updated;
  }
}

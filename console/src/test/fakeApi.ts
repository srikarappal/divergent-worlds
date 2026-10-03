// An in memory ConsoleApi for page tests: canned answers and a log of every call.
import { ApiError, type ConsoleApi, type RunEventListener } from "../api/client";
import type {
  CatalogEntry,
  EndpointRegistration,
  EndpointView,
  ExperimentPreflight,
  ExperimentRecord,
  ProbeResult,
  RunRecord,
} from "../api/types";

export function catalogEntry(kind: CatalogEntry["kind"], name: string, extra: Partial<CatalogEntry> = {}): CatalogEntry {
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

export class FakeApi implements ConsoleApi {
  readonly calls: Array<[string, unknown[]]> = [];
  catalogs: Record<string, CatalogEntry[]> = {};
  endpoints: EndpointView[] = [];
  runSpecs: Record<string, string> = {};
  saveFailure: ApiError | null = null;
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
    return { is_valid: true, points: {}, estimated_cost_usd: null, budget_usd: 0, errors: [] };
  }

  async dryRun(digest: string): Promise<string> {
    this.record("dryRun", digest);
    return "dry_run_000001_s000_a0";
  }

  async launch(digest: string, budgetUsd?: number): Promise<ExperimentRecord> {
    this.record("launch", digest, budgetUsd);
    return { experiment_id: "experiment_000001", spec_digest: digest, name: "x", budget_usd: budgetUsd ?? 0,
             spent_usd: 0, estimated_cost_usd: null, run_ids: [], approved: true };
  }

  async approveExperiment(experimentId: string): Promise<ExperimentRecord> {
    this.record("approveExperiment", experimentId);
    return this.launch("sha256:spec");
  }

  async listRuns(experimentId?: string): Promise<RunRecord[]> {
    this.record("listRuns", experimentId);
    return [];
  }

  async getRun(runId: string): Promise<RunRecord> {
    this.record("getRun", runId);
    return runRecord(runId);
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
    return runRecord(runId);
  }

  async runEvents(runId: string, listener: RunEventListener, follow?: boolean, after?: number): Promise<void> {
    this.record("runEvents", runId, listener, follow, after);
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

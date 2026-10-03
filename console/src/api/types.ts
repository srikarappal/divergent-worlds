// Shapes of the experimentd API; they mirror the Python models in rlworld.services and rlworld.preflight.

export type RunState = "awaiting_approval" | "running" | "paused" | "stopped" | "completed" | "failed";

export interface RunRecord {
  run_id: string;
  experiment_id: string;
  run_name: string;
  spec_digest: string;
  run_digest: string;
  run_type: "rl" | "eval" | "data";
  assignments: Record<string, unknown>;
  state: RunState;
  desired_concurrency: number;
  priority: string;
  job_id: string | null;
  created_at: number;
}

export interface ExperimentRecord {
  experiment_id: string;
  spec_digest: string;
  name: string;
  budget_usd: number;
  spent_usd: number;
  estimated_cost_usd: number | null;
  run_ids: string[];
  approved: boolean;
}

export interface PreflightIssue {
  severity: "error" | "warning";
  code: string;
  message: string;
}

export interface PreflightReport {
  is_valid: boolean;
  issues: PreflightIssue[];
  run_digest: string | null;
  estimated_concurrent_envs: number;
  estimated_hosts_by_pool: Record<string, number>;
  estimated_policy_gpus: number;
  estimated_judge_tokens: number;
  estimated_cost_usd: number | null;
  estimated_wall_clock_hours: number;
  estimate: Record<string, unknown> | null;
}

export interface ExperimentPreflight {
  is_valid: boolean;
  points: Record<string, PreflightReport>;
  estimated_cost_usd: number | null;
  budget_usd: number;
  errors: string[];
}

export interface RunEvent {
  sequence: number;
  at: number;
  kind: string;
  detail: Record<string, unknown>;
}

export type CatalogKind = "images" | "worlds" | "task_templates" | "verifiers" | "rubrics" | "endpoints";

export interface CatalogEntry {
  kind: CatalogKind;
  name: string;
  version: string;
  description: string;
  gate_status: "passed" | "failed" | "review" | null;
  clone_fidelity: number | null;
  pass_rates: Record<string, number>;
  details: Record<string, unknown>;
  usage_runs: number;
}

export interface EndpointRegistration {
  name: string;
  base_url: string;
  allowed_models: string[];
  api_key_secret?: string | null;
  provider_kind: "frontier" | "self_hosted" | "user";
  returns_token_ids: boolean;
  accepts_screenshots: boolean;
}

export interface EndpointView extends EndpointRegistration {
  max_concurrency: number;
  has_key: boolean;
}

export interface ProbeResult {
  reachable: boolean;
  latency_seconds: number | null;
  returns_token_ids: boolean;
  returns_logprobs: boolean;
  rate_limit_tokens_per_minute: number | null;
  provider_kind: string;
  error: string | null;
}

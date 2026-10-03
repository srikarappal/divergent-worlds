// Shapes of the experimentd API; they mirror the Python models in rlworld.services and rlworld.preflight.

export type RunState = "awaiting_approval" | "running" | "paused" | "stopped" | "completed" | "failed";

export interface RunRecord {
  run_id: string;
  experiment_id: string;
  tenant: string;
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
  tenant: string;
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

export interface DryRun {
  run_id: string;
  episode_id: string;
}

export interface ScreenshotRef {
  uri: string;
  width: number;
  height: number;
  image_format: string;
  sha256: string;
}

export interface Observation {
  step_index: number;
  screenshot: ScreenshotRef | null;
  accessibility: unknown[] | null;
  dom: string | null;
  terminal: { stdout: string; stderr: string; exit_code: number } | null;
  file_changes: Array<{ path: string; change: string }> | null;
  user_reply: string | null;
  native_result: unknown;
}

export interface ModelTurn {
  prompt_ref: string | null;
  output_text: string;
  tool_calls: Record<string, unknown>[];
  served_model: string;
  input_tokens: number;
  output_tokens: number;
  cost_usd: number | null;
}

export interface StateChange {
  path: string;
  kind: "added" | "removed" | "modified";
  before: unknown;
  after: unknown;
}

export interface StepRecord {
  step_index: number;
  observation: Observation;
  model_turn: ModelTurn;
  actions: Record<string, unknown>[];
  parse_error: string | null;
  state_changes: StateChange[];
  timings: { model_seconds: number; env_seconds: number; settle_seconds: number };
  vm_events: string[];
}

export interface CheckResult {
  name: string;
  tier: "exact" | "semantic" | "judge";
  passed: boolean;
  weight: number;
  detail: string;
  expected: unknown;
  actual: unknown;
}

export interface Provenance {
  verifier_versions: Record<string, string>;
  judge_model: string | null;
  rubric_version: string | null;
  judge_skipped: boolean;
  judge_cached: boolean;
  state_hash: string;
  ground_truth_digest: string;
  reward_policy: Record<string, unknown>;
}

export interface RewardRecord {
  total: number;
  components: Record<string, number>;
  termination: "success" | "failure" | "max_steps" | "agent_error" | "env_error";
  counts_for_training: boolean;
  checks: CheckResult[];
  side_effect_violations: StateChange[];
  error_detail: string | null;
  provenance: Provenance;
}

export interface EpisodeRecord {
  episode_id: string;
  key: {
    image_snapshot_digest: string;
    world_digest: string;
    task_digest: string;
    run_id: string;
    seed: number;
    policy_version: number;
  };
  task_id: string;
  task_template: string;
  difficulty_key: string;
  instruction: string;
  group_id: string | null;
  sample_index: number;
  run_type: "rl" | "eval" | "data";
  final_state_ref: string | null;
  reward: RewardRecord | null;
  human_intervened: boolean;
  started_at: string;
  finished_at: string | null;
  steps: StepRecord[];
}

export interface LiveTile {
  episode_id: string;
  task_id: string;
  task_template: string;
  group_id: string | null;
  sample_index: number;
  step_index: number | null;
  frame: string | null;
  started_at: string;
}

export type FrameSize = "full" | "thumbnail";

export type EpisodeFilter = {
  termination?: string;
  task_id?: string;
  template?: string;
  difficulty_key?: string;
  category?: string;
  policy_version?: number;
  limit?: number;
};

export interface CohortEpisode {
  episode: EpisodeRecord;
  frames: Array<string | null>;
  category: string | null;
}

export interface EpisodeProvenance {
  episode_id: string;
  reward: RewardRecord | null;
  initial_state: Record<string, unknown>;
  final_state: Record<string, unknown> | null;
  world_diff: StateChange[];
}

export interface FailureLabel {
  category: string;
  summary: string;
  confidence: number;
  evidence_step: number | null;
  source: "rules" | "llm" | "fallback";
}

export interface FailureCluster {
  template: string;
  policy_version: number;
  category: string;
  episode_ids: string[];
  share_of_failures: number;
  example_summaries: string[];
}

export interface BugTicket {
  queue: string;
  template: string;
  policy_version: number;
  category: string;
  title: string;
  episode_ids: string[];
  evidence: string[];
}

export interface FailureAnalysis {
  run_id: string;
  analyzed_at: number;
  labels: Record<string, FailureLabel>;
  clusters: FailureCluster[];
  tickets: BugTicket[];
  unlabeled_episode_ids: string[];
}

export interface EvalBreakdown {
  episodes: number;
  successes: number;
  success_rate: number;
  interval_low: number;
  interval_high: number;
  mean_total: number;
  terminations: Record<string, number>;
}

export interface EvalReport {
  overall: EvalBreakdown;
  by_template: Record<string, EvalBreakdown>;
  by_difficulty: Record<string, EvalBreakdown>;
  pass_at_k: Record<string, number>;
  excluded_env_errors: number;
  excluded_human_intervened: number;
}

export interface ComparisonRow {
  task_id: string;
  template: string;
  episodes: Record<string, number>;
  success_rates: Record<string, number>;
}

export interface Comparison {
  reports: Record<string, EvalReport>;
  shared_tasks: ComparisonRow[];
}

export type ReviewAction = "flag" | "label" | "verifier_bug" | "task_draft" | "audit_sample";

export interface ReviewItem {
  item_id: string;
  action: ReviewAction | "bug_ticket" | "gate_failure" | "alert";
  queue: string;
  run_id: string;
  episode_ids: string[];
  title: string;
  note: string;
  label: Record<string, unknown> | null;
  payload: Record<string, unknown>;
  status: "open" | "resolved" | "dismissed";
  created_by: string;
  created_at: number;
  resolved_by: string;
  resolved_at: number | null;
  source: "console" | "failure_analysis" | "gates" | "alert";
}

export type ReviewItemFilter = {
  queue?: string;
  status?: string;
  run_id?: string;
  episode_id?: string;
};

export interface AuthConfig {
  enabled: boolean;
  issuer?: string;
  client_id?: string;
  audience?: string;
  scopes?: string[];
}

export interface Principal {
  subject: string;
  email: string;
  tenant: string;
  roles: string[];
}

export interface FrameGrant {
  grant: string;
  expires_at: number | null;
}

export interface ReviewUpdate {
  status?: "open" | "resolved" | "dismissed";
  note?: string;
  label?: Record<string, unknown>;
}

export interface FiledReviewItem {
  source: "gates" | "alert";
  title: string;
  queue?: string;
  run_id?: string;
  episode_ids?: string[];
  note?: string;
}

export interface CalibrationReport {
  verifier: { labels: number; precision: number | null; recall: number | null; false_passes: number;
              false_fails: number };
  judge: Record<string, { label_count: number; accuracy: number; kappa: number; false_passes: number;
                          false_fails: number }>;
  judge_problems: string[];
}

export interface DataPolicy {
  tenant: string;
  screenshot_endpoints: string[] | null;
  trajectory_days: number;
  full_frame_days: number;
  exports: Array<"sft" | "preferences">;
}

export interface AuditRecord {
  sequence: number;
  at: number;
  actor: string;
  tenant: string;
  action: string;
  target: Record<string, unknown>;
  detail: Record<string, unknown>;
}

export type AuditFilter = {
  actor?: string;
  action?: string;
  since?: number;
  limit?: number;
};

export interface AdmissionStatus {
  capacity: number;
  reserved: Record<string, number>;
  default_tenant_quota: number;
  tenant_quotas: Record<string, number>;
  endpoint_max_envs: Record<string, number>;
  usage: { total: number; by_class: Record<string, number>; by_tenant: Record<string, number>;
           by_endpoint: Record<string, number> };
}

export interface RetentionReport {
  deleted_episodes: string[];
  thumbnail_only_frames: number;
  deleted_blobs: number;
}

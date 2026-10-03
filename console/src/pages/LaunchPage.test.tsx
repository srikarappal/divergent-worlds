import { fireEvent, render, screen, within } from "@testing-library/react";
import { beforeEach, expect, test } from "vitest";

import { ApiError } from "../api/client";
import type { PreflightReport, RewardRecord, StepRecord } from "../api/types";
import { episodeRecord, FakeApi } from "../test/fakeApi";
import { LaunchPage } from "./LaunchPage";

const navigations: string[] = [];

function recordNavigation(to: string): void {
  navigations.push(to);
}

function resetNavigations(): void {
  navigations.length = 0;
}

beforeEach(resetNavigations);

function pointReport(extra: Partial<PreflightReport> = {}): PreflightReport {
  return {
    is_valid: true, issues: [], run_digest: "sha256:run", estimated_concurrent_envs: 4000,
    estimated_hosts_by_pool: { qemu_templated: 40 }, estimated_policy_gpus: 96, estimated_judge_tokens: 40_000_000,
    estimated_cost_usd: 1200, estimated_wall_clock_hours: 5.5, estimate: null, ...extra,
  };
}

function step(stepIndex: number, actions: Record<string, unknown>[]): StepRecord {
  return {
    step_index: stepIndex,
    observation: { step_index: stepIndex, screenshot: { uri: "blob:f", width: 1280, height: 800,
                                                         image_format: "webp", sha256: `frame${stepIndex}` },
                   accessibility: null, dom: null, terminal: null, file_changes: null, user_reply: null,
                   native_result: null },
    model_turn: { prompt_ref: null, output_text: `Action ${stepIndex}`, tool_calls: [], served_model: "my_policy",
                  input_tokens: 0, output_tokens: 0, cost_usd: null },
    actions, parse_error: null, state_changes: [],
    timings: { model_seconds: 1.5, env_seconds: 0.4, settle_seconds: 0.3 }, vm_events: [],
  };
}

function successReward(): RewardRecord {
  return {
    total: 1, components: { outcome: 1, side_effects: 0, efficiency: 0 }, termination: "success",
    counts_for_training: true, error_detail: null, side_effect_violations: [],
    checks: [{ name: "total", tier: "exact", passed: true, weight: 1, detail: "B2 matches", expected: 1250,
               actual: 1250 }],
    provenance: { verifier_versions: { "verifiers.excel.reconciliation_values_match": "2" },
                  judge_model: "frontier_judge_2026_06", rubric_version: "3+abc", judge_skipped: false,
                  judge_cached: true, state_hash: "sha256:state", ground_truth_digest: "sha256:truth",
                  reward_policy: {} },
  };
}

async function preflightShowsEstimatesAndTraps(): Promise<void> {
  const api = new FakeApi();
  api.preflightReport = {
    is_valid: true, budget_usd: 2500, estimated_cost_usd: 1200, errors: [],
    points: { point_000: pointReport({ issues: [{ severity: "warning", code: "frontier_policy_at_scale",
                                                  message: "a frontier policy at training scale" }] }) },
  };
  render(<LaunchPage api={api} digest="sha256:abc" navigate={recordNavigation} />);
  expect(await screen.findByText(/Preflight passed/))
    .toHaveTextContent("estimated 1,200 USD against a budget of 2,500 USD");
  const point = screen.getByLabelText("preflight point_000");
  expect(point).toHaveTextContent("4000");
  expect(point).toHaveTextContent("40 qemu_templated");
  expect(point).toHaveTextContent("frontier_policy_at_scale");
  expect(screen.getByLabelText("launch cli")).toHaveTextContent("rlworld specs launch sha256:abc --budget-usd 2500");
}

test("preflight shows the estimate and trap warnings per sweep point", preflightShowsEstimatesAndTraps);

async function blockedPreflightsCannotLaunch(): Promise<void> {
  const api = new FakeApi();
  api.preflightReport = { is_valid: false, budget_usd: 2500, estimated_cost_usd: null,
                          errors: ["point_000: missing_alternative_gate: tasks lack the alternative solution gate"],
                          points: { point_000: pointReport({ is_valid: false }) } };
  render(<LaunchPage api={api} digest="sha256:abc" navigate={recordNavigation} />);
  expect(await screen.findByLabelText("Preflight problems")).toHaveTextContent("missing_alternative_gate");
  expect(screen.getByRole("button", { name: "Launch" })).toBeDisabled();
}

test("a blocked preflight keeps launch disabled", blockedPreflightsCannotLaunch);

async function dryRunsStreamAnEpisodeWithItsReward(): Promise<void> {
  const api = new FakeApi();
  api.episodeUpdates = [
    episodeRecord("dry_run_000001_s000_a0", { steps: [step(0, [{ name: "type", text: "1250" }])] }),
    episodeRecord("dry_run_000001_s000_a0", { steps: [step(1, [{ name: "done" }])], reward: successReward(),
                                             finished_at: "2026-10-03T10:01:00.000+00:00" }),
  ];
  render(<LaunchPage api={api} digest="sha256:abc" navigate={recordNavigation} pollMilliseconds={5} />);
  fireEvent.click(await screen.findByRole("button", { name: "Dry run one episode" }));
  const reward = await screen.findByLabelText("reward breakdown");
  expect(reward).toHaveTextContent("Reward 1.00: success");
  expect(reward).toHaveTextContent("frontier_judge_2026_06, rubric 3+abc, cached");
  expect(within(reward).getByLabelText("checks")).toHaveTextContent("B2 matches");
  const steps = screen.getByLabelText("steps");
  expect(steps).toHaveTextContent("type(text=1250)");
  expect(steps).toHaveTextContent("done");
  expect(screen.getByAltText("latest frame")).toHaveAttribute("src", "/frames/frame1?size=full");
  expect(api.callsTo("getEpisode")[1]).toEqual(["dry_run_000001", "dry_run_000001_s000_a0", 1]);
}

test("a dry run streams one episode live with its reward breakdown", dryRunsStreamAnEpisodeWithItsReward);

async function launchingOpensTheExperiment(): Promise<void> {
  const api = new FakeApi();
  render(<LaunchPage api={api} digest="sha256:abc" navigate={recordNavigation} />);
  const budget = await screen.findByLabelText("Budget (USD)");
  fireEvent.change(budget, { target: { value: "800" } });
  fireEvent.click(screen.getByRole("button", { name: "Launch" }));
  await screen.findByRole("button", { name: "Launch" });
  expect(api.callsTo("launch")[0]).toEqual(["sha256:abc", 800]);
  expect(navigations).toEqual(["/experiments/experiment_000001"]);
}

test("launching commits the budget and opens the experiment", launchingOpensTheExperiment);

async function rejectedLaunchesExplainWhy(): Promise<void> {
  const api = new FakeApi();
  api.launchFailure = new ApiError(422, "launch_blocked", "blocked",
                                   ["estimated cost 3,000.00 USD exceeds 800.00 USD"]);
  render(<LaunchPage api={api} digest="sha256:abc" navigate={recordNavigation} />);
  fireEvent.click(await screen.findByRole("button", { name: "Launch" }));
  expect(await screen.findByLabelText("Launch problems")).toHaveTextContent("exceeds 800.00 USD");
  expect(navigations).toEqual([]);
}

test("a rejected launch explains why", rejectedLaunchesExplainWhy);

import { fireEvent, render, screen, within } from "@testing-library/react";
import type { ReactElement } from "react";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, expect, test } from "vitest";

import type { EvalBreakdown, EvalReport, RewardRecord, StepRecord } from "../api/types";
import { episodeRecord, FakeApi } from "../test/fakeApi";
import {
  actionMarkers,
  CohortPage,
  ComparePage,
  EpisodeReviewPage,
  episodeFilter,
  RunReviewPage,
} from "./ReviewPages";

const RUN_ID = "experiment_000001_run_000";
const navigations: Array<[string, unknown]> = [];

function recordNavigation(to: string, options?: { replace?: boolean }): void {
  navigations.push([to, options]);
}

function resetNavigations(): void {
  navigations.length = 0;
}

beforeEach(resetNavigations);

function inRouter(page: ReactElement): ReactElement {
  return <MemoryRouter>{page}</MemoryRouter>;
}

function step(stepIndex: number, actions: Record<string, unknown>[], outputText: string): StepRecord {
  return {
    step_index: stepIndex,
    observation: { step_index: stepIndex, screenshot: { uri: "blob:f", width: 1280, height: 800,
                                                         image_format: "webp", sha256: `frame${stepIndex}` },
                   accessibility: null, dom: null, terminal: null, file_changes: null, user_reply: null,
                   native_result: null },
    model_turn: { prompt_ref: "sha256:p", output_text: outputText, tool_calls: [], served_model: "my_policy",
                  input_tokens: 0, output_tokens: 0, cost_usd: null },
    actions, parse_error: null, state_changes: [],
    timings: { model_seconds: 1.5, env_seconds: 0.4, settle_seconds: 0.3 }, vm_events: [],
  };
}

function failedReward(): RewardRecord {
  return {
    total: 0, components: { outcome: 0 }, termination: "failure", counts_for_training: true, error_detail: null,
    side_effect_violations: [],
    checks: [
      { name: "total", tier: "exact", passed: false, weight: 1, detail: "B2 holds 1200, expected 1250",
        expected: 1250, actual: 1200 },
      { name: "summary_tone", tier: "judge", passed: true, weight: 1, detail: "polite and complete", expected: null,
        actual: null },
    ],
    provenance: { verifier_versions: {}, judge_model: "frontier_judge_2026_06", rubric_version: "3+abc",
                  judge_skipped: false, judge_cached: false, state_hash: "sha256:state",
                  ground_truth_digest: "sha256:truth", reward_policy: {} },
  };
}

function reviewApi(): FakeApi {
  const api = new FakeApi();
  api.episodeUpdates = [episodeRecord("episode_b", {
    reward: failedReward(), finished_at: "2026-10-03T10:01:00.000+00:00",
    steps: [step(0, [{ name: "click", x: 640, y: 400, button: "left" }], "Action: click(start_box='(500,500)')"),
            step(1, [{ name: "type", text: "1200" }], "Action: type(content='1200')"),
            step(2, [{ name: "done" }], "Action: finished()")],
  })];
  api.provenance = {
    episode_id: "episode_b", reward: failedReward(), initial_state: { cells: { B2: null } },
    final_state: { cells: { B2: 1200 } },
    world_diff: [{ path: "files/reconciliation.xlsx/cells/B2/value", kind: "modified", before: null, after: 1200 }],
  };
  api.prompts[1] = [{ role: "user", content: "Step 1." }];
  api.runSpecs[RUN_ID] = "experiment: {}\n";
  return api;
}

function reviewPage(api: FakeApi, initialStep = 0): ReactElement {
  return inRouter(<EpisodeReviewPage api={api} runId={RUN_ID} episodeId="episode_b" initialStep={initialStep}
                                     navigate={recordNavigation} />);
}

async function theScrubberShowsActionsOnEachFrame(): Promise<void> {
  const api = reviewApi();
  render(reviewPage(api, 1));
  expect(await screen.findByAltText("step 1 frame")).toHaveAttribute("src", "/frames/frame1?size=full");
  expect(screen.getByLabelText("step 1")).toHaveTextContent("type(text=1200)");
  fireEvent.change(screen.getByLabelText("step"), { target: { value: "0" } });
  const marker = screen.getByTitle("click");
  expect(marker).toHaveStyle({ left: "50%", top: "50%" });
  expect(navigations).toEqual([["?step=0", { replace: true }]]);
  fireEvent.click(screen.getByRole("button", { name: "Next" }));
  fireEvent.click(screen.getByRole("button", { name: "Show model input" }));
  expect(await screen.findByLabelText("model input")).toHaveTextContent('"content": "Step 1."');
}

test("the scrubber overlays actions on each frame and shows the model input", theScrubberShowsActionsOnEachFrame);

async function provenanceDrillsFromScoreToState(): Promise<void> {
  render(reviewPage(reviewApi()));
  const reward = await screen.findByLabelText("reward breakdown");
  expect(within(reward).getByLabelText("checks")).toHaveTextContent("B2 holds 1200, expected 1250");
  expect(within(reward).getByLabelText("checks")).toHaveTextContent("polite and complete");
  const provenance = screen.getByLabelText("reward provenance");
  expect(within(provenance).getByLabelText("world diff")).toHaveTextContent("files/reconciliation.xlsx/cells/B2/value");
  expect(provenance).toHaveTextContent('"B2": 1200');
}

test("reward provenance drills from the score to the checks, judge rationale and world diff",
     provenanceDrillsFromScoreToState);

async function oneClickActionsFileReviewItems(): Promise<void> {
  const api = reviewApi();
  render(reviewPage(api));
  await screen.findByLabelText("review actions");
  fireEvent.change(screen.getByLabelText("Note"), { target: { value: "cursor jumped" } });
  fireEvent.click(screen.getByRole("button", { name: "Flag" }));
  expect(await screen.findByText("filed in the review queue")).toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "Save label" }));
  expect(await screen.findByLabelText("Episode problems")).toHaveTextContent("a label needs a verdict");
  fireEvent.change(screen.getByLabelText("Verdict"), { target: { value: "success" } });
  fireEvent.change(screen.getByLabelText("Failure category"), { target: { value: "verifier_false_negative" } });
  fireEvent.click(screen.getByRole("button", { name: "Save label" }));
  expect(await screen.findByText("filed in the labeling queue")).toBeInTheDocument();
  expect(api.callsTo("reviewEpisode")[2]).toEqual([RUN_ID, "episode_b", "label", "",
                                                   { verdict: "success", category: "verifier_false_negative" }]);
  expect(screen.getByLabelText("review items")).toHaveTextContent("review: flag episode_b, cursor jumped (open)");
}

test("one click actions file review items", oneClickActionsFileReviewItems);

async function reproducingRerunsOrClones(): Promise<void> {
  const api = reviewApi();
  render(reviewPage(api));
  fireEvent.click(await screen.findByRole("button", { name: "Rerun the same spec" }));
  await screen.findByRole("button", { name: "Clone and edit" });
  fireEvent.click(screen.getByRole("button", { name: "Clone and edit" }));
  expect(api.callsTo("saveSpec")).toEqual([["experiment: {}\n"]]);
  expect(navigations).toEqual([["/launch/sha256:abc123", undefined], [`/configure?clone=${RUN_ID}`, undefined]]);
}

test("reproduce reruns the same spec or clones it for editing", reproducingRerunsOrClones);

async function cohortsScrubSideBySide(): Promise<void> {
  const api = new FakeApi();
  api.cohortMembers = [
    { episode: episodeRecord("episode_b", { reward: failedReward() }), frames: ["b0", "b1", "b2"],
      category: "grounding" },
    { episode: episodeRecord("episode_c", { reward: failedReward() }), frames: ["c0"], category: null },
  ];
  render(inRouter(<CohortPage api={api} runId={RUN_ID} initialFilter={{ termination: "failure" }} />));
  expect(await screen.findByAltText("episode_b at step 0")).toHaveAttribute("src", "/frames/b0?size=thumbnail");
  fireEvent.change(screen.getByLabelText("cohort step"), { target: { value: "2" } });
  expect(screen.getByAltText("episode_b at step 2")).toHaveAttribute("src", "/frames/b2?size=thumbnail");
  expect(screen.getByAltText("episode_c at step 0").closest("a"))
    .toHaveAttribute("href", `/runs/${RUN_ID}/episodes/episode_c/review?step=0`);
  fireEvent.change(screen.getByLabelText("Policy version"), { target: { value: "4" } });
  fireEvent.click(screen.getByRole("button", { name: "Apply" }));
  await screen.findByText(/2 episodes/);
  expect(api.callsTo("cohort")[1]).toEqual([RUN_ID, { termination: "failure", policy_version: 4 }]);
}

test("cohorts scrub side by side and filter by any field", cohortsScrubSideBySide);

function breakdown(successes: number, episodes: number): EvalBreakdown {
  return { episodes, successes, success_rate: successes / episodes, interval_low: 0.1, interval_high: 0.9,
           mean_total: successes / episodes, terminations: {} };
}

function report(): EvalReport {
  return { overall: breakdown(1, 4), by_template: { "tasks.finance.reconcile_invoices": breakdown(1, 4) },
           by_difficulty: { easy: breakdown(1, 4) }, pass_at_k: { 1: 0.25 }, excluded_env_errors: 1,
           excluded_human_intervened: 0 };
}

async function runReviewsShowReportsAndClusters(): Promise<void> {
  const api = new FakeApi();
  api.report = report();
  api.comparison = {
    reports: { policy_version_3: report(), policy_version_4: report() },
    shared_tasks: [{ task_id: "task_000", template: "tasks.finance.reconcile_invoices",
                     episodes: { policy_version_3: 2, policy_version_4: 1 },
                     success_rates: { policy_version_3: 0.5, policy_version_4: 0 } }],
  };
  render(inRouter(<RunReviewPage api={api} runId={RUN_ID} />));
  expect(await screen.findByLabelText("eval report")).toHaveTextContent("Overall 25% [10%, 90%] of 4; pass@1 25%");
  expect(screen.getByText("Not analyzed yet.")).toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "Analyze failures" }));
  const cluster = await screen.findByRole("link", { name: "grounding" });
  expect(cluster.getAttribute("href")).toBe(
    `/runs/${RUN_ID}/cohort?category=grounding&template=tasks.finance.reconcile_invoices&policy_version=3`);
  expect(screen.getByLabelText("shared tasks")).toHaveTextContent("50% of 2");
}

test("a run review shows the report, failure clusters and policy versions", runReviewsShowReportsAndClusters);

async function runsCompareOnSharedTasks(): Promise<void> {
  const api = new FakeApi();
  api.comparison = {
    reports: { run_a: report(), run_b: report() },
    shared_tasks: [{ task_id: "task_000", template: "tasks.finance.reconcile_invoices",
                     episodes: { run_a: 3, run_b: 2 }, success_rates: { run_a: 1 / 3, run_b: 0 } }],
  };
  render(inRouter(<ComparePage api={api} runIds={["run_a", "run_b"]} />));
  const shared = await screen.findByLabelText("shared tasks");
  expect(shared).toHaveTextContent("33% of 3");
  expect(shared).toHaveTextContent("0% of 2");
  expect(screen.getByLabelText("compare_runs cli")).toHaveTextContent("rlworld compare runs run_a run_b");
}

test("runs compare on the tasks both attempted", runsCompareOnSharedTasks);

async function aCloneRunMeasuresFidelityAgainstTheRealApp(): Promise<void> {
  const api = new FakeApi();
  render(inRouter(<ComparePage api={api} runIds={["run_clone", "run_real"]} />));
  const measure = await screen.findByRole("button", { name: "Measure clone fidelity" });
  expect(measure).toBeDisabled();
  fireEvent.change(screen.getByLabelText("app clone"), { target: { value: "crm" } });
  fireEvent.click(measure);
  expect(await screen.findByText("run_clone and run_real share no task")).toBeInTheDocument();
  api.fidelity = { app: "crm", clone_run_id: "run_clone", real_run_id: "run_real", tasks: 40,
                   clone_success_rate: 0.7, real_success_rate: 0.45, gap: 0.25, fidelity: 0.75, agreement: 0.7,
                   clone_only: 11, real_only: 1, p_value: 0.0063, large_gap: true };
  fireEvent.click(measure);
  const table = await screen.findByLabelText("clone fidelity");
  expect(table).toHaveTextContent("Gap25%");
  expect(table).toHaveTextContent("Fidelity75%");
  expect(table).toHaveTextContent("11, 1");
  expect(table).toHaveTextContent("Large gap: training on the clone will not transfer");
  expect(api.callsTo("cloneFidelity")).toEqual([["crm", "run_clone", "run_real"], ["crm", "run_clone", "run_real"]]);
  expect(screen.getByLabelText("clone_fidelity cli"))
    .toHaveTextContent("rlworld compare fidelity crm run_clone run_real");
}

test("a clone run measures its fidelity against the real app run", aCloneRunMeasuresFidelityAgainstTheRealApp);

function filtersAndMarkersAreTyped(): void {
  expect(episodeFilter({ termination: "failure", policy_version: "3", limit: "", bogus: "x" }))
    .toEqual({ termination: "failure", policy_version: 3 });
  const drag = step(0, [{ name: "drag", path: [{ x: 0, y: 0 }, { x: 1280, y: 800 }] }], "");
  expect(actionMarkers(drag).map(markerPosition)).toEqual(["0%,0%", "100%,100%"]);
}

function markerPosition(marker: { left: string; top: string }): string {
  return `${marker.left},${marker.top}`;
}

test("cohort filters are typed and drag paths become markers", filtersAndMarkersAreTyped);

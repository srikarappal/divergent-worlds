import { fireEvent, render, screen } from "@testing-library/react";
import type { ReactElement } from "react";
import { MemoryRouter } from "react-router-dom";
import { expect, test } from "vitest";

import type { RunEvent } from "../api/types";
import { alertRecord, experimentRecord, FakeApi, runRecord } from "../test/fakeApi";
import { episodesPerSecond, RunDashboardPage, sparklinePoints } from "./RunDashboardPage";
import { ExperimentPage, LiveWallPage, RunsPage } from "./RunPages";

const RUN_ID = "experiment_000001_run_000";

function inRouter(page: ReactElement): ReactElement {
  return <MemoryRouter>{page}</MemoryRouter>;
}

function metricsEvent(sequence: number, at: number, detail: Record<string, unknown>): RunEvent {
  return { sequence, at, kind: "metrics", detail };
}

function apiWithRun(): FakeApi {
  const api = new FakeApi();
  api.runs[RUN_ID] = runRecord(RUN_ID, { run_name: "point_000", run_type: "eval",
                                         assignments: { "task.difficulty.distractors": 5 } });
  api.experiments.experiment_000001 = experimentRecord({ budget_usd: 2500, spent_usd: 100 });
  return api;
}

async function runsListLinksToDashboards(): Promise<void> {
  render(inRouter(<RunsPage api={apiWithRun()} />));
  const link = await screen.findByRole("link", { name: RUN_ID });
  expect(link).toHaveAttribute("href", `/runs/${RUN_ID}`);
  expect(screen.getByLabelText("runs")).toHaveTextContent('task.difficulty.distractors=5');
}

test("the runs list links every run to its dashboard", runsListLinksToDashboards);

async function experimentsWaitForApproval(): Promise<void> {
  const api = apiWithRun();
  api.experiments.experiment_000001 = experimentRecord({ approved: false, estimated_cost_usd: null });
  render(inRouter(<ExperimentPage api={api} experimentId="experiment_000001" />));
  expect(await screen.findByText(/Waiting for approval/)).toHaveTextContent("estimated unknown");
  fireEvent.click(screen.getByRole("button", { name: "Approve launch" }));
  expect(await screen.findByText(/Approved\./)).toBeInTheDocument();
  expect(api.callsTo("approveExperiment")).toEqual([["experiment_000001"]]);
  expect(screen.getByRole("link", { name: RUN_ID })).toBeInTheDocument();
}

test("an experiment above the threshold waits for approval", experimentsWaitForApproval);

async function theDashboardFollowsTheEventStream(): Promise<void> {
  const api = apiWithRun();
  api.events = [
    { sequence: 1, at: 100, kind: "launched", detail: { job_id: null } },
    metricsEvent(2, 110, { episodes: 40, success_rate: 0.25, mean_reward: 0.25, env_error_rate: 0.025,
                           mean_model_seconds: 2.5, mean_env_seconds: 0.8, mean_settle_seconds: 0.4,
                           rate_limit_wait_seconds: 3 }),
    { sequence: 3, at: 111, kind: "spend", detail: { amount_usd: 50, experiment_spent_usd: 1250 } },
    metricsEvent(4, 120, { episodes: 60, success_rate: 0.5, mean_reward: 0.5, env_error_rate: 0.01,
                           trainer_gpu_utilization: 0.92, tracker_url: "https://wandb.example/run/1" }),
  ];
  render(inRouter(<RunDashboardPage api={api} runId={RUN_ID} />));
  const metrics = await screen.findByLabelText("run metrics");
  expect(await screen.findByText("2.00")).toBeInTheDocument();
  expect(metrics).toHaveTextContent("Success rate50%");
  expect(metrics).toHaveTextContent("Trainer GPUs busy92%");
  expect(screen.getByRole("link", { name: "Training curves in the experiment tracker" }))
    .toHaveAttribute("href", "https://wandb.example/run/1");
  expect(screen.getByLabelText("budget")).toHaveTextContent("Spent 1,250 USD of 2,500 USD");
  expect(screen.getByRole("img", { name: "Success rate over time" })).toBeInTheDocument();
  expect(screen.getByLabelText("events")).toHaveTextContent("spend");
  expect(screen.getByRole("link", { name: "Live wall" })).toHaveAttribute("href", `/runs/${RUN_ID}/live`);
}

test("the dashboard follows the event stream", theDashboardFollowsTheEventStream);

async function liveControlsChangeOnlyOperationalKnobs(): Promise<void> {
  const api = apiWithRun();
  render(inRouter(<RunDashboardPage api={api} runId={RUN_ID} />));
  fireEvent.click(await screen.findByRole("button", { name: "Pause" }));
  expect(await screen.findByRole("button", { name: "Resume" })).toBeEnabled();
  fireEvent.change(screen.getByLabelText("Concurrency"), { target: { value: "64" } });
  fireEvent.click(screen.getByRole("button", { name: "Set concurrency" }));
  fireEvent.change(screen.getByLabelText("Priority"), { target: { value: "eval" } });
  fireEvent.change(screen.getByLabelText("Extend budget (USD)"), { target: { value: "500" } });
  fireEvent.click(screen.getByRole("button", { name: "Extend budget" }));
  await screen.findByText(/concurrency 64/);
  const commands = [];
  for (const call of api.callsTo("controlRun")) {
    commands.push(call.slice(1));
  }
  expect(commands).toEqual([["pause", undefined], ["set_concurrency", 64], ["set_priority", "eval"],
                            ["extend_budget", 500]]);
}

test("live controls change only operational knobs", liveControlsChangeOnlyOperationalKnobs);

async function theDashboardShowsTheRunsAlerts(): Promise<void> {
  const api = apiWithRun();
  api.alerts = [alertRecord("alert_000001"), alertRecord("alert_000009", { run_id: "experiment_000001_run_001" })];
  api.events = [{ sequence: 1, at: 100, kind: "alert", detail: { alert_id: "alert_000001", kind: "reward_spike" } }];
  render(inRouter(<RunDashboardPage api={api} runId={RUN_ID} />));
  const alerts = await screen.findByLabelText("run alerts");
  expect(await screen.findByText("reward on task_000 jumped from 0.10 to 0.90")).toBeInTheDocument();
  expect(alerts.querySelectorAll("tbody tr")).toHaveLength(1);
  fireEvent.click(screen.getByRole("button", { name: "Acknowledge" }));
  expect(await screen.findByText("acknowledged by developer")).toBeInTheDocument();
  expect(api.callsTo("acknowledgeAlert")).toEqual([["alert_000001"]]);
  expect(api.callsTo("listAlerts").length).toBeGreaterThanOrEqual(3);
  expect(api.callsTo("listAlerts")[0]).toEqual([{ run_id: RUN_ID }]);
}

test("the dashboard shows the run's alerts and reloads them on alert events", theDashboardShowsTheRunsAlerts);

async function theLiveWallShowsThumbnails(): Promise<void> {
  const api = apiWithRun();
  api.liveTiles = [
    { episode_id: "b00000_g00000_s000_a0", task_id: "task_000", task_template: "tasks.finance.reconcile_invoices",
      group_id: "b00000_g00000", sample_index: 0, step_index: 4, frame: "ab12", started_at: "2026-10-03" },
    { episode_id: "b00000_g00000_s001_a0", task_id: "task_000", task_template: "tasks.finance.reconcile_invoices",
      group_id: "b00000_g00000", sample_index: 1, step_index: null, frame: null, started_at: "2026-10-03" },
  ];
  render(inRouter(<LiveWallPage api={api} runId={RUN_ID} pollMilliseconds={5} />));
  const frame = await screen.findByAltText("b00000_g00000_s000_a0 frame");
  expect(frame).toHaveAttribute("src", "/frames/ab12?size=thumbnail");
  expect(frame.closest("a")).toHaveAttribute("href", `/runs/${RUN_ID}/episodes/b00000_g00000_s000_a0`);
  expect(screen.getByText("starting")).toBeInTheDocument();
  expect(screen.getByLabelText("live_wall cli")).toHaveTextContent(`rlworld runs live ${RUN_ID} --limit 24`);
}

test("the live wall shows sampled thumbnails that open the episode", theLiveWallShowsThumbnails);

function rateAndSparklineMath(): void {
  expect(episodesPerSecond([metricsEvent(1, 10, { episodes: 10 }), metricsEvent(2, 15, { episodes: 30 })])).toBe(4);
  expect(episodesPerSecond([metricsEvent(1, 10, { episodes: 10 })])).toBeNull();
  expect(sparklinePoints([0, 1], 100, 10)).toBe("0.0,10.0 100.0,0.0");
  expect(sparklinePoints([], 100, 10)).toBe("");
}

test("episode rates and sparklines come from the metric series", rateAndSparklineMath);

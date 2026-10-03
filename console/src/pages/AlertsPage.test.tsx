import { fireEvent, render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { expect, test } from "vitest";

import { alertRecord, FakeApi } from "../test/fakeApi";
import { AlertsPage } from "./AlertsPage";

function apiWithAlerts(): FakeApi {
  const api = new FakeApi();
  api.alerts = [
    alertRecord("alert_000001", { raised_at: 10 }),
    alertRecord("alert_000002", { kind: "budget_threshold", severity: "critical", run_id: "", review_item_id: null,
                                  title: "spend reached 100% of the $2,500.00 budget", raised_at: 20 }),
    alertRecord("alert_000003", { kind: "run_completion", severity: "info", status: "acknowledged",
                                  acknowledged_by: "lead", review_item_id: null, title: "run point_000 completed" }),
  ];
  return api;
}

async function openAlertsComeNewestFirst(): Promise<void> {
  render(<MemoryRouter><AlertsPage api={apiWithAlerts()} /></MemoryRouter>);
  const table = await screen.findByRole("table", { name: "alerts" });
  const rows = table.querySelectorAll("tbody tr");
  expect(rows).toHaveLength(2);
  expect(rows[0]).toHaveTextContent("criticalbudget thresholdspend reached 100%");
  expect(rows[1]).toHaveTextContent("1 episodes in the review queue");
  expect(screen.getByRole("link", { name: "experiment_000001" })).toHaveAttribute("href",
                                                                                 "/experiments/experiment_000001");
  expect(screen.getByRole("link", { name: "experiment_000001_run_000" }))
    .toHaveAttribute("href", "/runs/experiment_000001_run_000");
  expect(screen.getByLabelText("acknowledge_alert cli")).toHaveTextContent("rlworld alerts ack <alert_id>");
}

test("open alerts come newest first with their scope and review items", openAlertsComeNewestFirst);

async function acknowledgingRemovesAnAlertFromTheOpenList(): Promise<void> {
  const api = apiWithAlerts();
  render(<MemoryRouter><AlertsPage api={api} /></MemoryRouter>);
  const buttons = await screen.findAllByRole("button", { name: "Acknowledge" });
  fireEvent.click(buttons[0]);
  await screen.findByText("1 episodes in the review queue");
  expect(api.callsTo("acknowledgeAlert")).toEqual([["alert_000002"]]);
  expect(screen.getAllByRole("button", { name: "Acknowledge" })).toHaveLength(1);
  fireEvent.change(screen.getByLabelText("Status"), { target: { value: "acknowledged" } });
  expect(await screen.findByText("acknowledged by lead")).toBeInTheDocument();
  expect(screen.getByText("acknowledged by developer")).toBeInTheDocument();
}

test("acknowledging removes an alert from the open list", acknowledgingRemovesAnAlertFromTheOpenList);

import { fireEvent, render, screen, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { expect, test } from "vitest";

import { ConsoleRoutes } from "../App";
import { catalogEntry, FakeApi } from "../test/fakeApi";
import { CatalogsPage } from "./CatalogsPage";

async function catalogTabsListEntries(): Promise<void> {
  const api = new FakeApi();
  api.catalogs.images = [catalogEntry("images", "win2025_office_v4",
                                      { version: "sha256:4f9c", clone_fidelity: 0.93, usage_runs: 6 })];
  api.catalogs.task_templates = [catalogEntry("task_templates", "tasks.finance.reconcile_invoices",
                                              { gate_status: "passed", pass_rates: { "my_policy@3": 0.42 } })];
  render(<CatalogsPage api={api} />);
  const images = screen.getByRole("table", { name: "images catalog" });
  expect(await within(images).findByText("win2025_office_v4")).toBeInTheDocument();
  expect(images).toHaveTextContent("93%");
  fireEvent.click(screen.getByRole("button", { name: "Task templates" }));
  expect(await screen.findByText("my_policy@3 42%")).toBeInTheDocument();
  expect(screen.getByText("passed")).toBeInTheDocument();
  expect(screen.getByLabelText("list_catalog cli")).toHaveTextContent("rlworld catalogs list task_templates");
  fireEvent.click(screen.getByRole("button", { name: "Worlds" }));
  expect(await screen.findByText("Nothing in this catalog yet.")).toBeInTheDocument();
}

test("catalog tabs list entries with gates, fidelity, pass rates and usage", catalogTabsListEntries);

async function endpointKeysAreWriteOnly(): Promise<void> {
  const api = new FakeApi();
  render(<CatalogsPage api={api} />);
  fireEvent.click(screen.getByRole("button", { name: "Endpoints" }));
  await screen.findByRole("heading", { name: "Endpoint registry" });
  fireEvent.change(screen.getByLabelText("Name"), { target: { value: "byo_judge" } });
  fireEvent.change(screen.getByLabelText("Base URL"), { target: { value: "https://judge.customer.example/v1" } });
  fireEvent.change(screen.getByLabelText("Pinned models"), { target: { value: "judge_v1, judge_v2" } });
  fireEvent.change(screen.getByLabelText("Key secret name"), { target: { value: "byo_judge_key" } });
  fireEvent.click(screen.getByRole("button", { name: "Register endpoint" }));
  expect(await screen.findByText("registered byo_judge")).toBeInTheDocument();
  expect(api.callsTo("registerEndpoint")[0][0]).toMatchObject({
    name: "byo_judge", allowed_models: ["judge_v1", "judge_v2"], api_key_secret: "byo_judge_key",
  });
  fireEvent.click(await screen.findByRole("button", { name: "Test" }));
  expect(await screen.findByText(/reachable in 250 ms, token ids yes/)).toBeInTheDocument();
  fireEvent.change(screen.getByLabelText("byo_judge key"), { target: { value: "sk_customer_123" } });
  fireEvent.click(screen.getByRole("button", { name: "Set key" }));
  expect(await screen.findByText("key stored for byo_judge")).toBeInTheDocument();
  expect(api.callsTo("setEndpointKey")[0]).toEqual(["byo_judge", "sk_customer_123"]);
  expect(await screen.findByText("key set")).toBeInTheDocument();
  expect((screen.getByLabelText("byo_judge key") as HTMLInputElement).value).toBe("");
  expect(document.body.textContent).not.toContain("sk_customer_123");
}

test("endpoints register, test and take keys that are never shown again", endpointKeysAreWriteOnly);

async function routesReachEveryPage(): Promise<void> {
  render(<MemoryRouter initialEntries={["/catalogs"]}><ConsoleRoutes api={new FakeApi()} /></MemoryRouter>);
  expect(await screen.findByRole("navigation", { name: "catalogs" })).toBeInTheDocument();
  fireEvent.click(screen.getByRole("link", { name: "Configure" }));
  expect(await screen.findByLabelText("YAML")).toBeInTheDocument();
}

test("the shell routes between configure and catalogs", routesReachEveryPage);

import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { expect, test } from "vitest";
import { parse } from "yaml";

import { ApiError } from "../api/client";
import { PRESETS } from "../specs/presets";
import { catalogEntry, FakeApi } from "../test/fakeApi";
import { SpecEditorPage } from "./SpecEditorPage";

const CLONED_RUN = "experiment_000001_run_002";

function yamlBox(): HTMLTextAreaElement {
  return screen.getByLabelText("YAML") as HTMLTextAreaElement;
}

function saveButton(): HTMLElement {
  return screen.getByRole("button", { name: "Save spec" });
}

function imageSuggestionListed(): void {
  expect(document.querySelector("datalist option[value='win2025_office_v4']")).not.toBeNull();
}

function clonedSpecLoaded(): void {
  expect(yamlBox().value).toContain("group_size: 4");
}

async function formEditsRewriteTheYaml(): Promise<void> {
  const api = new FakeApi();
  api.catalogs.images = [catalogEntry("images", "win2025_office_v4")];
  render(<SpecEditorPage api={api} />);
  await waitFor(imageSuggestionListed);
  fireEvent.change(screen.getByLabelText("Group size"), { target: { value: "16" } });
  fireEvent.change(screen.getByLabelText("Run type"), { target: { value: "eval" } });
  const allowedActions = within(screen.getByRole("group", { name: "Allowed actions" }));
  fireEvent.click(allowedActions.getByLabelText("terminal"));
  fireEvent.click(screen.getByLabelText("Token ids"));
  const edited = parse(yamlBox().value);
  expect(edited.run.group_size).toBe(16);
  expect(edited.run.type).toBe("eval");
  expect(edited.task.allowed_actions).toEqual(["computer", "terminal", "control"]);
  expect(edited.run.policy.token_ids).toBe(false);
  expect(yamlBox().value).toContain("# who plays, who grades, how much");
}

test("form edits rewrite the YAML beside them and keep its comments", formEditsRewriteTheYaml);

function brokenSpecsCannotBeSaved(): void {
  render(<SpecEditorPage api={new FakeApi()} />);
  fireEvent.change(yamlBox(), { target: { value: "experiment: [\n" } });
  expect(screen.getByLabelText("YAML problems")).toBeInTheDocument();
  expect(saveButton()).toBeDisabled();
  fireEvent.change(yamlBox(), { target: { value: PRESETS[0].yaml.replace("budget_usd: 2500", "budget_usd: -5") } });
  expect(screen.getByLabelText("Schema problems")).toHaveTextContent("experiment.budget_usd");
  expect(saveButton()).toBeDisabled();
}

test("broken YAML and schema problems block saving", brokenSpecsCannotBeSaved);

async function savingShowsTheDigestAndItsCommands(): Promise<void> {
  const api = new FakeApi();
  render(<SpecEditorPage api={api} />);
  expect(screen.getByLabelText("preflight cli")).toHaveTextContent("rlworld specs preflight {digest}");
  fireEvent.click(saveButton());
  expect(await screen.findByRole("status")).toHaveTextContent("saved as sha256:abc123");
  expect(api.callsTo("saveSpec")[0][0]).toBe(PRESETS[0].yaml);
  expect(screen.getByLabelText("preflight cli")).toHaveTextContent("rlworld specs preflight sha256:abc123");
  expect(screen.getByLabelText("launch sdk")).toHaveTextContent('client.launch("sha256:abc123", budget_usd=2500)');
  fireEvent.change(screen.getByLabelText("Seed"), { target: { value: "7" } });
  expect(screen.getByLabelText("preflight cli")).toHaveTextContent("rlworld specs preflight {digest}");
}

test("saving shows the immutable digest and the matching CLI and SDK calls", savingShowsTheDigestAndItsCommands);

async function serverProblemsAreShown(): Promise<void> {
  const api = new FakeApi();
  api.saveFailure = new ApiError(400, "invalid_spec", "rejected", ["run: rl runs use the train split"]);
  render(<SpecEditorPage api={api} />);
  fireEvent.click(saveButton());
  expect(await screen.findByLabelText("Server problems")).toHaveTextContent("rl runs use the train split");
}

test("cross layer problems from the API are shown", serverProblemsAreShown);

async function clonedRunsDiffAgainstTheirSpec(): Promise<void> {
  const api = new FakeApi();
  api.runSpecs[CLONED_RUN] = PRESETS[0].yaml.replace("group_size: 8", "group_size: 4");
  render(<SpecEditorPage api={api} />);
  fireEvent.change(screen.getByLabelText("run to clone"), { target: { value: CLONED_RUN } });
  fireEvent.click(screen.getByRole("button", { name: "Clone run" }));
  await waitFor(clonedSpecLoaded);
  fireEvent.change(screen.getByLabelText("Group size"), { target: { value: "6" } });
  fireEvent.click(screen.getByRole("button", { name: "Diff" }));
  const diff = screen.getByLabelText("spec diff");
  expect(diff).toHaveTextContent(`Changes against run ${CLONED_RUN}`);
  expect(diff).toHaveTextContent("- group_size: 4");
  expect(diff).toHaveTextContent("+ group_size: 6");
  fireEvent.change(screen.getByLabelText("run to clone"), { target: { value: "missing_run" } });
  fireEvent.click(screen.getByRole("button", { name: "Clone run" }));
  expect(await screen.findByRole("status")).toHaveTextContent("could not clone missing_run");
}

test("a cloned run loads its spec and the diff shows every change", clonedRunsDiffAgainstTheirSpec);

function sweepsAreSpecFields(): void {
  render(<SpecEditorPage api={new FakeApi()} />);
  fireEvent.change(screen.getByLabelText("sweep path"), { target: { value: "run.group_size" } });
  fireEvent.change(screen.getByLabelText("sweep values"), { target: { value: "4, 8" } });
  fireEvent.click(screen.getByRole("button", { name: "Add sweep" }));
  expect(parse(yamlBox().value).experiment.sweep["run.group_size"]).toEqual([4, 8]);
  const distractors = screen.getByLabelText("task.difficulty.distractors values");
  fireEvent.change(distractors, { target: { value: "0, 20" } });
  fireEvent.blur(distractors);
  expect(parse(yamlBox().value).experiment.sweep["task.difficulty.distractors"]).toEqual([0, 20]);
  fireEvent.click(screen.getAllByRole("button", { name: "Remove" })[0]);
  expect(parse(yamlBox().value).experiment.sweep["task.difficulty.distractors"]).toBeUndefined();
  fireEvent.change(screen.getByLabelText("preset"), { target: { value: PRESETS[0].name } });
  expect(yamlBox().value).toBe(PRESETS[0].yaml);
}

test("sweeps are edited as spec fields and presets reset the editor", sweepsAreSpecFields);

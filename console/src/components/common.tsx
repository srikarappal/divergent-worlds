// Small presentational pieces shared by every page.
import type { ChangeEventHandler, ReactElement } from "react";

import { ApiError } from "../api/client";
import { cliCommand, operation, sdkSnippet, type TemplateValues } from "../api/operations";
import type { DiffLine } from "../specs/diff";

export interface ParityItem {
  name: string;
  values?: TemplateValues;
}

// Every page shows the CLI command and SDK call that do the same thing (DESIGN.md 6.14, parity).
export function ParityPanel(props: { items: readonly ParityItem[] }): ReactElement {
  const rows: ReactElement[] = [];
  for (const item of props.items) {
    rows.push(
      <div className="parity-row" key={item.name}>
        <div className="parity-summary">{operation(item.name).summary}</div>
        <pre aria-label={`${item.name} cli`}>{cliCommand(item.name, item.values)}</pre>
        <pre aria-label={`${item.name} sdk`}>{sdkSnippet(item.name, item.values)}</pre>
      </div>,
    );
  }
  return (
    <aside className="parity-panel">
      <h3>Same thing from the CLI and the SDK</h3>
      {rows}
    </aside>
  );
}

export function ProblemList(props: { title: string; problems: readonly string[] }): ReactElement | null {
  if (props.problems.length === 0) {
    return null;
  }
  const items: ReactElement[] = [];
  for (const [index, problem] of props.problems.entries()) {
    items.push(<li key={index}>{problem}</li>);
  }
  return (
    <div className="problems" role="alert" aria-label={`${props.title} problems`}>
      <strong>{props.title}</strong>
      <ul>{items}</ul>
    </div>
  );
}

export function YamlPane(props: { text: string; onYamlChange: ChangeEventHandler<HTMLTextAreaElement> }): ReactElement {
  return (
    <label className="yaml-pane">
      YAML
      <textarea value={props.text} onChange={props.onYamlChange} spellCheck={false} rows={40} />
    </label>
  );
}

export function DiffView(props: { label: string; lines: readonly DiffLine[] }): ReactElement {
  const rows: ReactElement[] = [];
  for (const [index, line] of props.lines.entries()) {
    const marker = line.kind === "added" ? "+" : line.kind === "removed" ? "-" : " ";
    rows.push(<div key={index} className={`diff-${line.kind}`}>{`${marker} ${line.text}`}</div>);
  }
  return (
    <section className="diff-view" aria-label="spec diff">
      <h3>Changes against {props.label}</h3>
      <pre>{rows}</pre>
    </section>
  );
}

export function formatPercent(value: number | null | undefined): string {
  return value === null || value === undefined ? "" : `${Math.round(value * 100)}%`;
}

// What to show for a failed call: the API's problem list when it sent one, otherwise the error message.
export function failureProblems(error: unknown): string[] {
  if (error instanceof ApiError) {
    return error.problems.length > 0 ? error.problems : [error.message];
  }
  return [error instanceof Error ? error.message : String(error)];
}

// One form field bound to one spec path; enum choices come from the shared JSON Schema.
import { Component, type ChangeEvent, type ReactElement } from "react";

import type { SpecPath } from "../specs/document";

export type FieldKind = "text" | "number" | "select" | "boolean" | "choices";

export interface FieldSpec {
  label: string;
  path: SpecPath;
  kind: FieldKind;
  options?: readonly string[];
  catalog?: string;
}

export interface FieldChangeListener {
  (path: SpecPath, value: unknown): void;
}

interface SpecFieldProps {
  field: FieldSpec;
  value: unknown;
  suggestions?: readonly string[];
  onFieldChange: FieldChangeListener;
}

function selectedChoices(value: unknown): Set<string> {
  const selected = new Set<string>();
  if (Array.isArray(value)) {
    for (const item of value) {
      selected.add(String(item));
    }
  }
  return selected;
}

function optionElements(options: readonly string[], withEmpty: boolean): ReactElement[] {
  const elements: ReactElement[] = withEmpty ? [<option key="" value="">(unset)</option>] : [];
  for (const option of options) {
    elements.push(<option key={option} value={option}>{option}</option>);
  }
  return elements;
}

export class SpecField extends Component<SpecFieldProps> {
  constructor(props: SpecFieldProps) {
    super(props);
    this.handleInput = this.handleInput.bind(this);
    this.handleChoice = this.handleChoice.bind(this);
  }

  handleInput(event: ChangeEvent<HTMLInputElement | HTMLSelectElement>): void {
    const { field, onFieldChange } = this.props;
    const target = event.target;
    if (field.kind === "boolean") {
      onFieldChange(field.path, (target as HTMLInputElement).checked);
    } else if (field.kind === "number") {
      onFieldChange(field.path, target.value === "" ? undefined : Number(target.value));
    } else {
      onFieldChange(field.path, target.value === "" ? undefined : target.value);
    }
  }

  handleChoice(event: ChangeEvent<HTMLInputElement>): void {
    const selected = selectedChoices(this.props.value);
    if (event.target.checked) {
      selected.add(event.target.value);
    } else {
      selected.delete(event.target.value);
    }
    const ordered: string[] = [];
    for (const option of this.props.field.options ?? []) {
      if (selected.has(option)) {
        ordered.push(option);
      }
    }
    this.props.onFieldChange(this.props.field.path, ordered);
  }

  renderChoices(): ReactElement {
    const { field, value } = this.props;
    const selected = selectedChoices(value);
    const boxes: ReactElement[] = [];
    for (const option of field.options ?? []) {
      boxes.push(
        <label key={option} className="choice">
          <input type="checkbox" value={option} checked={selected.has(option)} onChange={this.handleChoice} />
          {option}
        </label>,
      );
    }
    return (
      <fieldset className="field choices">
        <legend>{field.label}</legend>
        {boxes}
      </fieldset>
    );
  }

  render(): ReactElement {
    const { field, value, suggestions } = this.props;
    if (field.kind === "choices") {
      return this.renderChoices();
    }
    if (field.kind === "boolean") {
      return (
        <label className="field">
          <input type="checkbox" checked={value === true} onChange={this.handleInput} />
          {field.label}
        </label>
      );
    }
    if (field.kind === "select") {
      return (
        <label className="field">
          {field.label}
          <select value={value === undefined || value === null ? "" : String(value)} onChange={this.handleInput}>
            {optionElements(field.options ?? [], true)}
          </select>
        </label>
      );
    }
    const listId = suggestions && suggestions.length > 0 ? `suggestions_${field.path.join("_")}` : undefined;
    return (
      <label className="field">
        {field.label}
        <input
          type={field.kind === "number" ? "number" : "text"}
          value={value === undefined || value === null ? "" : String(value)}
          list={listId}
          onChange={this.handleInput}
        />
        {listId ? <datalist id={listId}>{optionElements(suggestions ?? [], false)}</datalist> : null}
      </label>
    );
  }
}

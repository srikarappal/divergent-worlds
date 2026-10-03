// The spec's YAML text is the editor's source of truth; form edits go through the YAML document so comments,
// key order and flow style survive.
import { parse, parseDocument } from "yaml";

export interface ParsedSpec {
  value: unknown;
  errors: string[];
}

export type SpecPath = readonly string[];

export function parseSpec(text: string): ParsedSpec {
  const document = parseDocument(text);
  if (document.errors.length > 0) {
    const errors: string[] = [];
    for (const error of document.errors) {
      errors.push(error.message);
    }
    return { value: null, errors };
  }
  return { value: document.toJS(), errors: [] };
}

export function readPath(value: unknown, path: SpecPath): unknown {
  let current = value;
  for (const key of path) {
    if (current === null || typeof current !== "object") {
      return undefined;
    }
    current = (current as Record<string, unknown>)[key];
  }
  return current;
}

// Sets one field; undefined or an empty string removes it so the model default applies.
export function writePath(text: string, path: SpecPath, fieldValue: unknown): string {
  const document = parseDocument(text);
  if (fieldValue === undefined || fieldValue === "") {
    document.deleteIn(path);
  } else {
    document.setIn(path, fieldValue);
  }
  return document.toString();
}

// Sweep values are typed as YAML flow items, such as "0, 5, 10" or "low, high".
export function parseSweepValues(text: string): unknown[] {
  const values = parse(`[${text}]`);
  return Array.isArray(values) ? values : [];
}

export function formatSweepValues(values: unknown): string {
  if (!Array.isArray(values)) {
    return "";
  }
  const formatted: string[] = [];
  for (const value of values) {
    formatted.push(typeof value === "string" ? value : JSON.stringify(value));
  }
  return formatted.join(", ");
}

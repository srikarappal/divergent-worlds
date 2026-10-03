// The public operations the console performs, from the manifest the SDK and the CLI share (DESIGN.md 6.14).
import manifest from "../../../schemas/api_operations.json";

export interface Operation {
  name: string;
  method: "GET" | "POST" | "PUT";
  path: string;
  body?: "yaml" | "json";
  query?: string[];
  summary: string;
  cli: string;
  sdk: string;
}

export type TemplateValues = Record<string, string | number | boolean | null | undefined>;

export const OPERATIONS: readonly Operation[] = manifest.operations as Operation[];
const PLACEHOLDER_SPLIT = /\{(\w+)\}/;
const OPERATIONS_BY_NAME = new Map<string, Operation>();
for (const listed of OPERATIONS) {
  OPERATIONS_BY_NAME.set(listed.name, listed);
}

export function operation(name: string): Operation {
  const found = OPERATIONS_BY_NAME.get(name);
  if (found === undefined) {
    throw new Error(`unknown operation ${name}`);
  }
  return found;
}

function templateValue(name: string, value: TemplateValues[string]): string {
  return value === undefined || value === null ? `{${name}}` : String(value);
}

// Fills {placeholders}; a value that is not known yet keeps its placeholder so the reader sees what to supply.
export function fillTemplate(template: string, values: TemplateValues): string {
  const parts = template.split(PLACEHOLDER_SPLIT);
  let filled = "";
  for (let index = 0; index < parts.length; index += 1) {
    filled += index % 2 === 0 ? parts[index] : templateValue(parts[index], values[parts[index]]);
  }
  return filled;
}

export function fillPath(path: string, values: Record<string, string>): string {
  const parts = path.split(PLACEHOLDER_SPLIT);
  let filled = "";
  for (let index = 0; index < parts.length; index += 1) {
    filled += index % 2 === 0 ? parts[index] : encodeURIComponent(values[parts[index]] ?? "");
  }
  return filled;
}

export function cliCommand(name: string, values: TemplateValues = {}): string {
  return fillTemplate(operation(name).cli, values);
}

export function sdkSnippet(name: string, values: TemplateValues = {}): string {
  return fillTemplate(operation(name).sdk, values);
}

export function methodName(operationName: string): string {
  const words = operationName.split("_");
  let camel = words[0];
  for (const word of words.slice(1)) {
    camel += word.charAt(0).toUpperCase() + word.slice(1);
  }
  return camel;
}

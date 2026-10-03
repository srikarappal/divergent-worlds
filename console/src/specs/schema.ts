// Client side validation against the spec JSON Schema the Python models export (schemas/spec_document.schema.json).
// Cross layer rules live in the API; the console shows those problems when a save is rejected.
import Ajv2020, { type ErrorObject } from "ajv/dist/2020";
import addFormats from "ajv-formats";
import { fullFormats } from "ajv-formats/dist/formats";

import specSchema from "../../../schemas/spec_document.schema.json";

export interface SpecProblem {
  path: string;
  message: string;
}

interface SchemaNode {
  $ref?: string;
  anyOf?: SchemaNode[];
  enum?: unknown[];
  items?: SchemaNode;
  properties?: Record<string, SchemaNode>;
  type?: string;
}

const ajv = new Ajv2020({ allErrors: true, strict: false });
addFormats(ajv);
// The Python models accept timestamps without a zone, such as clock.fixed: "2026-03-14T09:00:00".
ajv.addFormat("date-time", fullFormats["iso-date-time"]);
const validateDocument = ajv.compile(specSchema);
const DEFINITIONS = (specSchema as { $defs: Record<string, SchemaNode> }).$defs;

function problemFrom(error: ErrorObject): SpecProblem {
  const segments = error.instancePath.split("/").slice(1);
  if (error.keyword === "additionalProperties") {
    segments.push(String(error.params.additionalProperty));
    return { path: segments.join("."), message: "is not a known field" };
  }
  if (error.keyword === "required") {
    segments.push(String(error.params.missingProperty));
    return { path: segments.join("."), message: "is required" };
  }
  if (error.keyword === "enum") {
    return { path: segments.join(".") || "spec", message: `must be one of ${error.params.allowedValues.join(", ")}` };
  }
  return { path: segments.join(".") || "spec", message: error.message ?? "is invalid" };
}

// Problems in document order, one per path and message. The null branch of an optional field and the anyOf
// summary only repeat what the specific error already says.
export function validateSpec(document: unknown): SpecProblem[] {
  if (validateDocument(document)) {
    return [];
  }
  const seen = new Set<string>();
  const problems: SpecProblem[] = [];
  for (const error of validateDocument.errors ?? []) {
    const problem = problemFrom(error);
    const key = `${problem.path}|${problem.message}`;
    const isNullBranch = error.keyword === "type" && error.params.type === "null";
    if (!seen.has(key) && !isNullBranch && error.keyword !== "anyOf") {
      seen.add(key);
      problems.push(problem);
    }
  }
  return problems;
}

function resolve(node: SchemaNode | undefined): SchemaNode | undefined {
  if (node?.$ref !== undefined) {
    return resolve(DEFINITIONS[node.$ref.replace("#/$defs/", "")]);
  }
  if (node?.anyOf !== undefined) {
    for (const branch of node.anyOf) {
      if (branch.type !== "null") {
        return resolve(branch);
      }
    }
  }
  return node;
}

// Allowed values of an enum field, or of an array field's items, straight from the shared schema.
export function schemaOptions(path: readonly string[]): string[] {
  let node: SchemaNode | undefined = specSchema as SchemaNode;
  for (const key of path) {
    node = resolve(resolve(node)?.properties?.[key]);
  }
  const values = node?.enum ?? resolve(node?.items)?.enum ?? [];
  return values.map(String);
}

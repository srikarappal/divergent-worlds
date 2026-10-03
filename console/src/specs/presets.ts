// Presets are the example specs in the repository, bundled at build time; any past run can be cloned as well.

export interface Preset {
  name: string;
  yaml: string;
}

const PRESET_FILES = import.meta.glob("../../../examples/specs/*.yaml", {
  query: "?raw",
  import: "default",
  eager: true,
}) as Record<string, string>;

function presetName(filePath: string): string {
  return filePath.slice(filePath.lastIndexOf("/") + 1).replace(/\.yaml$/, "");
}

export const PRESETS: Preset[] = [];
for (const [filePath, yaml] of Object.entries(PRESET_FILES)) {
  PRESETS.push({ name: presetName(filePath), yaml });
}

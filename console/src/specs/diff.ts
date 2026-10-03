// Line diff between two spec texts, by longest common subsequence; specs are small enough for the quadratic table.

export interface DiffLine {
  kind: "same" | "added" | "removed";
  text: string;
}

export function diffLines(before: string, after: string): DiffLine[] {
  const beforeLines = before.split("\n");
  const afterLines = after.split("\n");
  const common: number[][] = [];
  for (let row = 0; row <= beforeLines.length; row += 1) {
    common.push(new Array<number>(afterLines.length + 1).fill(0));
  }
  for (let row = beforeLines.length - 1; row >= 0; row -= 1) {
    for (let column = afterLines.length - 1; column >= 0; column -= 1) {
      common[row][column] = beforeLines[row] === afterLines[column]
        ? common[row + 1][column + 1] + 1
        : Math.max(common[row + 1][column], common[row][column + 1]);
    }
  }
  const lines: DiffLine[] = [];
  let row = 0;
  let column = 0;
  while (row < beforeLines.length && column < afterLines.length) {
    if (beforeLines[row] === afterLines[column]) {
      lines.push({ kind: "same", text: beforeLines[row] });
      row += 1;
      column += 1;
    } else if (common[row + 1][column] >= common[row][column + 1]) {
      lines.push({ kind: "removed", text: beforeLines[row] });
      row += 1;
    } else {
      lines.push({ kind: "added", text: afterLines[column] });
      column += 1;
    }
  }
  for (; row < beforeLines.length; row += 1) {
    lines.push({ kind: "removed", text: beforeLines[row] });
  }
  for (; column < afterLines.length; column += 1) {
    lines.push({ kind: "added", text: afterLines[column] });
  }
  return lines;
}

export function changedLineCount(lines: readonly DiffLine[]): number {
  let changed = 0;
  for (const line of lines) {
    changed += line.kind === "same" ? 0 : 1;
  }
  return changed;
}

import { parseDiff, Diff, Hunk, type FileData } from "react-diff-view";
import "react-diff-view/style/index.css";
import "./diff-viewer.css";

// gitdiff-parser requires a "diff --git" header to extract paths.
// Plain unified diffs (--- a/foo / +++ b/foo, no "diff --git" line) produce
// empty oldPath/newPath. Normalise by injecting a synthetic header so path
// extraction always works for both formats.
function normalizePatch(patch: string): string {
  if (patch.startsWith("diff --git")) return patch;
  return patch.replace(
    /^(--- ([^\n]+)\n\+\+\+ ([^\n]+))/gm,
    (_, block, oldRaw, newRaw) => {
      const strip = (p: string) => p.replace(/^[ab]\//, "");
      const a = strip(oldRaw.trim());
      const b = strip(newRaw.trim());
      return `diff --git a/${a} b/${b}\n--- ${oldRaw}\n+++ ${newRaw}`;
    },
  );
}

function fileCounts(file: FileData): { add: number; del: number } {
  let add = 0;
  let del = 0;
  for (const h of file.hunks) {
    for (const c of h.changes) {
      if (c.type === "insert") add += 1;
      else if (c.type === "delete") del += 1;
    }
  }
  return { add, del };
}

export function DiffViewer({ patch }: { patch: string | null | undefined }) {
  if (!patch || !patch.trim()) return null;
  let files: FileData[] = [];
  try {
    files = parseDiff(normalizePatch(patch));
  } catch {
    return null;
  }
  if (files.length === 0) return null;
  return (
    <div className="durin-diff flex flex-col gap-2">
      {files.map((file, i) => {
        const { add, del } = fileCounts(file);
        const path = file.newPath || file.oldPath || "file";
        return (
          <div
            key={`${path}-${i}`}
            className="overflow-hidden rounded-[8px] border border-border/40"
          >
            <div className="flex items-center gap-2 border-b border-border/40 bg-muted/40 px-3 py-1.5 font-mono text-[12px] text-muted-foreground">
              <span className="text-foreground/80">{path}</span>
              <span className="ml-auto text-emerald-600 dark:text-emerald-400">
                +{add}
              </span>
              <span className="text-red-600 dark:text-red-400">-{del}</span>
            </div>
            <Diff viewType="unified" diffType={file.type} hunks={file.hunks}>
              {(hunks) => hunks.map((h) => <Hunk key={h.content} hunk={h} />)}
            </Diff>
          </div>
        );
      })}
    </div>
  );
}

import { FileBox, FileCode, FileText, Folder } from "lucide-react";

import type { SkillFile } from "@/lib/api";
import { cn } from "@/lib/utils";

interface Props {
  files: SkillFile[];
  selected?: string;
  onSelect: (path: string) => void;
}

/** Pick a file icon based on extension. */
function fileIcon(path: string): React.ReactNode {
  const ext = path.split(".").pop()?.toLowerCase() ?? "";
  if (["py", "sh", "ts", "tsx", "js", "jsx"].includes(ext))
    return <FileCode className="h-3.5 w-3.5 shrink-0 text-muted-foreground" aria-hidden />;
  if (["bin", "whl", "gz", "zip", "tar"].includes(ext))
    return <FileBox className="h-3.5 w-3.5 shrink-0 text-muted-foreground" aria-hidden />;
  return <FileText className="h-3.5 w-3.5 shrink-0 text-muted-foreground" aria-hidden />;
}

interface TreeNode {
  /** Relative display name (last segment for files, first segment for folders). */
  label: string;
  /** Full path (only set for file nodes). */
  path?: string;
  /** Child nodes (only set for folder nodes). */
  children?: TreeNode[];
}

/**
 * Group a flat list of SkillFile paths into a two-level tree (folder →
 * children). Paths without a `/` are top-level files. Paths with one or more
 * `/` are grouped under their first segment; the remaining suffix becomes the
 * child path.
 */
function buildTree(files: SkillFile[]): TreeNode[] {
  const folderMap = new Map<string, TreeNode>();
  const roots: TreeNode[] = [];

  for (const f of files) {
    const slash = f.path.indexOf("/");
    if (slash === -1) {
      roots.push({ label: f.path, path: f.path });
    } else {
      const folder = f.path.slice(0, slash);
      const rest = f.path.slice(slash + 1);
      if (!folderMap.has(folder)) {
        const node: TreeNode = { label: folder, children: [] };
        folderMap.set(folder, node);
        roots.push(node);
      }
      folderMap.get(folder)!.children!.push({ label: rest, path: f.path });
    }
  }

  return roots;
}

function FileRow({
  label,
  path,
  selected,
  onSelect,
}: {
  label: string;
  path: string;
  selected?: string;
  onSelect: (p: string) => void;
}) {
  const active = selected === path;
  return (
    <button
      type="button"
      onClick={() => onSelect(path)}
      className={cn(
        "flex w-full items-center gap-1.5 rounded-[6px] px-2 py-1 text-left text-[12px] transition-colors",
        active
          ? "bg-primary/10 text-primary"
          : "text-foreground hover:bg-muted/40",
      )}
    >
      {fileIcon(label)}
      <span className="truncate">{label}</span>
    </button>
  );
}

/**
 * Two-level file tree for a skill's files. Flat paths are grouped by their
 * first directory segment; files at the root render directly. Clicking a file
 * fires `onSelect(path)` with its full path.
 */
export function SkillFileTree({ files, selected, onSelect }: Props) {
  const nodes = buildTree(files);

  return (
    <div className="flex flex-col gap-0.5 py-1">
      {nodes.map((node) =>
        node.path !== undefined ? (
          <FileRow
            key={node.path}
            label={node.label}
            path={node.path}
            selected={selected}
            onSelect={onSelect}
          />
        ) : (
          <div key={node.label}>
            <div className="flex items-center gap-1.5 px-2 py-1">
              <Folder className="h-3.5 w-3.5 shrink-0 text-muted-foreground" aria-hidden />
              <span className="text-[11px] font-semibold uppercase tracking-wide text-muted-foreground">
                {node.label}
              </span>
            </div>
            <div className="ml-3 flex flex-col gap-0.5 border-l border-border/30 pl-2">
              {node.children?.map((child) => (
                <FileRow
                  key={child.path}
                  label={child.label}
                  path={child.path!}
                  selected={selected}
                  onSelect={onSelect}
                />
              ))}
            </div>
          </div>
        ),
      )}
    </div>
  );
}

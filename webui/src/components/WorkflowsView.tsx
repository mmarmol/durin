import { useEffect, useMemo, useState } from "react";
import {
  Background,
  Controls,
  Handle,
  Position,
  ReactFlow,
  type NodeProps,
} from "@xyflow/react";
import "@xyflow/react/dist/style.css";
import { Loader2, Workflow as WorkflowIcon } from "lucide-react";
import { useTranslation } from "react-i18next";

import { ApiError, getWorkflow, listWorkflows } from "@/lib/api";
import {
  workflowToFlow,
  type FlowNodeData,
  type WorkflowDef,
} from "@/lib/workflow-graph";
import { useClient } from "@/providers/ClientProvider";
import { cn } from "@/lib/utils";

function errMsg(e: unknown): string {
  return e instanceof ApiError ? `HTTP ${e.status}` : (e as Error).message;
}

const KIND_RING: Record<string, string> = {
  work: "border-emerald-400/70",
  decision: "border-amber-400/70",
  parallel: "border-violet-400/70",
  subworkflow: "border-sky-400/70",
};

function nodeSummary(node: FlowNodeData["node"]): string {
  switch (node.kind) {
    case "work":
      return `${(node.mode as string) ?? "build"} · ${(node.model as string) ?? "default"}`;
    case "decision":
      return node.criteria ? "judge" : "command";
    case "parallel":
      return `${((node.branches as string[]) ?? []).length} branches`;
    case "subworkflow":
      return String(node.workflow ?? "");
    default:
      return "";
  }
}

function NodeCard({ data }: NodeProps) {
  const { node, isStart } = data as unknown as FlowNodeData;
  return (
    <div
      className={cn(
        "min-w-[150px] rounded-md border bg-background px-3 py-2",
        KIND_RING[node.kind] ?? "border-border",
        isStart && "ring-1 ring-primary",
      )}
    >
      <Handle type="target" position={Position.Left} />
      <div className="text-[10px] uppercase tracking-wide text-muted-foreground">
        {node.kind}
      </div>
      <div className="text-sm font-medium">{node.id}</div>
      <div className="text-xs text-muted-foreground">{nodeSummary(node)}</div>
      <Handle type="source" position={Position.Right} />
    </div>
  );
}

const nodeTypes = {
  work: NodeCard,
  decision: NodeCard,
  parallel: NodeCard,
  subworkflow: NodeCard,
};

export function WorkflowsView() {
  const { t } = useTranslation();
  const { token } = useClient();
  const [names, setNames] = useState<string[]>([]);
  const [selected, setSelected] = useState<string | null>(null);
  const [def, setDef] = useState<WorkflowDef | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    (async () => {
      try {
        const list = await listWorkflows(token);
        setNames(list);
        if (list.length > 0) setSelected(list[0]);
      } catch (e) {
        setError(errMsg(e));
      } finally {
        setLoading(false);
      }
    })();
  }, [token]);

  useEffect(() => {
    if (!selected) {
      setDef(null);
      return;
    }
    (async () => {
      try {
        const d = (await getWorkflow(token, selected)) as unknown as WorkflowDef;
        setDef(d);
        setError(null);
      } catch (e) {
        setError(errMsg(e));
      }
    })();
  }, [selected, token]);

  const flow = useMemo(
    () => (def ? workflowToFlow(def) : { nodes: [], edges: [] }),
    [def],
  );

  return (
    <div className="flex h-full w-full">
      <aside className="w-56 shrink-0 overflow-y-auto border-r p-2">
        <div className="flex items-center gap-2 px-2 py-1 text-sm font-medium">
          <WorkflowIcon className="h-4 w-4" aria-hidden />
          {t("workflows.title")}
        </div>
        {loading ? (
          <div className="flex items-center gap-2 p-2 text-sm text-muted-foreground">
            <Loader2 className="h-4 w-4 animate-spin" /> {t("workflows.loading")}
          </div>
        ) : names.length === 0 ? (
          <div className="p-2 text-sm text-muted-foreground">
            {t("workflows.empty")}
          </div>
        ) : (
          names.map((n) => (
            <button
              key={n}
              type="button"
              onClick={() => setSelected(n)}
              className={cn(
                "block w-full truncate rounded px-2 py-1 text-left text-sm hover:bg-accent",
                selected === n && "bg-accent font-medium",
              )}
            >
              {n}
            </button>
          ))
        )}
      </aside>

      <div className="relative flex-1">
        {error && (
          <div className="absolute left-2 top-2 z-10 rounded bg-destructive/10 px-2 py-1 text-sm text-destructive">
            {error}
          </div>
        )}
        {def ? (
          <ReactFlow
            nodes={flow.nodes}
            edges={flow.edges}
            nodeTypes={nodeTypes}
            fitView
            nodesDraggable={false}
            nodesConnectable={false}
            elementsSelectable={false}
            proOptions={{ hideAttribution: true }}
          >
            <Background />
            <Controls showInteractive={false} />
          </ReactFlow>
        ) : (
          !loading && (
            <div className="flex h-full items-center justify-center text-sm text-muted-foreground">
              {t("workflows.selectPrompt")}
            </div>
          )
        )}
      </div>
    </div>
  );
}

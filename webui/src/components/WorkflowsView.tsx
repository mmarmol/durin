import { useCallback, useEffect, useMemo, useState } from "react";
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

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import { ApiError, getWorkflow, listWorkflows, saveWorkflow } from "@/lib/api";
import {
  workflowToFlow,
  type FlowNodeData,
  type WorkflowDef,
  type WorkflowNodeDef,
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

function nodeSummary(node: WorkflowNodeDef): string {
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

function NodeCard({ data, selected }: NodeProps) {
  const { node, isStart } = data as unknown as FlowNodeData;
  return (
    <div
      className={cn(
        "min-w-[150px] rounded-md border bg-background px-3 py-2",
        KIND_RING[node.kind] ?? "border-border",
        (isStart || selected) && "ring-2 ring-primary",
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

const MODES = ["build", "plan", "explore"];
const CONTEXTS = ["own", "shared"];
const TOOLS = ["none", "default"];

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <label className="flex flex-col gap-1">
      <span className="text-xs text-muted-foreground">{label}</span>
      {children}
    </label>
  );
}

function NodeConfigPanel({
  node,
  onChange,
}: {
  node: WorkflowNodeDef;
  onChange: (patch: Partial<WorkflowNodeDef>) => void;
}) {
  const selectCls =
    "h-8 rounded-md border border-border bg-background px-2 text-sm";
  return (
    <div className="flex flex-col gap-3">
      <div className="flex items-center gap-2">
        <span className="rounded bg-muted px-1.5 py-0.5 text-[10px] uppercase">
          {node.kind}
        </span>
        <span className="text-sm font-medium">{node.id}</span>
      </div>

      {node.kind === "work" && (
        <>
          <Field label="work mode">
            <select
              className={selectCls}
              value={(node.mode as string) ?? "build"}
              onChange={(e) => onChange({ mode: e.target.value })}
            >
              {MODES.map((m) => (
                <option key={m} value={m}>{m}</option>
              ))}
            </select>
          </Field>
          <Field label="model">
            <Input
              value={(node.model as string) ?? ""}
              placeholder="default"
              onChange={(e) => onChange({ model: e.target.value || undefined })}
            />
          </Field>
          <Field label="context">
            <select
              className={selectCls}
              value={(node.context as string) ?? "own"}
              onChange={(e) => onChange({ context: e.target.value })}
            >
              {CONTEXTS.map((c) => (
                <option key={c} value={c}>{c}</option>
              ))}
            </select>
          </Field>
          <Field label="tools">
            <select
              className={selectCls}
              value={(node.tools as string) ?? "none"}
              onChange={(e) => onChange({ tools: e.target.value })}
            >
              {TOOLS.map((t) => (
                <option key={t} value={t}>{t}</option>
              ))}
            </select>
          </Field>
          <Field label="prompt">
            <Textarea
              rows={6}
              value={(node.prompt as string) ?? ""}
              onChange={(e) => onChange({ prompt: e.target.value })}
            />
          </Field>
        </>
      )}

      {node.kind === "decision" && (
        <>
          <Field label={node.command ? "command" : "criteria"}>
            <Textarea
              rows={4}
              value={(node.criteria as string) || (node.command as string) || ""}
              onChange={(e) =>
                onChange(
                  node.command
                    ? { command: e.target.value }
                    : { criteria: e.target.value },
                )
              }
            />
          </Field>
          {node.criteria !== undefined && node.criteria !== "" && (
            <Field label="judge model">
              <Input
                value={(node.judge_model as string) ?? ""}
                placeholder="default"
                onChange={(e) => onChange({ judge_model: e.target.value || undefined })}
              />
            </Field>
          )}
        </>
      )}

      {(node.kind === "parallel" || node.kind === "subworkflow") && (
        <div className="text-xs text-muted-foreground">
          {node.kind} node — structure editing comes later.
        </div>
      )}
    </div>
  );
}

export function WorkflowsView() {
  const { t } = useTranslation();
  const { token } = useClient();
  const [names, setNames] = useState<string[]>([]);
  const [selected, setSelected] = useState<string | null>(null);
  const [def, setDef] = useState<WorkflowDef | null>(null);
  const [selectedNodeId, setSelectedNodeId] = useState<string | null>(null);
  const [dirty, setDirty] = useState(false);
  const [saving, setSaving] = useState(false);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

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
        setSelectedNodeId(null);
        setDirty(false);
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

  const updateNode = useCallback(
    (patch: Partial<WorkflowNodeDef>) => {
      if (!selectedNodeId) return;
      setDef((d) =>
        d
          ? {
              ...d,
              nodes: d.nodes.map((n) =>
                n.id === selectedNodeId ? { ...n, ...patch } : n,
              ),
            }
          : d,
      );
      setDirty(true);
      setNotice(null);
    },
    [selectedNodeId],
  );

  const onSave = useCallback(async () => {
    if (!selected || !def) return;
    setSaving(true);
    setError(null);
    try {
      await saveWorkflow(token, selected, def);
      setDirty(false);
      setNotice(t("workflows.saved"));
    } catch (e) {
      setError(errMsg(e));
    } finally {
      setSaving(false);
    }
  }, [selected, def, token, t]);

  const selectedNode = def?.nodes.find((n) => n.id === selectedNodeId) ?? null;

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

      <div className="relative flex h-full flex-1">
        <div className="relative h-full flex-1">
          {error && (
            <div className="absolute left-2 top-2 z-10 rounded bg-destructive/10 px-2 py-1 text-sm text-destructive">
              {error}
            </div>
          )}
          {notice && (
            <div className="absolute left-2 top-2 z-10 rounded bg-emerald-500/10 px-2 py-1 text-sm text-emerald-600">
              {notice}
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
              onNodeClick={(_, node) => setSelectedNodeId(node.id)}
              onPaneClick={() => setSelectedNodeId(null)}
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

        {selectedNode && (
          <aside className="flex w-72 shrink-0 flex-col gap-3 overflow-y-auto border-l p-3">
            <NodeConfigPanel node={selectedNode} onChange={updateNode} />
            <div className="mt-auto flex items-center gap-2 border-t pt-3">
              <Button size="sm" onClick={onSave} disabled={!dirty || saving}>
                {saving ? (
                  <Loader2 className="h-4 w-4 animate-spin" />
                ) : (
                  t("workflows.save")
                )}
              </Button>
              {dirty && (
                <span className="text-xs text-muted-foreground">
                  {t("workflows.unsaved")}
                </span>
              )}
            </div>
          </aside>
        )}
      </div>
    </div>
  );
}

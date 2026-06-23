import { useCallback, useEffect, useMemo, useState } from "react";
import {
  Background,
  Controls,
  Handle,
  Position,
  ReactFlow,
  type Connection,
  type NodeProps,
} from "@xyflow/react";
import "@xyflow/react/dist/style.css";
import { Lightbulb, Loader2, Play, Plus, Trash2, Workflow as WorkflowIcon } from "lucide-react";
import { useTranslation } from "react-i18next";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import {
  ApiError,
  applyWorkflowRecommendation,
  getWorkflow,
  getWorkflowRecommendations,
  listWorkflows,
  runWorkflow,
  saveWorkflow,
  type WorkflowRecommendation,
  type WorkflowRunResult,
} from "@/lib/api";
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
        {node.kind}{isStart ? " · start" : ""}
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
const selectCls = "h-8 w-full rounded-md border border-border bg-background px-2 text-sm";

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <label className="flex flex-col gap-1">
      <span className="text-xs text-muted-foreground">{label}</span>
      {children}
    </label>
  );
}

function TargetSelect({
  value,
  options,
  onChange,
}: {
  value: string | null | undefined;
  options: string[];
  onChange: (v: string | null) => void;
}) {
  return (
    <select
      className={selectCls}
      value={value ?? ""}
      onChange={(e) => onChange(e.target.value || null)}
    >
      <option value="">(end)</option>
      {options.map((o) => (
        <option key={o} value={o}>{o}</option>
      ))}
    </select>
  );
}

function NodeConfigPanel({
  node,
  nodeIds,
  isStart,
  onChange,
  onMakeStart,
  onDelete,
}: {
  node: WorkflowNodeDef;
  nodeIds: string[];
  isStart: boolean;
  onChange: (patch: Partial<WorkflowNodeDef>) => void;
  onMakeStart: () => void;
  onDelete: () => void;
}) {
  const others = nodeIds.filter((id) => id !== node.id);
  return (
    <div className="flex flex-col gap-3">
      <div className="flex items-center gap-2">
        <span className="rounded bg-muted px-1.5 py-0.5 text-[10px] uppercase">
          {node.kind}
        </span>
        <span className="text-sm font-medium">{node.id}</span>
        {!isStart && (
          <button
            type="button"
            className="ml-auto text-xs text-muted-foreground hover:text-foreground"
            onClick={onMakeStart}
          >
            set as start
          </button>
        )}
      </div>

      {node.kind === "work" && (
        <>
          <Field label="work mode">
            <select className={selectCls} value={(node.mode as string) ?? "build"}
              onChange={(e) => onChange({ mode: e.target.value })}>
              {MODES.map((m) => <option key={m} value={m}>{m}</option>)}
            </select>
          </Field>
          <Field label="model">
            <Input value={(node.model as string) ?? ""} placeholder="default"
              onChange={(e) => onChange({ model: e.target.value || undefined })} />
          </Field>
          <Field label="context">
            <select className={selectCls} value={(node.context as string) ?? "own"}
              onChange={(e) => onChange({ context: e.target.value })}>
              {CONTEXTS.map((c) => <option key={c} value={c}>{c}</option>)}
            </select>
          </Field>
          <Field label="tools">
            <select className={selectCls} value={(node.tools as string) ?? "none"}
              onChange={(e) => onChange({ tools: e.target.value })}>
              {TOOLS.map((tl) => <option key={tl} value={tl}>{tl}</option>)}
            </select>
          </Field>
          <Field label="prompt">
            <Textarea rows={5} value={(node.prompt as string) ?? ""}
              onChange={(e) => onChange({ prompt: e.target.value })} />
          </Field>
          <Field label="next">
            <TargetSelect value={node.next as string} options={others}
              onChange={(v) => onChange({ next: v })} />
          </Field>
        </>
      )}

      {node.kind === "decision" && (
        <>
          <Field label={node.command ? "command" : "criteria"}>
            <Textarea rows={3}
              value={(node.criteria as string) || (node.command as string) || ""}
              onChange={(e) => onChange(node.command ? { command: e.target.value } : { criteria: e.target.value })} />
          </Field>
          <Field label="on pass">
            <TargetSelect value={node.on_pass as string} options={others}
              onChange={(v) => onChange({ on_pass: v })} />
          </Field>
          <Field label="on fail">
            <TargetSelect value={node.on_fail as string} options={others}
              onChange={(v) => onChange({ on_fail: v })} />
          </Field>
        </>
      )}

      {(node.kind === "parallel" || node.kind === "subworkflow") && (
        <Field label="next">
          <TargetSelect value={node.next as string} options={others}
            onChange={(v) => onChange({ next: v })} />
        </Field>
      )}

      <button
        type="button"
        className="mt-1 flex items-center gap-1.5 self-start text-xs text-destructive hover:underline"
        onClick={onDelete}
      >
        <Trash2 className="h-3.5 w-3.5" /> delete node
      </button>
    </div>
  );
}

let _idSeq = 0;

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
  const [recs, setRecs] = useState<WorkflowRecommendation[]>([]);
  const [task, setTask] = useState("");
  const [running, setRunning] = useState(false);
  const [runResult, setRunResult] = useState<WorkflowRunResult | null>(null);

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

  const mutate = useCallback((fn: (d: WorkflowDef) => WorkflowDef) => {
    setDef((d) => (d ? fn(d) : d));
    setDirty(true);
    setNotice(null);
  }, []);

  const updateNode = useCallback(
    (patch: Partial<WorkflowNodeDef>) => {
      if (!selectedNodeId) return;
      mutate((d) => ({
        ...d,
        nodes: d.nodes.map((n) => (n.id === selectedNodeId ? { ...n, ...patch } : n)),
      }));
    },
    [selectedNodeId, mutate],
  );

  const addNode = useCallback(
    (kind: "work" | "decision") => {
      const id = `${kind}-${++_idSeq}`;
      const node: WorkflowNodeDef =
        kind === "work"
          ? { id, kind: "work", mode: "build", prompt: "", next: null }
          : { id, kind: "decision", criteria: "", on_pass: null, on_fail: null };
      mutate((d) => ({ ...d, nodes: [...d.nodes, node] }));
      setSelectedNodeId(id);
    },
    [mutate],
  );

  const deleteNode = useCallback(
    (id: string) => {
      mutate((d) => {
        const nodes = d.nodes
          .filter((n) => n.id !== id)
          .map((n) => ({
            ...n,
            next: n.next === id ? null : n.next,
            on_pass: n.on_pass === id ? null : n.on_pass,
            on_fail: n.on_fail === id ? null : n.on_fail,
            branches: Array.isArray(n.branches)
              ? n.branches.filter((b) => b !== id)
              : n.branches,
          }));
        const start = d.start === id ? (nodes[0]?.id ?? "") : d.start;
        return { ...d, nodes, start };
      });
      setSelectedNodeId(null);
    },
    [mutate],
  );

  const onConnect = useCallback(
    (c: Connection) => {
      if (!c.source || !c.target) return;
      mutate((d) => ({
        ...d,
        nodes: d.nodes.map((n) => {
          if (n.id !== c.source) return n;
          if (n.kind === "decision") {
            return n.on_pass ? { ...n, on_fail: c.target } : { ...n, on_pass: c.target };
          }
          if (n.kind === "parallel") {
            const b = Array.isArray(n.branches) ? n.branches : [];
            return { ...n, branches: [...new Set([...b, c.target!])] };
          }
          return { ...n, next: c.target };
        }),
      }));
    },
    [mutate],
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

  useEffect(() => {
    setRunResult(null);
    if (!selected) {
      setRecs([]);
      return;
    }
    getWorkflowRecommendations(token, selected).then(setRecs).catch(() => setRecs([]));
  }, [selected, token]);

  const onRun = useCallback(async () => {
    if (!selected || !task.trim()) return;
    setRunning(true);
    setError(null);
    setRunResult(null);
    try {
      setRunResult(await runWorkflow(token, selected, task));
    } catch (e) {
      setError(errMsg(e));
    } finally {
      setRunning(false);
    }
  }, [selected, task, token]);

  const onApplyRec = useCallback(
    async (id: string) => {
      if (!selected) return;
      try {
        await applyWorkflowRecommendation(token, selected, id);
        setDef((await getWorkflow(token, selected)) as unknown as WorkflowDef);
        setRecs(await getWorkflowRecommendations(token, selected));
        setNotice(t("workflows.recApplied"));
      } catch (e) {
        setError(errMsg(e));
      }
    },
    [selected, token, t],
  );

  const nodeIds = def?.nodes.map((n) => n.id) ?? [];
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
          <div className="p-2 text-sm text-muted-foreground">{t("workflows.empty")}</div>
        ) : (
          names.map((n) => (
            <button key={n} type="button" onClick={() => setSelected(n)}
              className={cn(
                "block w-full truncate rounded px-2 py-1 text-left text-sm hover:bg-accent",
                selected === n && "bg-accent font-medium",
              )}>
              {n}
            </button>
          ))
        )}
      </aside>

      <div className="flex h-full flex-1">
        <div className="flex h-full flex-1 flex-col">
          {recs.length > 0 && (
            <div className="flex flex-col gap-1 border-b bg-amber-500/10 px-3 py-2">
              {recs.map((r) => (
                <div key={r.id} className="flex items-center gap-2 text-sm">
                  <Lightbulb className="h-4 w-4 shrink-0 text-amber-600" aria-hidden />
                  <span className="flex-1 text-amber-700 dark:text-amber-300">
                    <span className="font-medium">{r.target_id}.{r.field}</span> — {r.reason}
                  </span>
                  <Button size="sm" variant="outline" onClick={() => onApplyRec(r.id)}>
                    apply
                  </Button>
                </div>
              ))}
            </div>
          )}
          <div className="relative flex-1">
            {def && (
              <div className="absolute left-2 top-2 z-10 flex items-center gap-2">
                <Button size="sm" variant="outline" onClick={() => addNode("work")}>
                  <Plus className="h-3.5 w-3.5" /> work
                </Button>
                <Button size="sm" variant="outline" onClick={() => addNode("decision")}>
                  <Plus className="h-3.5 w-3.5" /> decision
                </Button>
                {dirty && (
                  <Button size="sm" onClick={onSave} disabled={saving}>
                    {saving ? <Loader2 className="h-4 w-4 animate-spin" /> : t("workflows.save")}
                  </Button>
                )}
                {notice && <span className="text-sm text-emerald-600">{notice}</span>}
                {error && <span className="text-sm text-destructive">{error}</span>}
              </div>
            )}
            {def ? (
              <ReactFlow
                nodes={flow.nodes}
                edges={flow.edges}
                nodeTypes={nodeTypes}
                fitView
                nodesDraggable={false}
                onConnect={onConnect}
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
          {def && (
            <div className="border-t p-2">
              <div className="flex items-center gap-2">
                <Input
                  value={task}
                  onChange={(e) => setTask(e.target.value)}
                  placeholder={t("workflows.taskPlaceholder")}
                  className="flex-1"
                />
                <Button size="sm" onClick={onRun} disabled={running || !task.trim()}>
                  {running ? (
                    <Loader2 className="h-4 w-4 animate-spin" />
                  ) : (
                    <>
                      <Play className="h-3.5 w-3.5" /> {t("workflows.run")}
                    </>
                  )}
                </Button>
              </div>
              {runResult && (
                <div className="mt-2 max-h-48 overflow-y-auto rounded border p-2 text-xs">
                  <div className="mb-1 font-medium">
                    {t("workflows.status")}: {runResult.status}
                  </div>
                  <div className="mb-1 flex flex-wrap gap-1">
                    {runResult.runs.map((r, i) => (
                      <span
                        key={i}
                        className={cn(
                          "rounded px-1.5 py-0.5",
                          r.passed === false
                            ? "bg-destructive/10 text-destructive"
                            : "bg-muted",
                        )}
                      >
                        {r.node_id}#{r.iteration}
                        {r.passed === true ? " ✓" : r.passed === false ? " ✗" : ""}
                      </span>
                    ))}
                  </div>
                  <div className="whitespace-pre-wrap text-muted-foreground">
                    {runResult.final_output}
                  </div>
                </div>
              )}
            </div>
          )}
        </div>

        {selectedNode && (
          <aside className="w-72 shrink-0 overflow-y-auto border-l p-3">
            <NodeConfigPanel
              node={selectedNode}
              nodeIds={nodeIds}
              isStart={def?.start === selectedNode.id}
              onChange={updateNode}
              onMakeStart={() => mutate((d) => ({ ...d, start: selectedNode.id }))}
              onDelete={() => deleteNode(selectedNode.id)}
            />
          </aside>
        )}
      </div>
    </div>
  );
}

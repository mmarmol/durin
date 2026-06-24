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
  listPersonas,
  runWorkflow,
  saveWorkflow,
  type PersonaItem,
  type WorkflowRecommendation,
  type WorkflowRunResult,
} from "@/lib/api";
import {
  workflowToFlow,
  type FlowNodeData,
  type IODescriptor,
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

function isRouting(node: WorkflowNodeDef): boolean {
  return node.on_pass != null || node.on_fail != null;
}

// A routing node always presents as a decision (amber ring + "decision" badge)
// regardless of its stored kind, so a new kind:"work"+routing node matches a
// legacy kind:"decision" node and the pass/fail edges it draws.
function displayKind(node: WorkflowNodeDef): string {
  return isRouting(node) ? "decision" : node.kind;
}

function nodeSummary(node: WorkflowNodeDef): string {
  if (isRouting(node)) return node.command != null ? "command" : "judge";
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
  const kind = displayKind(node);
  return (
    <div
      className={cn(
        "min-w-[150px] rounded-md border bg-background px-3 py-2",
        KIND_RING[kind] ?? "border-border",
        (isStart || selected) && "ring-2 ring-primary",
      )}
    >
      <Handle type="target" position={Position.Left} />
      <div className="text-[10px] uppercase tracking-wide text-muted-foreground">
        {kind}{isStart ? " · start" : ""}
      </div>
      <div className="text-sm font-medium">{node.id}</div>
      <div className="text-xs text-muted-foreground">{nodeSummary(node)}</div>
      <Handle type="source" position={Position.Right} />
    </div>
  );
}

function ioTags(desc: IODescriptor): string {
  const parts: string[] = [];
  if (desc.text) parts.push("text");
  if (desc.file) parts.push("file");
  return parts.length > 0 ? parts.join(" · ") : "any";
}

function IOCard({ data }: NodeProps) {
  const d = data as unknown as { input?: IODescriptor; output?: IODescriptor };
  const isInput = d.input != null;
  const desc = isInput ? d.input! : d.output!;
  return (
    <div
      className={cn(
        "flex items-center gap-2 rounded-full border px-4 py-1.5 text-xs font-medium shadow-sm",
        isInput
          ? "border-teal-400/70 bg-teal-50 text-teal-700 dark:bg-teal-950/40 dark:text-teal-300"
          : "border-orange-400/70 bg-orange-50 text-orange-700 dark:bg-orange-950/40 dark:text-orange-300",
      )}
    >
      {isInput ? null : <Handle type="target" position={Position.Left} />}
      <span className="uppercase tracking-widest opacity-60">{isInput ? "INPUT" : "OUTPUT"}</span>
      <span className="opacity-80">{ioTags(desc)}</span>
      {isInput ? <Handle type="source" position={Position.Right} /> : null}
    </div>
  );
}

const nodeTypes = {
  work: NodeCard,
  decision: NodeCard,
  parallel: NodeCard,
  subworkflow: NodeCard,
  input_obj: IOCard,
  output_obj: IOCard,
};

const MODES = ["build", "plan", "explore"];
const CONTEXTS = ["own", "shared"];
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

// Value used in the "runs as" picker to mean the node has no persona/model override.
const RUNS_AS_DEFAULT = "__default__";
// Value used to indicate the user typed a specific model string.
const RUNS_AS_MODEL = "__model__";
// Prefix used to encode persona names in the picker value.
const RUNS_AS_PERSONA_PREFIX = "persona:";

function NodeConfigPanel({
  node,
  nodeIds,
  isStart,
  personas,
  onChange,
  onMakeStart,
  onDelete,
}: {
  node: WorkflowNodeDef;
  nodeIds: string[];
  isStart: boolean;
  personas: PersonaItem[];
  onChange: (patch: Partial<WorkflowNodeDef>) => void;
  onMakeStart: () => void;
  onDelete: () => void;
}) {
  const { t } = useTranslation();
  const others = nodeIds.filter((id) => id !== node.id);

  // A node routes when on_pass or on_fail is set (regardless of kind).
  const routes = node.on_pass != null || node.on_fail != null;
  // body: "command" if the command field is present, else "agent"
  const body: "agent" | "command" = node.command != null ? "command" : "agent";

  // Determine the current "runs as" picker value.
  // Persona takes precedence; if a model string is set, show model entry; else default.
  const currentPersona = node.persona as string | undefined;
  const currentModel = node.model as string | undefined;
  let runsAsValue: string;
  if (currentPersona) {
    runsAsValue = RUNS_AS_PERSONA_PREFIX + currentPersona;
  } else if (currentModel) {
    runsAsValue = RUNS_AS_MODEL;
  } else {
    runsAsValue = RUNS_AS_DEFAULT;
  }

  function handleBodyToggle(newBody: "agent" | "command") {
    if (newBody === "command") {
      // Switch to command: set command to empty string, clear agent-only fields.
      onChange({ command: "", mode: undefined, model: undefined, persona: undefined, prompt: undefined });
    } else {
      // Switch to agent: clear command, restore agent defaults.
      onChange({ command: undefined, mode: "build" });
    }
  }

  function handleRunsAsChange(val: string) {
    if (val === RUNS_AS_DEFAULT) {
      onChange({ model: undefined, persona: undefined });
    } else if (val === RUNS_AS_MODEL) {
      // Keep any existing model string, just clear persona.
      onChange({ persona: undefined });
    } else if (val.startsWith(RUNS_AS_PERSONA_PREFIX)) {
      const name = val.slice(RUNS_AS_PERSONA_PREFIX.length);
      onChange({ persona: name, model: undefined });
    }
  }

  function toggleRoutes(on: boolean) {
    if (on) {
      onChange({ on_pass: null, on_fail: null, next: undefined });
    } else {
      onChange({ on_pass: undefined, on_fail: undefined, next: null });
    }
  }

  const routingToggleId = `routing-toggle-${node.id}`;

  return (
    <div className="flex flex-col gap-3">
      <div className="flex items-center gap-2">
        <span className="rounded bg-muted px-1.5 py-0.5 text-[10px] uppercase">
          {displayKind(node)}
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

      {(node.kind === "work" || node.kind === "decision") && (
        <>
          {/* Body toggle: agent vs command */}
          <Field label={t("workflows.body")}>
            <select
              className={selectCls}
              value={body}
              onChange={(e) => handleBodyToggle(e.target.value as "agent" | "command")}
            >
              <option value="agent">{t("workflows.bodyAgent")}</option>
              <option value="command">{t("workflows.bodyCommand")}</option>
            </select>
          </Field>

          {body === "command" ? (
            <Field label={t("workflows.command")}>
              <Textarea
                rows={3}
                value={(node.command as string) ?? ""}
                onChange={(e) => onChange({ command: e.target.value })}
              />
            </Field>
          ) : (
            <>
              {/* Runs as: default model / specific model / personas */}
              <Field label={t("workflows.runsAs")}>
                <select
                  className={selectCls}
                  value={runsAsValue}
                  onChange={(e) => handleRunsAsChange(e.target.value)}
                >
                  <option value={RUNS_AS_DEFAULT}>{t("workflows.runsAsDefault")}</option>
                  <option value={RUNS_AS_MODEL}>{t("workflows.runsAsModel")}</option>
                  {personas.length > 0 && (
                    <optgroup label={t("workflows.runsAsPersonasGroup")}>
                      {personas.map((p) => (
                        <option key={p.name} value={RUNS_AS_PERSONA_PREFIX + p.name}>
                          {p.name}
                          {p.description ? ` — ${p.description}` : ""}
                        </option>
                      ))}
                    </optgroup>
                  )}
                </select>
              </Field>

              {/* Show model text input only when specific model is selected */}
              {runsAsValue === RUNS_AS_MODEL && (
                <Field label={t("workflows.modelId")}>
                  <Input
                    value={currentModel ?? ""}
                    placeholder={t("workflows.modelPlaceholder")}
                    onChange={(e) => onChange({ model: e.target.value || undefined })}
                  />
                </Field>
              )}

              <Field label={t("workflows.mode")}>
                <select
                  className={selectCls}
                  value={(node.mode as string) ?? "build"}
                  onChange={(e) => onChange({ mode: e.target.value })}
                >
                  {MODES.map((m) => <option key={m} value={m}>{m}</option>)}
                </select>
              </Field>

              <Field label={t("workflows.context")}>
                <select
                  className={selectCls}
                  value={(node.context as string) ?? "own"}
                  onChange={(e) => onChange({ context: e.target.value })}
                >
                  {CONTEXTS.map((c) => <option key={c} value={c}>{c}</option>)}
                </select>
              </Field>

              <Field label={t("workflows.prompt")}>
                <Textarea
                  rows={5}
                  value={(node.prompt as string) ?? ""}
                  onChange={(e) => onChange({ prompt: e.target.value })}
                />
              </Field>
            </>
          )}

          {/* Routing toggle — use explicit id/htmlFor to avoid any label nesting issue,
              and a div wrapper so no outer label can capture the click. */}
          <div className="flex items-center gap-2">
            <input
              id={routingToggleId}
              type="checkbox"
              className="h-4 w-4 cursor-pointer accent-primary"
              checked={routes}
              onChange={(e) => toggleRoutes(e.target.checked)}
            />
            <label
              htmlFor={routingToggleId}
              className="cursor-pointer select-none text-xs text-muted-foreground"
            >
              {t("workflows.routes")}
            </label>
          </div>

          {routes ? (
            <>
              <Field label={t("workflows.onPass")}>
                <TargetSelect
                  value={node.on_pass as string | null}
                  options={others}
                  onChange={(v) => onChange({ on_pass: v })}
                />
              </Field>
              <Field label={t("workflows.onFail")}>
                <TargetSelect
                  value={node.on_fail as string | null}
                  options={others}
                  onChange={(v) => onChange({ on_fail: v })}
                />
              </Field>
            </>
          ) : (
            <Field label={t("workflows.next")}>
              <TargetSelect
                value={node.next as string}
                options={others}
                onChange={(v) => onChange({ next: v })}
              />
            </Field>
          )}
        </>
      )}

      {(node.kind === "parallel" || node.kind === "subworkflow") && (
        <Field label={t("workflows.next")}>
          <TargetSelect
            value={node.next as string}
            options={others}
            onChange={(v) => onChange({ next: v })}
          />
        </Field>
      )}

      <button
        type="button"
        className="mt-1 flex items-center gap-1.5 self-start text-xs text-destructive hover:underline"
        onClick={onDelete}
      >
        <Trash2 className="h-3.5 w-3.5" /> {t("workflows.deleteNode")}
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
  const [creating, setCreating] = useState(false);
  const [newName, setNewName] = useState("");
  const [personas, setPersonas] = useState<PersonaItem[]>([]);

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
    listPersonas(token).then((r) => setPersonas(r.personas)).catch(() => setPersonas([]));
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

  const addNode = useCallback(() => {
    const id = `node-${++_idSeq}`;
    const node: WorkflowNodeDef = { id, kind: "work", mode: "build", prompt: "", next: null };
    mutate((d) => ({ ...d, nodes: [...d.nodes, node] }));
    setSelectedNodeId(id);
  }, [mutate]);

  const createWorkflow = useCallback(async () => {
    const name = newName.trim();
    if (!name) return;
    if (names.includes(name)) {
      setError(t("workflows.nameExists"));
      return;
    }
    // Minimal valid graph: one work node, which is also the start. The user edits from there.
    const fresh: WorkflowDef = {
      name,
      start: "start",
      nodes: [{ id: "start", kind: "work", mode: "build", prompt: "", next: null }],
    };
    setSaving(true);
    setError(null);
    try {
      await saveWorkflow(token, name, fresh);
      setNames((ns) => Array.from(new Set([...ns, name])).sort());
      setCreating(false);
      setNewName("");
      setSelected(name); // the [selected] effect loads it and renders the canvas
    } catch (e) {
      setError(errMsg(e));
    } finally {
      setSaving(false);
    }
  }, [newName, names, token, t]);

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
          // A node with on_pass/on_fail (routing node) — fill the empty slot first.
          const isRouting = n.on_pass != null || n.on_fail != null;
          if (n.kind === "decision" || isRouting) {
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
          <span className="flex-1">{t("workflows.title")}</span>
          <button
            type="button"
            onClick={() => {
              setCreating((c) => !c);
              setNewName("");
              setError(null);
            }}
            className="rounded p-1 text-muted-foreground hover:bg-accent hover:text-foreground"
            title={t("workflows.new")}
            aria-label={t("workflows.new")}
          >
            <Plus className="h-4 w-4" />
          </button>
        </div>
        {creating && (
          <div className="px-2 pb-1">
            <Input
              autoFocus
              value={newName}
              onChange={(e) => setNewName(e.target.value)}
              placeholder={t("workflows.newPlaceholder")}
              disabled={saving}
              onKeyDown={(e) => {
                if (e.key === "Enter") void createWorkflow();
                if (e.key === "Escape") {
                  setCreating(false);
                  setNewName("");
                }
              }}
              className="h-8 text-sm"
            />
          </div>
        )}
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
                <Button size="sm" variant="outline" onClick={addNode}>
                  <Plus className="h-3.5 w-3.5" /> {t("workflows.addNode")}
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
              personas={personas}
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

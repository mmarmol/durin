import { useCallback, useEffect, useState } from "react";
import {
  Background,
  Controls,
  Handle,
  Position,
  ReactFlow,
  useEdgesState,
  useNodesState,
  type Connection,
  type Edge,
  type Node,
  type NodeProps,
} from "@xyflow/react";
import "@xyflow/react/dist/style.css";
import { Check, Copy, Lightbulb, Loader2, Play, Plus, Trash2, Workflow as WorkflowIcon } from "lucide-react";
import { useTranslation } from "react-i18next";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import {
  ApiError,
  applyWorkflowRecommendation,
  deleteWorkflow,
  getWorkflow,
  getWorkflowRecommendations,
  listWorkflows,
  listPersonas,
  runWorkflow,
  saveWorkflow,
  type PersonaItem,
  type WorkflowRecommendation,
  type WorkflowRunNode,
  type WorkflowRunResult,
} from "@/lib/api";
import {
  safeSubflowTargets,
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
  parallel: "border-violet-400/70",
  subflow: "border-sky-400/70",
};

// Maps a stored node kind to the i18n key suffix used for display labels.
// Both "work" and the legacy "decision" alias present as "work" to the user;
// routing is shown only by the presence of pass/fail edges, never by a badge.
export function kindLabelKey(kind: string): string {
  if (kind === "decision" || kind === "work") return "work";
  if (kind === "subworkflow") return "subflow";
  return kind; // "parallel"
}

function nodeSummary(node: WorkflowNodeDef): string {
  if (node.command != null) return "command";
  if (node.kind === "parallel") return node.worker ? "dynamic · ×N" : `${((node.branches as string[]) ?? []).length} branches`;
  if (node.kind === "subworkflow") return String(node.workflow ?? "");
  return `${(node.mode as string) ?? "build"} · ${(node.model as string) ?? "default"}`;
}

function NodeCard({ data, selected }: NodeProps) {
  const { t } = useTranslation();
  const { node, isStart } = data as unknown as FlowNodeData;
  const isDynamicWorker = !!(data as Record<string, unknown>).dynamicWorker;
  const labelKey = kindLabelKey(node.kind);
  return (
    <div
      className={cn(
        "min-w-[150px] rounded-md border bg-background px-3 py-2",
        KIND_RING[labelKey] ?? "border-border",
        (isStart || selected) && "ring-2 ring-primary",
        isDynamicWorker && "ring-1 ring-violet-400/60",
      )}
    >
      <Handle type="target" position={Position.Left} />
      <div className="flex items-center gap-1 text-[10px] uppercase tracking-wide text-muted-foreground">
        <span>{t("workflows.kind." + labelKey)}{isStart ? " · " + t("workflows.start") : ""}</span>
        {isDynamicWorker && (
          <span className="rounded bg-violet-100 px-1 py-0.5 text-[9px] font-medium text-violet-700 dark:bg-violet-900/40 dark:text-violet-300">
            {t("workflows.dynamicBadge")}
          </span>
        )}
      </div>
      <div className="text-sm font-medium">{node.id}</div>
      <div className="text-xs text-muted-foreground">{nodeSummary(node)}</div>
      <Handle type="source" position={Position.Right} />
    </div>
  );
}

function ioTags(desc: IODescriptor, t: (k: string) => string): string {
  const parts: string[] = [];
  if (desc.text) parts.push(t("workflows.ioText"));
  if (desc.file) parts.push(t("workflows.ioFile"));
  return parts.length > 0 ? parts.join(" · ") : t("workflows.ioAny");
}

function IOCard({ data, selected }: NodeProps) {
  const { t } = useTranslation();
  const d = data as unknown as { input?: IODescriptor; output?: IODescriptor };
  const isInput = d.input != null;
  const desc = isInput ? d.input! : d.output!;
  return (
    <div
      className={cn(
        "flex cursor-pointer items-center gap-2 rounded-full border px-4 py-1.5 text-xs font-medium shadow-sm",
        isInput
          ? "border-teal-400/70 bg-teal-50 text-teal-700 dark:bg-teal-950/40 dark:text-teal-300"
          : "border-orange-400/70 bg-orange-50 text-orange-700 dark:bg-orange-950/40 dark:text-orange-300",
        selected && "ring-2 ring-primary",
      )}
    >
      {isInput ? null : <Handle type="target" position={Position.Left} />}
      <span className="uppercase tracking-widest opacity-60">{t("workflows.kind." + (isInput ? "input" : "output"))}</span>
      <span className="opacity-80">{ioTags(desc, t)}</span>
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
  const { t } = useTranslation();
  return (
    <select
      className={selectCls}
      value={value ?? ""}
      onChange={(e) => onChange(e.target.value || null)}
    >
      <option value="">{t("workflows.caseTargetEnd")}</option>
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

function PromptEditorModal({ value, onChange, onClose }: { value: string; onChange: (v: string) => void; onClose: () => void }) {
  const { t } = useTranslation();
  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 p-6" onClick={onClose}>
      <div className="flex h-full w-full max-w-3xl flex-col rounded-lg border bg-background p-4" onClick={(e) => e.stopPropagation()}>
        <div className="mb-2 flex items-center justify-between">
          <span className="text-sm font-medium">{t("workflows.prompt")}</span>
          <button type="button" className="text-sm text-muted-foreground hover:text-foreground" onClick={onClose}>{t("workflows.done")}</button>
        </div>
        <Textarea autoFocus className="flex-1 resize-none font-mono text-sm" value={value} onChange={(e) => onChange(e.target.value)} />
      </div>
    </div>
  );
}

// Editable list of { label, target } rows for multi-way routing via the cases field.
// Each row maps a case label (the agent's verdict word) to a target node id or null (end).
// Labels must be non-empty and unique; duplicates are prevented on edit.
function CasesEditor({
  cases,
  options,
  onChange,
  t,
}: {
  cases: Record<string, string | null>;
  options: string[];
  onChange: (cases: Record<string, string | null>) => void;
  t: (k: string) => string;
}) {
  const entries = Object.entries(cases);

  function setLabel(oldLabel: string, newLabel: string) {
    // Refuse the rename when it would create a duplicate label.
    if (newLabel !== oldLabel && newLabel in cases) return;
    const next: Record<string, string | null> = {};
    for (const [k, v] of entries) {
      next[k === oldLabel ? newLabel : k] = v;
    }
    onChange(next);
  }

  function setTarget(label: string, target: string | null) {
    onChange({ ...cases, [label]: target });
  }

  function addCase() {
    // Find a label name that does not collide with existing ones.
    let name = "";
    let i = entries.length + 1;
    while (name === "" || name in cases) {
      name = `case${i++}`;
    }
    onChange({ ...cases, [name]: null });
  }

  function removeCase(label: string) {
    const next = { ...cases };
    delete next[label];
    onChange(next);
  }

  return (
    <div className="flex flex-col gap-2">
      <span className="text-xs text-muted-foreground">{t("workflows.cases")}</span>
      {entries.map(([label, target]) => (
        <div key={label} className="flex items-center gap-1">
          <Input
            value={label}
            placeholder={t("workflows.caseLabelPlaceholder")}
            className="h-7 flex-1 text-xs"
            onChange={(e) => setLabel(label, e.target.value)}
          />
          <span className="text-xs text-muted-foreground">→</span>
          <select
            className={`${selectCls} h-7 flex-1`}
            value={target ?? ""}
            onChange={(e) => setTarget(label, e.target.value || null)}
          >
            <option value="">{t("workflows.caseTargetEnd")}</option>
            {options.map((o) => (
              <option key={o} value={o}>{o}</option>
            ))}
          </select>
          <button
            type="button"
            className="shrink-0 p-1 text-muted-foreground hover:text-destructive"
            onClick={() => removeCase(label)}
            aria-label={t("workflows.caseRemove")}
          >
            <Trash2 className="h-3.5 w-3.5" />
          </button>
        </div>
      ))}
      <button
        type="button"
        className="flex items-center gap-1 self-start text-xs text-muted-foreground hover:text-foreground"
        onClick={addCase}
      >
        <Plus className="h-3.5 w-3.5" /> {t("workflows.caseAdd")}
      </button>
    </div>
  );
}

function NodeConfigPanel({
  node,
  nodeIds,
  isStart,
  personas,
  allWorkflowNames,
  currentWorkflowName,
  token,
  onChange,
  onMakeStart,
  onDelete,
}: {
  node: WorkflowNodeDef;
  nodeIds: string[];
  isStart: boolean;
  personas: PersonaItem[];
  allWorkflowNames: string[];
  currentWorkflowName: string;
  token: string;
  onChange: (patch: Partial<WorkflowNodeDef>) => void;
  onMakeStart: () => void;
  onDelete: () => void;
}) {
  const { t } = useTranslation();
  const [promptModalOpen, setPromptModalOpen] = useState(false);
  const others = nodeIds.filter((id) => id !== node.id);

  // Fetch subflow call-graph refs when this panel is open for a subworkflow node.
  // Hooks must be declared unconditionally; the fetch body is gated inside the effect.
  const [refs, setRefs] = useState<Record<string, string[]> | null>(null);
  useEffect(() => {
    if (node.kind !== "subworkflow") return;
    let alive = true;
    setRefs(null);
    (async () => {
      const entries = await Promise.all(
        allWorkflowNames.map(async (n) => {
          try {
            const d = (await getWorkflow(token, n)) as unknown as WorkflowDef;
            return [n, d.nodes.filter((x) => x.kind === "subworkflow").map((x) => String(x.workflow ?? ""))] as const;
          } catch {
            return [n, []] as const;
          }
        }),
      );
      if (alive) setRefs(Object.fromEntries(entries));
    })();
    return () => { alive = false; };
  }, [node.kind, allWorkflowNames, token]);

  // Determine the active routing shape: "binary" (on_pass/on_fail), "multiway" (cases), or "none" (next).
  const routingShape: "binary" | "multiway" | "none" =
    node.on_pass != null || node.on_fail != null
      ? "binary"
      : node.cases != null
        ? "multiway"
        : "none";
  const routes = routingShape !== "none";
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
      // Also clear multi-way cases — a command node routes only on its exit code
      // (binary on_pass/on_fail), never on an emitted label.
      onChange({ command: "", mode: undefined, model: undefined, persona: undefined, prompt: undefined, cases: undefined });
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

  function switchRoutingShape(shape: "none" | "binary" | "multiway") {
    // Always clear all three shapes first, then apply the selected one.
    const clear: Partial<WorkflowNodeDef> = { on_pass: undefined, on_fail: undefined, cases: undefined, next: undefined };
    if (shape === "none") {
      onChange({ ...clear, next: null });
    } else if (shape === "binary") {
      onChange({ ...clear, on_pass: null, on_fail: null, mode: "explore" });
    } else {
      // multiway: start with one empty case row
      onChange({ ...clear, cases: { "case1": null }, mode: "explore" });
    }
  }

  const routingToggleId = `routing-toggle-${node.id}`;

  return (
    <div className="flex flex-col gap-3">
      <div className="flex items-center gap-2">
        <span className="rounded bg-muted px-1.5 py-0.5 text-[10px] uppercase">
          {t("workflows.kind." + kindLabelKey(node.kind))}
        </span>
        <span className="text-sm font-medium">{node.id}</span>
        {!isStart && (
          <button
            type="button"
            className="ml-auto text-xs text-muted-foreground hover:text-foreground"
            onClick={onMakeStart}
          >
            {t("workflows.setAsStart")}
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

              <div className="flex-1 min-h-0 flex flex-col gap-1">
                <div className="flex items-center justify-between">
                  <span className="text-xs text-muted-foreground">{t("workflows.prompt")}</span>
                  <button
                    type="button"
                    className="text-xs text-muted-foreground hover:text-foreground"
                    onClick={() => setPromptModalOpen(true)}
                  >
                    {t("workflows.expand")}
                  </button>
                </div>
                <Textarea
                  className="flex-1 resize-none"
                  value={(node.prompt as string) ?? ""}
                  onChange={(e) => onChange({ prompt: e.target.value })}
                />
              </div>
              {promptModalOpen && (
                <PromptEditorModal
                  value={(node.prompt as string) ?? ""}
                  onChange={(v) => onChange({ prompt: v })}
                  onClose={() => setPromptModalOpen(false)}
                />
              )}
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
              onChange={(e) => switchRoutingShape(e.target.checked ? "binary" : "none")}
            />
            <label
              htmlFor={routingToggleId}
              className="cursor-pointer select-none text-xs text-muted-foreground"
            >
              {t("workflows.routes")}
            </label>
          </div>

          {routes && (
            <Field label={t("workflows.routingShape")}>
              <select
                className={selectCls}
                value={routingShape}
                onChange={(e) => switchRoutingShape(e.target.value as "binary" | "multiway")}
              >
                <option value="binary">{t("workflows.routingShapeBinary")}</option>
                {/* A command node routes only on its exit code, so multi-way (label-based) is agent-only. */}
                {body !== "command" && (
                  <option value="multiway">{t("workflows.routingShapeMultiway")}</option>
                )}
              </select>
            </Field>
          )}

          {routingShape === "binary" && (
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
          )}

          {routingShape === "multiway" && (
            <CasesEditor
              cases={(node.cases as Record<string, string | null>) ?? {}}
              options={others}
              onChange={(cases) => onChange({ cases })}
              t={t}
            />
          )}

          {routingShape === "none" && (
            <Field label={t("workflows.next")}>
              <TargetSelect
                value={node.next as string}
                options={others}
                onChange={(v) => onChange({ next: v })}
              />
            </Field>
          )}

          <Field label={t("workflows.maxVisits")}>
            <Input
              type="number"
              min={1}
              value={(node.max_visits as number | undefined) ?? ""}
              placeholder={t("workflows.maxVisitsHint")}
              onChange={(e) => {
                const n = parseInt(e.target.value, 10);
                onChange({ max_visits: e.target.value === "" || !Number.isFinite(n) ? undefined : Math.max(1, n) });
              }}
              className="h-8"
            />
          </Field>
        </>
      )}

      {node.kind === "parallel" && (() => {
        const isDynamic = typeof node.worker === "string" && node.worker !== "";
        const parallelMode = isDynamic ? "dynamic" : "static";

        function handleParallelModeChange(mode: "static" | "dynamic") {
          if (mode === "dynamic") {
            onChange({ worker: null, list_from: null, branches: undefined });
          } else {
            onChange({ branches: [], worker: undefined, list_from: undefined });
          }
        }

        const branchList: string[] = Array.isArray(node.branches) ? (node.branches as string[]) : [];

        function toggleBranch(id: string, checked: boolean) {
          const next = checked
            ? [...new Set([...branchList, id])]
            : branchList.filter((b) => b !== id);
          onChange({ branches: next });
        }

        return (
          <>
            {/* Mode toggle */}
            <Field label={t("workflows.parallelMode")}>
              <select
                className={selectCls}
                value={parallelMode}
                onChange={(e) => handleParallelModeChange(e.target.value as "static" | "dynamic")}
              >
                <option value="static">{t("workflows.parallelStatic")}</option>
                <option value="dynamic">{t("workflows.parallelDynamic")}</option>
              </select>
            </Field>

            {parallelMode === "static" ? (
              <Field label={t("workflows.parallelBranches")}>
                <div className="flex flex-col gap-1">
                  {others.length === 0 ? (
                    <span className="text-xs text-muted-foreground">(no other nodes)</span>
                  ) : (
                    others.map((id) => (
                      <label key={id} className="flex items-center gap-2 text-xs">
                        <input
                          type="checkbox"
                          className="h-3.5 w-3.5 cursor-pointer accent-primary"
                          checked={branchList.includes(id)}
                          onChange={(e) => toggleBranch(id, e.target.checked)}
                        />
                        {id}
                      </label>
                    ))
                  )}
                </div>
              </Field>
            ) : (
              <>
                <Field label={t("workflows.parallelWorker")}>
                  <TargetSelect
                    value={node.worker as string | null}
                    options={others}
                    onChange={(v) => onChange({ worker: v })}
                  />
                </Field>
                <Field label={t("workflows.parallelListFrom")}>
                  <TargetSelect
                    value={node.list_from as string | null}
                    options={others}
                    onChange={(v) => onChange({ list_from: v })}
                  />
                </Field>
              </>
            )}

            {(parallelMode === "static"
              ? branchList.length === 0
              : !(node.worker && node.list_from)) && (
              <span className="text-xs text-amber-600">
                {parallelMode === "static"
                  ? t("workflows.parallelNeedBranch")
                  : t("workflows.parallelNeedWorkerList")}
              </span>
            )}

            {/* Common: max_concurrency, next (merge), reconcile */}
            <Field label={t("workflows.parallelMaxConcurrency")}>
              <Input
                type="number"
                min={1}
                value={(node.max_concurrency as number) ?? 2}
                onChange={(e) => onChange({ max_concurrency: Math.max(1, parseInt(e.target.value, 10) || 2) })}
                className="h-8"
              />
            </Field>

            <Field label={t("workflows.next")}>
              <TargetSelect
                value={node.next as string | null}
                options={others}
                onChange={(v) => onChange({ next: v })}
              />
            </Field>

            {parallelMode === "static" && (
              <Field label={t("workflows.parallelReconcile")}>
                <select
                  className={selectCls}
                  value={(node.reconcile as string) ?? "read"}
                  onChange={(e) => onChange({ reconcile: e.target.value as "read" | "union" })}
                >
                  <option value="read">read</option>
                  <option value="union">union</option>
                </select>
              </Field>
            )}
          </>
        );
      })()}

      {node.kind === "subworkflow" && (() => {
        const safeTargets = refs ? safeSubflowTargets(currentWorkflowName, refs) : [];
        return (
          <>
            <Field label={t("workflows.subflowTarget")}>
              <select
                className={selectCls}
                value={(node.workflow as string) ?? ""}
                disabled={refs === null}
                onChange={(e) => onChange({ workflow: e.target.value })}
              >
                <option value="">(none)</option>
                {safeTargets.map((n) => (
                  <option key={n} value={n}>{n}</option>
                ))}
              </select>
            </Field>
            <Field label={t("workflows.next")}>
              <TargetSelect
                value={node.next as string}
                options={others}
                onChange={(v) => onChange({ next: v })}
              />
            </Field>
          </>
        );
      })()}

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

// Config panel for an INPUT/OUTPUT canvas object: toggles whether the workflow accepts
// (input) or produces (output) text and/or files. Editing patches def.input / def.output.
function IOConfigPanel({
  which,
  desc,
  onChange,
  onRemove,
}: {
  which: "input" | "output";
  desc: IODescriptor;
  onChange: (patch: IODescriptor) => void;
  onRemove: () => void;
}) {
  const { t } = useTranslation();
  const title = which === "input" ? t("workflows.ioInputTitle") : t("workflows.ioOutputTitle");
  const hint = which === "input" ? t("workflows.ioInputHint") : t("workflows.ioOutputHint");
  return (
    <div className="flex flex-col gap-3">
      <div className="flex items-center gap-2">
        <span className="rounded bg-muted px-1.5 py-0.5 text-[10px] uppercase">{t("workflows.kind." + which)}</span>
        <span className="text-sm font-medium">{title}</span>
      </div>
      <p className="text-xs text-muted-foreground">{hint}</p>
      <label className="flex items-center gap-2 text-sm">
        <input
          type="checkbox"
          className="h-4 w-4 cursor-pointer accent-primary"
          checked={!!desc.text}
          onChange={(e) => onChange({ ...desc, text: e.target.checked })}
        />
        {t("workflows.ioText")}
      </label>
      <label className="flex items-center gap-2 text-sm">
        <input
          type="checkbox"
          className="h-4 w-4 cursor-pointer accent-primary"
          checked={!!desc.file}
          onChange={(e) => onChange({ ...desc, file: e.target.checked })}
        />
        {t("workflows.ioFile")}
      </label>
      <div className="flex-1 min-h-0 flex flex-col gap-1">
        <span className="text-xs text-muted-foreground">{t("workflows.ioDescription")}</span>
        <Textarea
          className="flex-1 resize-none"
          value={desc.description ?? ""}
          placeholder={t("workflows.ioDescriptionPlaceholder")}
          onChange={(e) => onChange({ ...desc, description: e.target.value || undefined })}
        />
      </div>
      <button
        type="button"
        className="mt-1 flex items-center gap-1.5 self-start text-xs text-destructive hover:underline"
        onClick={onRemove}
      >
        <Trash2 className="h-3.5 w-3.5" /> {t("workflows.removeIo")}
      </button>
    </div>
  );
}

// A node's run status mapped to a chip tone. "ok"/"no_session" are normal; the two
// failure statuses ("persist_failed", "node_failed") render in the destructive tone.
function statusTone(status: string): string {
  if (status === "node_failed" || status === "persist_failed") {
    return "bg-destructive/10 text-destructive";
  }
  if (status === "no_session") return "bg-muted text-muted-foreground";
  return "bg-emerald-500/10 text-emerald-700 dark:text-emerald-400";
}

// A workflow node session is headless (not a chat in the sidebar), so there is no
// in-app viewer to open it by key. Surface the key as a labelled, copyable reference
// so an auditor can look the session up by hand.
function CopyableKey({ value }: { value: string }) {
  const { t } = useTranslation();
  const [copied, setCopied] = useState(false);
  const onCopy = useCallback(() => {
    if (!navigator.clipboard) return;
    void navigator.clipboard.writeText(value).then(() => {
      setCopied(true);
      setTimeout(() => setCopied(false), 1_500);
    });
  }, [value]);
  return (
    <button
      type="button"
      onClick={onCopy}
      title={t("workflows.copySession")}
      aria-label={copied ? t("workflows.sessionCopied") : t("workflows.copySession")}
      className="inline-flex max-w-full items-center gap-1 rounded bg-muted px-1.5 py-0.5 font-mono text-[10px] text-muted-foreground hover:text-foreground"
    >
      {copied ? <Check className="h-3 w-3 shrink-0" /> : <Copy className="h-3 w-3 shrink-0" />}
      <span className="truncate">{value}</span>
    </button>
  );
}

// One per-node/worker row in a run's trace: identity (node_id#iteration, plus fan-out
// worker_index / static branch_id so concurrent units are legible), a status chip and
// route verdict, the copyable session key, and the node's (truncated) output.
function RunNodeRow({ run }: { run: WorkflowRunNode }) {
  const { t } = useTranslation();
  const verdict =
    run.route_label != null && run.route_label !== ""
      ? run.route_label
      : run.passed === true
        ? "✓"
        : run.passed === false
          ? "✗"
          : null;
  return (
    <div className="flex flex-col gap-1 rounded border px-2 py-1.5">
      <div className="flex flex-wrap items-center gap-1.5">
        <span className="font-mono text-[11px] font-medium">
          {run.node_id}#{run.iteration}
        </span>
        {run.worker_index != null && (
          <span className="rounded bg-violet-500/10 px-1 py-0.5 text-[10px] text-violet-700 dark:text-violet-300">
            {t("workflows.workerLabel", { index: run.worker_index })}
          </span>
        )}
        {run.branch_id && (
          <span className="rounded bg-sky-500/10 px-1 py-0.5 text-[10px] text-sky-700 dark:text-sky-300">
            {t("workflows.branchLabel", { id: run.branch_id })}
          </span>
        )}
        <span className={cn("rounded px-1 py-0.5 text-[10px]", statusTone(run.status))}>
          {t("workflows.runStatus." + run.status, run.status)}
        </span>
        {verdict != null && (
          <span
            className={cn(
              "rounded px-1 py-0.5 text-[10px]",
              run.passed === false ? "bg-destructive/10 text-destructive" : "bg-muted",
            )}
          >
            {verdict}
          </span>
        )}
        {run.session_key && <CopyableKey value={run.session_key} />}
      </div>
      {run.output && (
        <div className="whitespace-pre-wrap break-words text-[11px] text-muted-foreground">
          {run.output}
        </div>
      )}
    </div>
  );
}

// The detail view of a single run: the exhausted/incomplete banner (if any), a row per
// node/worker (status, output, session affordance), the final output and output folder.
function RunDetail({ result }: { result: WorkflowRunResult }) {
  const { t } = useTranslation();
  return (
    <div className="flex flex-col gap-2">
      {result.status !== "completed" && (
        <div className="rounded bg-amber-500/10 px-2 py-1.5 text-amber-700 dark:text-amber-400">
          <span className="font-medium">{t("workflows.exhausted")}</span>
          {result.exhausted_node && (
            <span className="ml-1">
              — {t("workflows.exhaustedNode")}: <span className="font-mono">{result.exhausted_node}</span>
            </span>
          )}
        </div>
      )}
      <div className="flex flex-col gap-1.5">
        {result.runs.map((run, i) => (
          <RunNodeRow key={`${run.node_id}#${run.iteration}#${i}`} run={run} />
        ))}
      </div>
      {result.final_output && (
        <div className="flex flex-col gap-0.5">
          <span className="text-[10px] uppercase tracking-wide text-muted-foreground">
            {t("workflows.finalOutput")}
          </span>
          <div className="whitespace-pre-wrap break-words text-muted-foreground">
            {result.final_output}
          </div>
        </div>
      )}
      {result.output_dir && (
        <div className="font-mono text-[11px] text-muted-foreground">
          {t("workflows.outputDir")}: {result.output_dir}
        </div>
      )}
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
  // Minimal run history: the runs triggered for the selected workflow this session,
  // newest-first. Clicking one re-shows its detail (the full result is kept in memory).
  const [runHistory, setRunHistory] = useState<WorkflowRunResult[]>([]);
  const [creating, setCreating] = useState(false);
  const [newName, setNewName] = useState("");
  const [personas, setPersonas] = useState<PersonaItem[]>([]);
  const [inputPaths, setInputPaths] = useState("");
  const [confirmDelete, setConfirmDelete] = useState<string | null>(null);
  const [runnerOpen, setRunnerOpen] = useState(false);

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

  // React Flow owns node/edge state so a node follows the cursor during a drag
  // (via onNodesChange). The state is seeded from the def whenever the graph changes;
  // a drag's drop is persisted to def.ui.positions in onNodeDragStop, which re-seeds
  // here with the stored position so the node stays put.
  const [rfNodes, setRfNodes, onNodesChange] = useNodesState<Node>([]);
  const [rfEdges, setRfEdges, onEdgesChange] = useEdgesState<Edge>([]);
  useEffect(() => {
    const f = def ? workflowToFlow(def) : { nodes: [], edges: [] };
    setRfNodes(f.nodes);
    setRfEdges(f.edges);
  }, [def, setRfNodes, setRfEdges]);

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

  const addParallelNode = useCallback(() => {
    const id = `parallel-${++_idSeq}`;
    const node: WorkflowNodeDef = { id, kind: "parallel", reconcile: "read", max_concurrency: 2, branches: [], next: null };
    mutate((d) => ({ ...d, nodes: [...d.nodes, node] }));
    setSelectedNodeId(id);
  }, [mutate]);

  const addSubflowNode = useCallback(() => {
    const id = `subflow-${++_idSeq}`;
    const node: WorkflowNodeDef = { id, kind: "subworkflow", workflow: "", next: null };
    mutate((d) => ({ ...d, nodes: [...d.nodes, node] }));
    setSelectedNodeId(id);
  }, [mutate]);

  // Add / edit / remove the workflow's INPUT or OUTPUT descriptor. Adding defaults to
  // text and selects the new canvas object so the config panel opens for refinement.
  const addIo = useCallback(
    (which: "input" | "output") => {
      mutate((d) => ({ ...d, [which]: d[which] ?? { text: true } }));
      setSelectedNodeId(which === "input" ? "__input__" : "__output__");
    },
    [mutate],
  );

  const setIo = useCallback(
    (which: "input" | "output", patch: IODescriptor) => {
      mutate((d) => ({ ...d, [which]: patch }));
    },
    [mutate],
  );

  const removeIo = useCallback(
    (which: "input" | "output") => {
      mutate((d) => {
        const next = { ...d };
        delete next[which];
        return next;
      });
      setSelectedNodeId(null);
    },
    [mutate],
  );

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

  const onDeleteWorkflow = useCallback(
    async (name: string) => {
      setError(null);
      try {
        await deleteWorkflow(token, name);
        setNames((ns) => ns.filter((x) => x !== name));
        setConfirmDelete(null);
        if (selected === name) setSelected(null);
      } catch (e) {
        setError(errMsg(e));
      }
    },
    [token, selected],
  );

  const deleteNode = useCallback(
    (id: string) => {
      mutate((d) => {
        const nodes = d.nodes
          .filter((n) => n.id !== id)
          .map((n) => {
            const patch: Partial<WorkflowNodeDef> = {
              next: n.next === id ? null : n.next,
              on_pass: n.on_pass === id ? null : n.on_pass,
              on_fail: n.on_fail === id ? null : n.on_fail,
              branches: Array.isArray(n.branches)
                ? n.branches.filter((b) => b !== id)
                : n.branches,
            };
            // Remap any cases that pointed to the deleted node to null (end).
            if (n.cases != null) {
              const remapped: Record<string, string | null> = {};
              for (const [label, target] of Object.entries(n.cases)) {
                remapped[label] = target === id ? null : target;
              }
              patch.cases = remapped;
            }
            return { ...n, ...patch };
          });
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
          // Multi-way routing nodes: cases are authored in the panel, not by dragging edges.
          if (n.cases != null) return n;
          // A node with on_pass/on_fail (binary routing) — fill the empty slot first.
          const isRouting = n.on_pass != null || n.on_fail != null;
          if (n.kind === "decision" || isRouting) {
            return n.on_pass ? { ...n, on_fail: c.target } : { ...n, on_pass: c.target };
          }
          if (n.kind === "parallel") {
            // A DYNAMIC parallel (worker set) must keep branches empty — wiring is
            // panel-only (worker/list_from). Dragging an edge would contaminate it
            // and the backend would reject branches+worker together.
            if (typeof n.worker === "string" && n.worker !== "") return n;
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
    setRunHistory([]);
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
      const files = inputPaths.split("\n").map((s) => s.trim()).filter(Boolean);
      const result = await runWorkflow(token, selected, task, files);
      setRunResult(result);
      // Newest-first; de-dupe by run_id so a re-render never doubles an entry.
      setRunHistory((prev) => [result, ...prev.filter((r) => r.run_id !== result.run_id)]);
    } catch (e) {
      setError(errMsg(e));
    } finally {
      setRunning(false);
    }
  }, [selected, task, token, inputPaths]);

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
  const selectedIo: "input" | "output" | null =
    selectedNodeId === "__input__" ? "input" : selectedNodeId === "__output__" ? "output" : null;

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
            <div
              key={n}
              className={cn(
                "group flex items-center gap-1 rounded pr-1 hover:bg-accent",
                selected === n && "bg-accent",
              )}
            >
              <button
                type="button"
                onClick={() => setSelected(n)}
                className={cn(
                  "min-w-0 flex-1 truncate px-2 py-1 text-left text-sm",
                  selected === n && "font-medium",
                )}
              >
                {n}
              </button>
              {confirmDelete === n ? (
                <span className="flex shrink-0 items-center gap-1 text-xs">
                  <button
                    type="button"
                    onClick={() => onDeleteWorkflow(n)}
                    className="rounded px-1 py-0.5 text-destructive hover:underline"
                  >
                    {t("workflows.confirmDelete")}
                  </button>
                  <button
                    type="button"
                    onClick={() => setConfirmDelete(null)}
                    className="rounded px-1 py-0.5 text-muted-foreground hover:underline"
                  >
                    {t("workflows.cancel")}
                  </button>
                </span>
              ) : (
                <button
                  type="button"
                  onClick={() => setConfirmDelete(n)}
                  title={t("workflows.deleteWorkflow")}
                  aria-label={t("workflows.deleteWorkflow")}
                  className="shrink-0 rounded p-1 text-muted-foreground opacity-0 hover:text-destructive focus:opacity-100 group-hover:opacity-100"
                >
                  <Trash2 className="h-3.5 w-3.5" />
                </button>
              )}
            </div>
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
                  <Plus className="h-3.5 w-3.5" /> {t("workflows.addWork")}
                </Button>
                <Button size="sm" variant="outline" onClick={addParallelNode}>
                  <Plus className="h-3.5 w-3.5" /> {t("workflows.addParallel")}
                </Button>
                <Button size="sm" variant="outline" onClick={addSubflowNode}>
                  <Plus className="h-3.5 w-3.5" /> {t("workflows.addSubflow")}
                </Button>
                {!def.input && (
                  <Button size="sm" variant="outline" onClick={() => addIo("input")}>
                    <Plus className="h-3.5 w-3.5" /> {t("workflows.addInput")}
                  </Button>
                )}
                {!def.output && (
                  <Button size="sm" variant="outline" onClick={() => addIo("output")}>
                    <Plus className="h-3.5 w-3.5" /> {t("workflows.addOutput")}
                  </Button>
                )}
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
                nodes={rfNodes}
                edges={rfEdges}
                onNodesChange={onNodesChange}
                onEdgesChange={onEdgesChange}
                nodeTypes={nodeTypes}
                fitView
                onConnect={onConnect}
                onNodeClick={(_, node) => setSelectedNodeId(node.id)}
                onPaneClick={() => setSelectedNodeId(null)}
                onNodeDragStop={(_, node) =>
                  mutate((d) => ({
                    ...d,
                    ui: {
                      ...d.ui,
                      positions: {
                        ...d.ui?.positions,
                        [node.id]: { x: Math.round(node.position.x), y: Math.round(node.position.y) },
                      },
                    },
                  }))
                }
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
            <div className="border-t">
              {!runnerOpen ? (
                <button
                  type="button"
                  className="w-full px-3 py-1.5 text-left text-xs text-muted-foreground hover:bg-accent"
                  onClick={() => setRunnerOpen(true)}
                >
                  ▸ {t("workflows.testTitle")}
                </button>
              ) : (
                <div className="p-2">
                  <div className="mb-2 flex items-center justify-between">
                    <span className="text-xs font-medium text-muted-foreground">
                      {t("workflows.testTitle")}
                    </span>
                    <button
                      type="button"
                      className="text-xs text-muted-foreground hover:text-foreground"
                      onClick={() => setRunnerOpen(false)}
                    >
                      ▾ {t("workflows.collapse")}
                    </button>
                  </div>
                  {def.input?.description && (
                    <div className="mb-2 text-xs text-muted-foreground">
                      {t("workflows.expectsLabel")}: {def.input.description}
                    </div>
                  )}
                  {def.input?.file && (
                    <div className="mb-2 flex flex-col gap-1">
                      <span className="text-xs text-muted-foreground">
                        {t("workflows.inputFilesLabel")}
                      </span>
                      <Textarea
                        rows={2}
                        value={inputPaths}
                        onChange={(e) => setInputPaths(e.target.value)}
                        placeholder={t("workflows.inputFilesPlaceholder")}
                        className="font-mono text-xs"
                      />
                    </div>
                  )}
                  <div className="flex items-start gap-2">
                    <Textarea
                      rows={3}
                      value={task}
                      onChange={(e) => setTask(e.target.value)}
                      placeholder={t("workflows.taskPlaceholder")}
                      className="flex-1 resize-y"
                    />
                    <Button size="sm" onClick={onRun} disabled={running || !task.trim()} className="shrink-0">
                      {running ? (
                        <Loader2 className="h-4 w-4 animate-spin" />
                      ) : (
                        <>
                          <Play className="h-3.5 w-3.5" /> {t("workflows.run")}
                        </>
                      )}
                    </Button>
                  </div>
                  {runHistory.length > 1 && (
                    <div className="mt-2 flex flex-wrap items-center gap-1">
                      <span className="text-[10px] uppercase tracking-wide text-muted-foreground">
                        {t("workflows.runHistory")}
                      </span>
                      {runHistory.map((r, i) => (
                        <button
                          key={r.run_id}
                          type="button"
                          onClick={() => setRunResult(r)}
                          title={r.run_id}
                          className={cn(
                            "rounded border px-1.5 py-0.5 font-mono text-[10px]",
                            runResult?.run_id === r.run_id
                              ? "border-primary text-foreground"
                              : "text-muted-foreground hover:text-foreground",
                            r.status !== "completed" && "border-amber-500/60",
                          )}
                        >
                          #{runHistory.length - i}
                        </button>
                      ))}
                    </div>
                  )}
                  {runResult && (
                    <div className="mt-2 max-h-72 overflow-y-auto rounded border p-2 text-xs">
                      <RunDetail result={runResult} />
                    </div>
                  )}
                </div>
              )}
            </div>
          )}
        </div>

        {selectedNode && (
          <aside className="w-96 shrink-0 flex flex-col h-full border-l p-3 overflow-y-auto">
            <NodeConfigPanel
              node={selectedNode}
              nodeIds={nodeIds}
              isStart={def?.start === selectedNode.id}
              personas={personas}
              allWorkflowNames={names}
              currentWorkflowName={selected ?? ""}
              token={token}
              onChange={updateNode}
              onMakeStart={() => mutate((d) => ({ ...d, start: selectedNode.id }))}
              onDelete={() => deleteNode(selectedNode.id)}
            />
          </aside>
        )}

        {selectedIo && def && (
          <aside className="w-96 shrink-0 flex flex-col h-full border-l p-3 overflow-y-auto">
            <IOConfigPanel
              which={selectedIo}
              desc={(selectedIo === "input" ? def.input : def.output) ?? {}}
              onChange={(patch) => setIo(selectedIo, patch)}
              onRemove={() => removeIo(selectedIo)}
            />
          </aside>
        )}
      </div>
    </div>
  );
}

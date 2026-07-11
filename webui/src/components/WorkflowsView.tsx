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
import {
  Check,
  Copy,
  Lightbulb,
  Loader2,
  Play,
  Plus,
  Sparkles,
  Terminal,
  Trash2,
  Workflow as WorkflowIcon,
} from "lucide-react";
import { useTranslation } from "react-i18next";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import {
  ApiError,
  applyWorkflowRecommendation,
  dismissWorkflowRecommendation,
  deleteWorkflow,
  duplicateWorkflow,
  getWorkflow,
  getWorkflowRecommendations,
  getWorkflowRunManifest,
  getWorkflowScript,
  listWorkflowRuns,
  listWorkflows,
  listWorkflowScripts,
  listPersonas,
  runWorkflow,
  saveWorkflow,
  saveWorkflowScript,
  type PersonaItem,
  type WorkflowRecommendation,
  type WorkflowRunResult,
  type WorkflowRunSummary,
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
import { RunDetail, runChipTone } from "@/components/workflows/RunDetail";
import { RunsView } from "@/components/workflows/RunsView";

function errMsg(e: unknown): string {
  if (e instanceof ApiError) return e.detail ? `HTTP ${e.status}: ${e.detail}` : `HTTP ${e.status}`;
  return (e as Error).message;
}

const KIND_RING: Record<string, string> = {
  work: "border-emerald-400/70",
  script: "border-amber-400/70",
  parallel: "border-violet-400/70",
  subflow: "border-sky-400/70",
};

// Node kinds that render a distinguishing icon in their card header, beyond the
// KIND_RING border color (e.g. script nodes look like agent nodes otherwise).
const KIND_ICON: Record<string, typeof Terminal> = {
  script: Terminal,
};

// Maps a stored node kind to the i18n key suffix used for display labels.
// Routing is shown only by the presence of pass/fail edges, never by a badge.
export function kindLabelKey(kind: string): string {
  if (kind === "work") return "work";
  if (kind === "subworkflow") return "subflow";
  return kind; // "script" | "parallel"
}

function nodeSummary(node: WorkflowNodeDef): string {
  if (node.kind === "script") return String(node.command || node.script || "");
  if (node.kind === "parallel") return node.worker ? "dynamic · ×N" : `${((node.branches as string[]) ?? []).length} branches`;
  if (node.kind === "subworkflow") return String(node.workflow ?? "");
  return `${(node.mode as string) ?? "build"} · ${(node.model as string) ?? "default"}`;
}

function NodeCard({ data, selected }: NodeProps) {
  const { t } = useTranslation();
  const { node, isStart } = data as unknown as FlowNodeData;
  const isDynamicWorker = !!(data as Record<string, unknown>).dynamicWorker;
  const labelKey = kindLabelKey(node.kind);
  const KindIcon = KIND_ICON[labelKey];
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
        {KindIcon && <KindIcon className="h-2.5 w-2.5" aria-hidden />}
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
  script: NodeCard,
  parallel: NodeCard,
  subworkflow: NodeCard,
  input_obj: IOCard,
  output_obj: IOCard,
};

const MODES = ["build", "plan", "explore"];
const CONTEXTS = ["own", "shared"];
const SESSIONS = ["fresh", "persistent"];
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

// Routing controls shared by "work" and "script" nodes: the routes toggle, the
// routing-shape select (binary vs multi-way), the on_pass/on_fail selects, the
// cases editor, and the `next` field fallback when routing is off. `routingExtras`
// merges extra fields when routing is switched on — work nodes default the mode to
// "explore"; script nodes pass nothing, since mode does not apply to them.
function RoutingFields({
  node,
  nodes,
  patch,
  t,
  routingExtras,
}: {
  node: WorkflowNodeDef;
  nodes: WorkflowNodeDef[];
  patch: (p: Partial<WorkflowNodeDef>) => void;
  t: (k: string) => string;
  routingExtras?: Partial<WorkflowNodeDef>;
}) {
  const others = nodes.map((n) => n.id).filter((id) => id !== node.id);

  // Determine the active routing shape: "binary" (on_pass/on_fail), "multiway" (cases), or "none" (next).
  // Detect by KEY PRESENCE (!== undefined), not value: a routing branch may legitimately be
  // null (= ends at the workflow output), and a freshly-enabled binary node has both edges
  // null — so a `!= null` test would mis-read it as "none" and the routing toggle would
  // un-check itself the moment it is checked.
  const routingShape: "binary" | "multiway" | "none" =
    node.on_pass !== undefined || node.on_fail !== undefined
      ? "binary"
      : node.cases !== undefined
        ? "multiway"
        : "none";
  const routes = routingShape !== "none";

  function switchRoutingShape(shape: "none" | "binary" | "multiway") {
    // Always clear all three shapes first, then apply the selected one.
    const clear: Partial<WorkflowNodeDef> = { on_pass: undefined, on_fail: undefined, cases: undefined, next: undefined };
    if (shape === "none") {
      patch({ ...clear, next: null });
    } else if (shape === "binary") {
      patch({ ...clear, on_pass: null, on_fail: null, ...routingExtras });
    } else {
      // multiway: start with one empty case row
      patch({ ...clear, cases: { "case1": null }, ...routingExtras });
    }
  }

  const routingToggleId = `routing-toggle-${node.id}`;

  return (
    <>
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
            <option value="multiway">{t("workflows.routingShapeMultiway")}</option>
          </select>
        </Field>
      )}

      {routingShape === "binary" && (
        <>
          <Field label={t("workflows.onPass")}>
            <TargetSelect
              value={node.on_pass as string | null}
              options={others}
              onChange={(v) => patch({ on_pass: v })}
            />
          </Field>
          <Field label={t("workflows.onFail")}>
            <TargetSelect
              value={node.on_fail as string | null}
              options={others}
              onChange={(v) => patch({ on_fail: v })}
            />
          </Field>
        </>
      )}

      {routingShape === "multiway" && (
        <CasesEditor
          cases={(node.cases as Record<string, string | null>) ?? {}}
          options={others}
          onChange={(cases) => patch({ cases })}
          t={t}
        />
      )}

      {routingShape === "none" && (
        <Field label={t("workflows.next")}>
          <TargetSelect
            value={node.next as string}
            options={others}
            onChange={(v) => patch({ next: v })}
          />
        </Field>
      )}
    </>
  );
}

// Inline create/edit form for one script file under workflows/scripts/, used by
// ScriptFields' "New script…" and "Edit" affordances. `mode: "new"` starts with an
// empty name+content (name is user-typed, checked against the backend's single-
// path-segment rule client-side as a hint — the server re-validates regardless);
// `mode: "edit"` fetches `initialName`'s current content and keeps the name fixed.
// Deliberately no modal/dialog: it renders inline in the node config panel, like
// the rest of this file's edit affordances (see the "duplicate" inline form above).
function ScriptFileEditor({
  mode,
  token,
  initialName,
  t,
  onCancel,
  onSaved,
}: {
  mode: "new" | "edit";
  token: string;
  initialName: string;
  t: (k: string, opts?: Record<string, unknown>) => string;
  onCancel: () => void;
  onSaved: (name: string) => void;
}) {
  const [name, setName] = useState(mode === "new" ? "" : initialName);
  const [content, setContent] = useState("");
  const [original, setOriginal] = useState("");
  const [loading, setLoading] = useState(mode === "edit");
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");

  useEffect(() => {
    if (mode !== "edit") return;
    let alive = true;
    getWorkflowScript(token, initialName)
      .then((c) => {
        if (!alive) return;
        setContent(c);
        setOriginal(c);
      })
      .catch(() => { if (alive) setError(t("workflows.scriptLoadError")); })
      .finally(() => { if (alive) setLoading(false); });
    return () => { alive = false; };
  }, [mode, initialName, token, t]);

  const trimmedName = name.trim();
  // Mirrors the server's containment rule (single relative path segment) so the
  // button disables before a doomed request round-trips; the server still re-checks.
  const nameValid = mode === "edit" || (
    trimmedName !== "" && trimmedName !== "." && trimmedName !== ".."
    && !trimmedName.includes("/") && !trimmedName.includes("\\")
  );
  const unchanged = mode === "edit" && content === original;
  const canSave = nameValid && content.trim() !== "" && !saving && !loading && !unchanged;

  async function handleSave() {
    setSaving(true);
    setError("");
    const target = mode === "new" ? trimmedName : initialName;
    try {
      await saveWorkflowScript(token, target, content);
      onSaved(target);
    } catch (e) {
      setError(e instanceof ApiError ? (e.detail || e.message) : String(e));
    } finally {
      setSaving(false);
    }
  }

  return (
    <div className="flex flex-col gap-2 rounded border p-2">
      {mode === "new" && (
        <Field label={t("workflows.scriptNewName")}>
          <Input
            autoFocus
            value={name}
            placeholder={t("workflows.scriptNamePlaceholder")}
            onChange={(e) => setName(e.target.value)}
            className="h-8"
          />
        </Field>
      )}
      <span className="text-[11px] text-muted-foreground">{t("workflows.scriptNameHint")}</span>
      <Field label={t("workflows.scriptContent")}>
        <Textarea
          className="min-h-[10rem] resize-y font-mono text-sm"
          value={content}
          disabled={loading}
          placeholder={loading ? t("workflows.scriptLoading") : undefined}
          onChange={(e) => setContent(e.target.value)}
        />
      </Field>
      {error && <p className="text-[11px] text-destructive">{error}</p>}
      <div className="flex items-center gap-2">
        <Button size="sm" className="h-8" onClick={() => void handleSave()} disabled={!canSave}>
          {saving ? <Loader2 className="h-4 w-4 animate-spin" /> : t("workflows.save")}
        </Button>
        <Button size="sm" variant="ghost" className="h-8" onClick={onCancel}>
          {t("workflows.cancel")}
        </Button>
      </div>
    </div>
  );
}

// Config fields for a "script" node: source (inline command vs. a file under
// workflows/scripts/ — exactly one applies, mirroring the backend's contract),
// an optional timeout, the subprocess env knob (clean allowlist default vs. full
// gateway env), the shared routing controls, and the per-node visit budget.
// Agent-only fields (model/persona/context/session/prompt/mode/tools/skills/mcps/
// max_turns) are deliberately never rendered here — the backend parser rejects them
// on a script node.
function ScriptFields({
  node,
  allNodes,
  token,
  onChange,
  t,
  workflowMaxVisits,
}: {
  node: WorkflowNodeDef;
  allNodes: WorkflowNodeDef[];
  token: string;
  onChange: (patch: Partial<WorkflowNodeDef>) => void;
  t: (k: string, opts?: Record<string, unknown>) => string;
  workflowMaxVisits?: number;
}) {
  const [scriptFiles, setScriptFiles] = useState<string[]>([]);
  const refreshScripts = useCallback(() => {
    return listWorkflowScripts(token).then(setScriptFiles).catch(() => setScriptFiles([]));
  }, [token]);
  useEffect(() => { void refreshScripts(); }, [refreshScripts]);

  // "none": no editor open. "new"/"edit" mirror ScriptFileEditor's modes.
  const [editorMode, setEditorMode] = useState<"none" | "new" | "edit">("none");

  // Detect by key presence, not truthiness: an empty `script: ""` (file mode, no file
  // picked yet) must still show the file picker, not fall back to the inline command.
  const isFileMode = node.script !== undefined;
  const selectedScript = String(node.script ?? "");

  function handleEditorSaved(name: string) {
    setEditorMode("none");
    onChange({ script: name });
    void refreshScripts();
  }

  return (
    <>
      <Field label={t("workflows.scriptSource")}>
        <select
          className={selectCls}
          value={isFileMode ? "file" : "inline"}
          onChange={(e) => {
            setEditorMode("none");
            if (e.target.value === "file") onChange({ command: undefined, script: "" });
            else onChange({ script: undefined, command: "" });
          }}
        >
          <option value="inline">{t("workflows.scriptSourceInline")}</option>
          <option value="file">{t("workflows.scriptSourceFile")}</option>
        </select>
      </Field>

      {isFileMode ? (
        <>
          <Field label={t("workflows.scriptFile")}>
            <select
              className={selectCls}
              value={selectedScript}
              onChange={(e) => { setEditorMode("none"); onChange({ script: e.target.value }); }}
            >
              <option value="">{t("workflows.scriptFileNone")}</option>
              {scriptFiles.map((f) => (
                <option key={f} value={f}>{f}</option>
              ))}
            </select>
          </Field>

          <div className="flex items-center gap-3">
            <button
              type="button"
              className="text-xs text-muted-foreground hover:text-foreground"
              onClick={() => setEditorMode(editorMode === "new" ? "none" : "new")}
            >
              {t("workflows.scriptNew")}
            </button>
            {selectedScript && (
              <button
                type="button"
                className="text-xs text-muted-foreground hover:text-foreground"
                onClick={() => setEditorMode(editorMode === "edit" ? "none" : "edit")}
              >
                {t("workflows.scriptEditAction")}
              </button>
            )}
          </div>

          {editorMode !== "none" && (
            <ScriptFileEditor
              mode={editorMode}
              token={token}
              initialName={selectedScript}
              t={t}
              onCancel={() => setEditorMode("none")}
              onSaved={handleEditorSaved}
            />
          )}
        </>
      ) : (
        <Field label={t("workflows.command")}>
          <Textarea
            className="min-h-[6rem] resize-y font-mono text-sm"
            value={String(node.command ?? "")}
            placeholder={t("workflows.commandPlaceholder")}
            onChange={(e) => onChange({ command: e.target.value })}
          />
        </Field>
      )}

      <Field label={t("workflows.timeout")}>
        <Input
          type="number"
          min={1}
          value={(node.timeout as number | undefined) ?? ""}
          placeholder={t("workflows.timeoutHint")}
          onChange={(e) => {
            const n = parseInt(e.target.value, 10);
            onChange({ timeout: e.target.value === "" || !Number.isFinite(n) ? undefined : Math.max(1, n) });
          }}
          className="h-8"
        />
      </Field>

      <Field label={t("workflows.scriptEnv")}>
        <select
          className={selectCls}
          value={node.env === "inherit" ? "inherit" : "clean"}
          onChange={(e) => onChange({ env: e.target.value === "inherit" ? "inherit" : undefined })}
        >
          <option value="clean">{t("workflows.scriptEnvClean")}</option>
          <option value="inherit">{t("workflows.scriptEnvInherit")}</option>
        </select>
      </Field>

      <RoutingFields node={node} nodes={allNodes} patch={onChange} t={t} />

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
      <span className="text-[11px] text-muted-foreground">
        {t("workflows.effectivePassLimit", {
          limit: (node.max_visits as number | undefined) ?? workflowMaxVisits ?? 3,
        })}
      </span>
    </>
  );
}

// The id of the parallel node (if any) that references `nodeId` as a static branch
// or as its dynamic worker — used to warn that a persistent session on `nodeId` is
// invalid there (each parallel unit needs its own fresh session).
function parallelParentOf(nodeId: string, allNodes: WorkflowNodeDef[]): string | null {
  for (const n of allNodes) {
    if (n.kind !== "parallel") continue;
    if (n.worker === nodeId) return n.id;
    if (Array.isArray(n.branches) && n.branches.includes(nodeId)) return n.id;
  }
  return null;
}

function NodeConfigPanel({
  node,
  nodeIds,
  isStart,
  personas,
  allWorkflowNames,
  currentWorkflowName,
  allNodes,
  workflowMaxVisits,
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
  allNodes: WorkflowNodeDef[];
  workflowMaxVisits?: number;
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

      {node.kind === "work" && (
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
                  {CONTEXTS.map((c) => <option key={c} value={c}>{t(`workflows.context_${c}`)}</option>)}
                </select>
              </Field>

              {((node.context as string) ?? "own") === "own" && (
                <Field label={t("workflows.session")}>
                  <select
                    className={selectCls}
                    value={(node.session as string) ?? "fresh"}
                    onChange={(e) => onChange({ session: e.target.value === "fresh" ? undefined : e.target.value })}
                  >
                    {SESSIONS.map((s) => <option key={s} value={s}>{t(`workflows.session_${s}`)}</option>)}
                  </select>
                </Field>
              )}

              {(node.session as string) === "persistent" && (() => {
                const parallelId = parallelParentOf(node.id, allNodes);
                return parallelId ? (
                  <p className="rounded bg-warn/10 px-2 py-1.5 text-[11px] text-warn">
                    {t("workflows.persistentInParallelWarning", { parallelId })}
                  </p>
                ) : null;
              })()}

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
                  className="flex-1 resize-y min-h-[16rem] font-mono text-sm leading-relaxed"
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

          <RoutingFields node={node} nodes={allNodes} patch={onChange} t={t} routingExtras={{ mode: "explore" }} />

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
          <span className="text-[11px] text-muted-foreground">
            {t("workflows.effectivePassLimit", {
              limit: (node.max_visits as number | undefined) ?? workflowMaxVisits ?? 3,
            })}
          </span>
        </>
      )}

      {node.kind === "script" && (
        <ScriptFields node={node} allNodes={allNodes} token={token} onChange={onChange} t={t} workflowMaxVisits={workflowMaxVisits} />
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

        // The backend parser only accepts WorkNode targets in a parallel node's
        // branches/worker positions (script/subworkflow/parallel nodes are rejected),
        // so both pickers are restricted to kind "work". A branch/worker id from an
        // older definition that no longer resolves to a work node is simply not
        // rendered as an option — it isn't dropped from the definition itself.
        const workNodeIds = others.filter((id) => allNodes.find((n) => n.id === id)?.kind === "work");

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
                  {workNodeIds.length === 0 ? (
                    <span className="text-xs text-muted-foreground">(no other nodes)</span>
                  ) : (
                    workNodeIds.map((id) => (
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
                    options={workNodeIds}
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
          className="flex-1 resize-y min-h-[7rem]"
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

let _idSeq = 0;

type WorkflowsPane = "editor" | "runs";

export function WorkflowsView() {
  const { t } = useTranslation();
  const { token } = useClient();
  const [pane, setPane] = useState<WorkflowsPane>("editor");
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
  const [resuming, setResuming] = useState(false);
  const [runResult, setRunResult] = useState<WorkflowRunResult | null>(null);
  // Minimal run history: the runs triggered for the selected workflow this session,
  // newest-first. Clicking one re-shows its detail (the full result is kept in memory).
  const [runHistory, setRunHistory] = useState<WorkflowRunResult[]>([]);
  // Persisted run history for the selected workflow (survives a reload), fetched on
  // workflow select. A chip here has only the summary until clicked, at which point
  // its manifest is fetched and cached in `runHistory` alongside this session's live runs.
  const [persistedRuns, setPersistedRuns] = useState<WorkflowRunSummary[]>([]);
  const [manifestLoading, setManifestLoading] = useState<string | null>(null);
  const [creating, setCreating] = useState(false);
  const [newName, setNewName] = useState("");
  const [duplicating, setDuplicating] = useState(false);
  const [dupName, setDupName] = useState("");
  const [personas, setPersonas] = useState<PersonaItem[]>([]);
  const [inputPaths, setInputPaths] = useState("");
  const [outFmt, setOutFmt] = useState("");
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
    // next is left UNSET (undefined), not null: a brand-new node starts UNCONNECTED — no
    // edge to the workflow output — until the user wires it. (null means "ends the flow".)
    const node: WorkflowNodeDef = { id, kind: "work", mode: "build", prompt: "" };
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

  const addScriptNode = useCallback(() => {
    const id = `script-${++_idSeq}`;
    const node: WorkflowNodeDef = { id, kind: "script", command: "" };
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

  // Copy the open workflow under a new name and open the copy, to use as a starting point.
  const onDuplicate = useCallback(async () => {
    const target = dupName.trim();
    if (!target || !selected) return;
    if (names.includes(target)) {
      setError(t("workflows.nameExists"));
      return;
    }
    setSaving(true);
    setError(null);
    try {
      const created = await duplicateWorkflow(token, selected, target);
      setNames((ns) => Array.from(new Set([...ns, created])).sort());
      setDuplicating(false);
      setDupName("");
      setSelected(created); // the [selected] effect loads the copy and renders it
    } catch (e) {
      setError(errMsg(e));
    } finally {
      setSaving(false);
    }
  }, [dupName, selected, names, token, t]);

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
          if (isRouting) {
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
    setPersistedRuns([]);
    if (!selected) {
      setRecs([]);
      return;
    }
    getWorkflowRecommendations(token, selected).then(setRecs).catch(() => setRecs([]));
    listWorkflowRuns(token, selected).then(setPersistedRuns).catch(() => setPersistedRuns([]));
  }, [selected, token]);

  const onRun = useCallback(async () => {
    if (!selected || !task.trim()) return;
    setRunning(true);
    setError(null);
    setRunResult(null);
    try {
      const files = inputPaths.split("\n").map((s) => s.trim()).filter(Boolean);
      const result = await runWorkflow(token, selected, task, files, outFmt);
      setRunResult(result);
      // Newest-first; de-dupe by run_id so a re-render never doubles an entry.
      setRunHistory((prev) => [result, ...prev.filter((r) => r.run_id !== result.run_id)]);
    } catch (e) {
      setError(errMsg(e));
    } finally {
      setRunning(false);
    }
  }, [selected, task, token, inputPaths, outFmt]);

  // Resume a needs_input run with the user's answers. Reuses the same run_id, so the
  // result REPLACES that run's entry in history (in place) rather than appending a
  // new chip — the run history stays one entry per run_id regardless of how many
  // times it pauses and resumes.
  const onResume = useCallback(
    async (runId: string, answers: string) => {
      if (!selected || !answers.trim()) return;
      setResuming(true);
      setError(null);
      try {
        const result = await runWorkflow(token, selected, answers, [], outFmt, "", runId);
        setRunResult(result);
        setRunHistory((prev) => prev.map((r) => (r.run_id === result.run_id ? result : r)));
      } catch (e) {
        setError(errMsg(e));
      } finally {
        setResuming(false);
      }
    },
    [selected, token, outFmt],
  );

  // Show a run's detail: a live/already-fetched run is already in `runHistory` and just
  // gets re-shown; a persisted-only chip fetches its manifest (raw, no per-node output
  // text) and caches it into `runHistory` so re-clicking it is instant.
  const onSelectRun = useCallback(
    async (runId: string) => {
      const cached = runHistory.find((r) => r.run_id === runId);
      if (cached) {
        setRunResult(cached);
        return;
      }
      if (!selected) return;
      setManifestLoading(runId);
      setError(null);
      try {
        const manifest = await getWorkflowRunManifest(token, selected, runId);
        setRunResult(manifest);
        setRunHistory((prev) => [manifest, ...prev.filter((r) => r.run_id !== manifest.run_id)]);
      } catch (e) {
        setError(errMsg(e));
      } finally {
        setManifestLoading(null);
      }
    },
    [runHistory, selected, token],
  );

  // Merge this session's live runs with the persisted history for the strip: a run_id
  // present in both is shown once (live entry wins, since it may be more current — e.g.
  // right after a resume — than what was last persisted). Newest-first by construction:
  // runHistory is already newest-first, and persisted entries not seen live are appended
  // in their own newest-first order from the server.
  const historyStrip: {
    run_id: string;
    status: string;
    needs_input_node: string | null;
    parent_run_id: string | null;
  }[] = [
    ...runHistory.map((r) => ({
      run_id: r.run_id,
      status: r.status,
      needs_input_node: r.needs_input_node ?? null,
      parent_run_id: null,
    })),
    ...persistedRuns
      .filter((p) => !runHistory.some((r) => r.run_id === p.run_id))
      .map((p) => ({
        run_id: p.run_id,
        status: p.status,
        needs_input_node: p.needs_input_node,
        parent_run_id: p.parent_run_id ?? null,
      })),
  ];

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

  const onDismissRec = useCallback(
    async (id: string) => {
      if (!selected) return;
      try {
        await dismissWorkflowRecommendation(token, selected, id);
        setRecs(await getWorkflowRecommendations(token, selected));
      } catch (e) {
        setError(errMsg(e));
      }
    },
    [selected, token],
  );

  const onCopyStructural = useCallback(
    async (r: WorkflowRecommendation) => {
      // Copy-ready context so the user can open a chat and treat the idea with
      // the agent: the workflow, the dream's proposal, why the autonomous scope
      // refused it, and the run evidence.
      const text = [
        `Review a structural improvement idea for my workflow "${selected}".`,
        `The dream proposed (out of its prompt-only scope, so it was NOT applied):`,
        JSON.stringify(r.proposal ?? {}, null, 2),
        `Why it was escalated: ${r.why_rejected ?? ""}`,
        `Run evidence: ${r.diagnostic ?? ""}`,
        `Reason given: ${r.reason}`,
        `If it holds up, apply it with workflow_edit; otherwise tell me why not.`,
      ].join("\n");
      try {
        await navigator.clipboard.writeText(text);
        setNotice(t("workflows.recCopied"));
      } catch {
        setError(t("workflows.recCopyFailed"));
      }
    },
    [selected, t],
  );

  const nodeIds = def?.nodes.map((n) => n.id) ?? [];
  const selectedNode = def?.nodes.find((n) => n.id === selectedNodeId) ?? null;
  const selectedIo: "input" | "output" | null =
    selectedNodeId === "__input__" ? "input" : selectedNodeId === "__output__" ? "output" : null;

  return (
    <div className="flex h-full w-full flex-col">
      <div className="flex items-center gap-2 border-b px-3 py-2">
        <div
          className="flex h-7 rounded-full bg-muted p-0.5"
          role="group"
          aria-label={t("workflows.title")}
        >
          {(["editor", "runs"] as const).map((opt) => (
            <button
              key={opt}
              type="button"
              onClick={() => setPane(opt)}
              aria-pressed={pane === opt}
              className={cn(
                "rounded-full px-3 text-[12.5px] font-medium transition-colors",
                pane === opt
                  ? "bg-background text-foreground shadow-sm"
                  : "text-muted-foreground hover:text-foreground",
              )}
            >
              {opt === "editor" ? t("workflows.title") : t("runs.title")}
            </button>
          ))}
        </div>
      </div>
      <div className={cn("flex min-h-0 flex-1", pane !== "runs" && "hidden")}>
        <RunsView />
      </div>
      <div className={cn("flex min-h-0 flex-1", pane !== "editor" && "hidden")}>
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
              {recs.map((r) =>
                r.kind === "structural" ? (
                  <div key={r.id} className="flex items-start gap-2 text-sm">
                    <Lightbulb className="mt-0.5 h-4 w-4 shrink-0 text-purple-600" aria-hidden />
                    <span className="flex-1 text-purple-700 dark:text-purple-300">
                      <span className="mr-1 rounded-full bg-purple-500/10 px-2 py-0.5 text-[10px] font-semibold uppercase">
                        {t("workflows.recStructural")}
                      </span>
                      {r.reason || r.why_rejected}
                      <span className="block text-[11px] text-muted-foreground">
                        {t("workflows.recStructuralHint")} · {r.diagnostic}
                      </span>
                    </span>
                    <Button size="sm" variant="outline" onClick={() => void onCopyStructural(r)}>
                      {t("workflows.recCopy")}
                    </Button>
                    <Button size="sm" variant="ghost" onClick={() => void onDismissRec(r.id)}>
                      {t("workflows.recDismiss")}
                    </Button>
                  </div>
                ) : (
                  <div key={r.id} className="flex items-center gap-2 text-sm">
                    <Lightbulb className="h-4 w-4 shrink-0 text-amber-600" aria-hidden />
                    <span className="flex-1 text-amber-700 dark:text-amber-300">
                      <span className="font-medium">{r.target_id}.{r.field}</span> — {r.reason}
                    </span>
                    <Button size="sm" variant="outline" onClick={() => onApplyRec(r.id)}>
                      apply
                    </Button>
                    <Button size="sm" variant="ghost" onClick={() => void onDismissRec(r.id)}>
                      {t("workflows.recDismiss")}
                    </Button>
                  </div>
                ),
              )}
            </div>
          )}
          <div className="relative flex-1">
            {def && (
              <div className="pointer-events-none absolute inset-x-2 top-2 z-10 flex items-start justify-between gap-2">
                {/* Palette — add nodes to the canvas (one grouped control) */}
                <div className="pointer-events-auto inline-flex items-center gap-0.5 rounded-md border border-border bg-background p-0.5">
                  <Button size="sm" variant="ghost" className="h-7 gap-1 px-2" onClick={addNode}>
                    <Plus className="h-3.5 w-3.5" /> {t("workflows.addWork")}
                  </Button>
                  <span className="h-4 w-px bg-border" />
                  <Button size="sm" variant="ghost" className="h-7 gap-1 px-2" onClick={addParallelNode}>
                    <Plus className="h-3.5 w-3.5" /> {t("workflows.addParallel")}
                  </Button>
                  <span className="h-4 w-px bg-border" />
                  <Button size="sm" variant="ghost" className="h-7 gap-1 px-2" onClick={addSubflowNode}>
                    <Plus className="h-3.5 w-3.5" /> {t("workflows.addSubflow")}
                  </Button>
                  <span className="h-4 w-px bg-border" />
                  <Button size="sm" variant="ghost" className="h-7 gap-1 px-2" onClick={addScriptNode}>
                    <Plus className="h-3.5 w-3.5" /> {t("workflows.addScript")}
                  </Button>
                  {!def.input && (
                    <>
                      <span className="h-4 w-px bg-border" />
                      <Button size="sm" variant="ghost" className="h-7 gap-1 px-2" onClick={() => addIo("input")}>
                        <Plus className="h-3.5 w-3.5" /> {t("workflows.addInput")}
                      </Button>
                    </>
                  )}
                  {!def.output && (
                    <>
                      <span className="h-4 w-px bg-border" />
                      <Button size="sm" variant="ghost" className="h-7 gap-1 px-2" onClick={() => addIo("output")}>
                        <Plus className="h-3.5 w-3.5" /> {t("workflows.addOutput")}
                      </Button>
                    </>
                  )}
                </div>

                {/* Workflow-level — self-improvement setting + actions */}
                <div className="pointer-events-auto flex items-center gap-2">
                  {/* Workflow-level default pass budget, inherited by any node without its own max_visits. */}
                  <label
                    className="flex h-8 items-center gap-1.5 rounded-md border border-border bg-background px-2 text-xs text-muted-foreground"
                    title={t("workflows.maxVisitsWorkflow")}
                  >
                    <span>{t("workflows.maxVisitsWorkflow")}</span>
                    <Input
                      type="number"
                      min={1}
                      value={(def.max_visits as number | undefined) ?? ""}
                      placeholder="3"
                      onChange={(e) => {
                        const n = parseInt(e.target.value, 10);
                        mutate((d) => ({
                          ...d,
                          max_visits: e.target.value === "" || !Number.isFinite(n) ? undefined : Math.max(1, n),
                        }));
                      }}
                      className="h-6 w-14 text-sm"
                    />
                  </label>
                  {/* Self-improvement — two states (manual/auto) like a skill's, as one cohesive control. */}
                  <label
                    className="flex h-8 cursor-pointer items-center gap-1.5 rounded-md border border-border bg-background px-2 text-xs text-muted-foreground"
                    title={t(`workflows.improvementHint_${(def.improvement_mode as string) || "manual"}`)}
                  >
                    <Sparkles className="h-3.5 w-3.5" />
                    <span>{t("workflows.improvementMode")}</span>
                    <select
                      className="cursor-pointer bg-transparent text-sm text-foreground outline-none"
                      value={(def.improvement_mode as string) || "manual"}
                      onChange={(e) => mutate((d) => ({ ...d, improvement_mode: e.target.value }))}
                    >
                      <option value="manual">{t("workflows.improvementManual")}</option>
                      <option value="auto">{t("workflows.improvementAuto")}</option>
                    </select>
                  </label>
                  {!duplicating ? (
                    <Button
                      size="icon"
                      variant="outline"
                      className="h-8 w-8"
                      onClick={() => { setDuplicating(true); setDupName(`${selected}-copy`); }}
                      title={t("workflows.duplicateHint")}
                      aria-label={t("workflows.duplicate")}
                    >
                      <Copy className="h-3.5 w-3.5" />
                    </Button>
                  ) : (
                    <div className="flex items-center gap-1">
                      <Input
                        autoFocus
                        value={dupName}
                        onChange={(e) => setDupName(e.target.value)}
                        disabled={saving}
                        placeholder={t("workflows.duplicateNamePlaceholder")}
                        onKeyDown={(e) => {
                          if (e.key === "Enter") void onDuplicate();
                          if (e.key === "Escape") { setDuplicating(false); setDupName(""); }
                        }}
                        className="h-8 w-44 text-sm"
                      />
                      <Button size="sm" className="h-8" onClick={() => void onDuplicate()} disabled={saving}>
                        {saving ? <Loader2 className="h-4 w-4 animate-spin" /> : <Check className="h-3.5 w-3.5" />}
                      </Button>
                      <Button size="sm" variant="ghost" className="h-8" onClick={() => { setDuplicating(false); setDupName(""); }}>
                        {t("workflows.cancel")}
                      </Button>
                    </div>
                  )}
                  {dirty && (
                    <Button size="sm" className="h-8" onClick={onSave} disabled={saving}>
                      {saving ? <Loader2 className="h-4 w-4 animate-spin" /> : t("workflows.save")}
                    </Button>
                  )}
                  {notice && <span className="text-sm text-emerald-600">{notice}</span>}
                  {error && <span className="text-sm text-destructive">{error}</span>}
                </div>
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
                  <div className="mb-2 flex flex-col gap-1">
                    <span className="text-xs text-muted-foreground">
                      {t("workflows.outputFormatLabel")}
                    </span>
                    <Input
                      value={outFmt}
                      onChange={(e) => setOutFmt(e.target.value)}
                      placeholder={t("workflows.outputFormatPlaceholder")}
                      className="text-xs"
                    />
                  </div>
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
                  {historyStrip.length > 0 && (
                    <div className="mt-2 flex flex-col gap-1">
                      <div className="flex flex-wrap items-center gap-1">
                        <span className="text-[10px] uppercase tracking-wide text-muted-foreground">
                          {t("workflows.runHistory")}
                        </span>
                        {historyStrip.map((r, i) => (
                          <button
                            key={r.run_id}
                            type="button"
                            onClick={() => onSelectRun(r.run_id)}
                            title={
                              r.parent_run_id
                                ? `${r.run_id} · ${r.status} · sub of ${r.parent_run_id}`
                                : `${r.run_id} · ${r.status}`
                            }
                            disabled={manifestLoading === r.run_id}
                            className={cn(
                              "rounded border px-1.5 py-0.5 font-mono text-[10px]",
                              runResult?.run_id === r.run_id ? "border-primary text-foreground" : runChipTone(r.status),
                            )}
                          >
                            {manifestLoading === r.run_id ? (
                              <Loader2 className="h-2.5 w-2.5 animate-spin" />
                            ) : (
                              `#${historyStrip.length - i}`
                            )}
                          </button>
                        ))}
                      </div>
                      <span className="text-[10px] text-muted-foreground opacity-70">
                        {t("workflows.historyRetention")}
                      </span>
                    </div>
                  )}
                  {runResult && (
                    <div className="mt-2 max-h-72 overflow-y-auto rounded border p-2 text-xs">
                      <RunDetail
                        result={runResult}
                        resuming={resuming}
                        onResume={(answers) => onResume(runResult.run_id, answers)}
                      />
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
              allNodes={def?.nodes ?? []}
              workflowMaxVisits={def?.max_visits}
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
    </div>
  );
}

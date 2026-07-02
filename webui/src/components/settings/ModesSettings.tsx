import { useCallback, useEffect, useMemo, useState } from "react";
import {
  Check,
  ChevronDown,
  Copy,
  Pencil,
  Plus,
  SlidersHorizontal,
  Trash2,
  X,
} from "lucide-react";
import { useTranslation } from "react-i18next";

import { Button } from "@/components/ui/button";
import {
  ApiError,
  deleteMode as apiDeleteMode,
  upsertMode as apiUpsertMode,
  listModes,
  listTools,
  type ModeInfo,
  type ToolInfo,
} from "@/lib/api";
import { cn } from "@/lib/utils";

type AccessKind = "full" | "except" | "only";

interface Draft {
  name: string;
  isNew: boolean;
  description: string;
  accessKind: AccessKind;
  tools: string[]; // denied (except) or allowed (only)
  prompt: string;
  icon: string | null;
}

function modeToDraft(mode: ModeInfo, asNew: boolean): Draft {
  const accessKind: AccessKind =
    mode.allowed !== null ? "only" : mode.denied.length > 0 ? "except" : "full";
  return {
    name: asNew ? "" : mode.name,
    isNew: asNew,
    description: mode.description,
    accessKind,
    tools: mode.allowed !== null ? [...mode.allowed] : [...mode.denied],
    prompt: mode.prompt_suffix,
    icon: mode.icon,
  };
}

function blankDraft(): Draft {
  return { name: "", isNew: true, description: "", accessKind: "full", tools: [], prompt: "", icon: null };
}

function accessSummary(mode: ModeInfo, t: (k: string) => string): string {
  if (mode.allowed !== null) return t("settings.modes.access.onlyCount").replace("{n}", String(mode.allowed.length));
  if (mode.denied.length > 0) return t("settings.modes.access.exceptCount").replace("{n}", String(mode.denied.length));
  return t("settings.modes.access.full");
}

export function ModesSettings({ token }: { token: string }) {
  const { t } = useTranslation();
  const [modes, setModes] = useState<ModeInfo[]>([]);
  const [tools, setTools] = useState<ToolInfo[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [draft, setDraft] = useState<Draft | null>(null);
  const [busy, setBusy] = useState(false);
  const [confirmDelete, setConfirmDelete] = useState<string | null>(null);
  const [expanded, setExpanded] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    try {
      // Tools catalog is best-effort: the editor degrades to free-text entry
      // if it is unavailable, so a catalog failure must not block the modes list.
      const [m, tl] = await Promise.all([
        listModes(token),
        listTools(token).catch(() => [] as ToolInfo[]),
      ]);
      setModes(m);
      setTools(tl);
      setError(null);
    } catch (e) {
      setError(e instanceof ApiError ? `HTTP ${e.status}` : (e as Error).message);
    }
  }, [token]);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  // Presets clone the access of an existing mode (read straight from the list),
  // so "like plan" stays correct even if plan's tool set changes server-side.
  const readOnlyTools = useMemo(
    () => modes.find((m) => m.name === "explore")?.allowed ?? [],
    [modes],
  );
  const planTools = useMemo(() => modes.find((m) => m.name === "plan")?.allowed ?? [], [modes]);

  const save = useCallback(async () => {
    if (!draft) return;
    const name = draft.name.trim();
    if (!name) {
      setError(t("settings.modes.nameRequired"));
      return;
    }
    setBusy(true);
    try {
      await apiUpsertMode(token, {
        name,
        description: draft.description,
        allowed: draft.accessKind === "only" ? draft.tools : null,
        denied: draft.accessKind === "except" ? draft.tools : [],
        prompt_suffix: draft.prompt,
        icon: draft.icon,
      });
      setDraft(null);
      await refresh();
    } catch (e) {
      setError(e instanceof ApiError ? `HTTP ${e.status}` : (e as Error).message);
    } finally {
      setBusy(false);
    }
  }, [draft, token, refresh, t]);

  const remove = useCallback(
    async (name: string) => {
      setBusy(true);
      try {
        await apiDeleteMode(token, name);
        setConfirmDelete(null);
        await refresh();
      } catch (e) {
        setError(e instanceof ApiError ? `HTTP ${e.status}` : (e as Error).message);
      } finally {
        setBusy(false);
      }
    },
    [token, refresh],
  );

  return (
    <div className="space-y-4">
      {error ? (
        <div className="rounded-[14px] border border-destructive/20 bg-destructive/5 px-4 py-2.5 text-[13px] text-destructive">
          {error}
        </div>
      ) : null}

      <div className="overflow-hidden rounded-[22px] border border-border/45 bg-card/86">
        <div className="flex items-center justify-between gap-2 border-b border-border/45 px-5 py-3">
          <div>
            <div className="text-[13px] font-semibold text-foreground/80">{t("settings.modes.title")}</div>
            <div className="text-[12px] text-muted-foreground">{t("settings.modes.subtitle")}</div>
          </div>
          <Button
            size="sm"
            variant="ghost"
            disabled={!!draft}
            onClick={() => setDraft(blankDraft())}
            className="rounded-full"
          >
            <Plus className="mr-1 h-3.5 w-3.5" aria-hidden />
            {t("settings.modes.new")}
          </Button>
        </div>

        <div className="divide-y divide-border/40">
          {modes.map((mode) => {
            const isOpen = expanded === mode.name;
            return (
              <div key={mode.name}>
                <div className="flex items-center gap-3 px-5 py-3">
                  <button
                    type="button"
                    onClick={() => setExpanded(isOpen ? null : mode.name)}
                    className="shrink-0 text-muted-foreground hover:text-foreground"
                    aria-expanded={isOpen}
                    aria-label={isOpen ? t("settings.modes.hide") : t("settings.modes.view")}
                    title={isOpen ? t("settings.modes.hide") : t("settings.modes.view")}
                  >
                    <ChevronDown className={cn("h-4 w-4 transition-transform", isOpen ? "" : "-rotate-90")} aria-hidden />
                  </button>
                  <SlidersHorizontal className="h-4 w-4 shrink-0 text-muted-foreground" aria-hidden />
                  <button
                    type="button"
                    onClick={() => setExpanded(isOpen ? null : mode.name)}
                    className="min-w-0 flex-1 text-left"
                  >
                    <div className="flex items-center gap-2">
                      <span className="font-mono text-[13px] font-medium text-foreground">{mode.name}</span>
                      {mode.builtin ? (
                        <span className="rounded-full bg-muted px-1.5 py-0.5 text-[10px] uppercase tracking-wide text-muted-foreground">
                          {t("settings.modes.builtinBadge")}
                        </span>
                      ) : null}
                      <span className="rounded-full bg-primary/10 px-1.5 py-0.5 text-[10px] text-primary">
                        {accessSummary(mode, t)}
                      </span>
                    </div>
                    {mode.description ? (
                      <div className="truncate text-[12px] text-muted-foreground">{mode.description}</div>
                    ) : null}
                  </button>
                  {mode.builtin ? (
                    <Button
                      size="sm"
                      variant="ghost"
                      disabled={!!draft}
                      onClick={() => setDraft(modeToDraft(mode, true))}
                      className="rounded-full text-[12px] text-muted-foreground"
                      title={t("settings.modes.duplicate")}
                    >
                      <Copy className="mr-1 h-3.5 w-3.5" aria-hidden />
                      {t("settings.modes.duplicate")}
                    </Button>
                  ) : confirmDelete === mode.name ? (
                    <div className="flex items-center gap-1">
                      <Button size="sm" variant="ghost" disabled={busy} onClick={() => void remove(mode.name)} className="rounded-full text-destructive">
                        {t("settings.modes.confirmDelete")}
                      </Button>
                      <Button size="sm" variant="ghost" onClick={() => setConfirmDelete(null)} className="rounded-full">
                        {t("settings.modes.cancel")}
                      </Button>
                    </div>
                  ) : (
                    <div className="flex items-center gap-1">
                      <Button size="sm" variant="ghost" disabled={!!draft} onClick={() => setDraft(modeToDraft(mode, false))} className="rounded-full text-muted-foreground" aria-label={t("settings.modes.edit")} title={t("settings.modes.edit")}>
                        <Pencil className="h-3.5 w-3.5" aria-hidden />
                      </Button>
                      <Button size="sm" variant="ghost" disabled={!!draft} onClick={() => setConfirmDelete(mode.name)} className="rounded-full text-muted-foreground" aria-label={t("settings.modes.delete")} title={t("settings.modes.delete")}>
                        <Trash2 className="h-3.5 w-3.5" aria-hidden />
                      </Button>
                    </div>
                  )}
                </div>
                {isOpen ? <ModeDetail mode={mode} tools={tools} /> : null}
              </div>
            );
          })}
        </div>
      </div>

      {draft ? (
        <ModeEditor
          draft={draft}
          busy={busy}
          tools={tools}
          readOnlyTools={readOnlyTools}
          planTools={planTools}
          onChange={setDraft}
          onSave={() => void save()}
          onCancel={() => setDraft(null)}
        />
      ) : null}
    </div>
  );
}

/** Read-only inspection of a mode: its tool surface + system instruction. */
function ModeDetail({ mode, tools }: { mode: ModeInfo; tools: ToolInfo[] }) {
  const { t } = useTranslation();
  const byName = useMemo(() => new Map(tools.map((tool) => [tool.name, tool])), [tools]);
  const names = mode.allowed !== null ? mode.allowed : mode.denied;
  const fullAccess = mode.allowed === null && mode.denied.length === 0;

  return (
    <div className="border-t border-border/30 bg-muted/20 px-5 py-3">
      <div className="mb-1.5 text-[11px] font-medium uppercase tracking-wide text-muted-foreground">
        {t("settings.modes.detailTools")}
      </div>
      {fullAccess ? (
        <div className="text-[12px] text-muted-foreground">{t("settings.modes.fullAccessNote")}</div>
      ) : (
        <div className="flex flex-wrap items-center gap-1.5">
          {mode.allowed === null && mode.denied.length > 0 ? (
            <span className="mr-1 text-[11px] text-muted-foreground">{t("settings.modes.access.except")}</span>
          ) : null}
          {names.map((name) => {
            const info = byName.get(name);
            return (
              <span
                key={name}
                title={info?.description || undefined}
                className="inline-flex items-center gap-1 rounded-full bg-card px-2 py-0.5 font-mono text-[11.5px] text-foreground/80 ring-1 ring-border/50"
              >
                {name}
                {info?.read_only ? (
                  <span className="rounded-sm bg-emerald-500/15 px-1 text-[9px] uppercase text-emerald-600 dark:text-emerald-400">
                    {t("settings.modes.readOnlyBadge")}
                  </span>
                ) : null}
              </span>
            );
          })}
        </div>
      )}

      <div className="mb-1.5 mt-3 text-[11px] font-medium uppercase tracking-wide text-muted-foreground">
        {t("settings.modes.fieldPrompt")}
      </div>
      {mode.prompt_suffix.trim() ? (
        <pre className="max-h-52 overflow-auto whitespace-pre-wrap rounded-md bg-card p-2.5 text-[11.5px] leading-relaxed text-foreground/80 ring-1 ring-border/40">
          {mode.prompt_suffix.trim()}
        </pre>
      ) : (
        <div className="text-[12px] text-muted-foreground">{t("settings.modes.noPrompt")}</div>
      )}
    </div>
  );
}

function ModeEditor({
  draft,
  busy,
  tools,
  readOnlyTools,
  planTools,
  onChange,
  onSave,
  onCancel,
}: {
  draft: Draft;
  busy: boolean;
  tools: ToolInfo[];
  readOnlyTools: string[];
  planTools: string[];
  onChange: (d: Draft) => void;
  onSave: () => void;
  onCancel: () => void;
}) {
  const { t } = useTranslation();

  const segs: { kind: AccessKind; label: string }[] = [
    { kind: "full", label: t("settings.modes.access.full") },
    { kind: "except", label: t("settings.modes.access.except") },
    { kind: "only", label: t("settings.modes.access.only") },
  ];

  return (
    <div className="overflow-hidden rounded-[22px] border border-border/45 bg-card/86 p-5">
      <div className="mb-3 flex gap-3">
        <label className="flex-1 text-[12px] font-medium text-muted-foreground">
          {t("settings.modes.fieldName")}
          <input
            value={draft.name}
            disabled={!draft.isNew}
            onChange={(e) => onChange({ ...draft, name: e.target.value })}
            placeholder={t("settings.modes.namePlaceholder")}
            className="mt-1 w-full rounded-md border border-border/60 bg-card px-2.5 py-1.5 font-mono text-[13px] text-foreground disabled:opacity-60 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
          />
        </label>
      </div>
      <label className="mb-3 block text-[12px] font-medium text-muted-foreground">
        {t("settings.modes.fieldDescription")}
        <input
          value={draft.description}
          onChange={(e) => onChange({ ...draft, description: e.target.value })}
          placeholder={t("settings.modes.descriptionPlaceholder")}
          className="mt-1 w-full rounded-md border border-border/60 bg-card px-2.5 py-1.5 text-[13px] text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
        />
      </label>

      <div className="mb-1.5 text-[12px] font-medium text-muted-foreground">{t("settings.modes.fieldAccess")}</div>
      <div className="mb-2 flex flex-wrap gap-1.5">
        <button type="button" onClick={() => onChange({ ...draft, accessKind: "only", tools: [...readOnlyTools] })} className="rounded-full border border-border/60 px-2.5 py-1 text-[11px] text-muted-foreground hover:bg-accent/40">
          {t("settings.modes.presetReadOnly")}
        </button>
        <button type="button" onClick={() => onChange({ ...draft, accessKind: "only", tools: [...planTools] })} className="rounded-full border border-border/60 px-2.5 py-1 text-[11px] text-muted-foreground hover:bg-accent/40">
          {t("settings.modes.presetLikePlan")}
        </button>
      </div>
      <div className="mb-2 flex gap-1 rounded-md border border-border/60 bg-muted/40 p-1">
        {segs.map((s) => (
          <button
            key={s.kind}
            type="button"
            onClick={() => onChange({ ...draft, accessKind: s.kind })}
            className={cn(
              "flex-1 rounded px-2 py-1 text-[12px] transition-colors",
              draft.accessKind === s.kind ? "bg-primary/10 font-medium text-primary" : "text-muted-foreground hover:text-foreground",
            )}
          >
            {s.label}
          </button>
        ))}
      </div>
      {draft.accessKind !== "full" ? (
        <ToolPicker
          tools={tools}
          selected={draft.tools}
          accessKind={draft.accessKind}
          onChange={(next) => onChange({ ...draft, tools: next })}
        />
      ) : null}

      <label className="mb-4 mt-3 block text-[12px] font-medium text-muted-foreground">
        {t("settings.modes.fieldPrompt")}
        <span className="ml-1 rounded-full bg-muted px-1.5 py-0.5 text-[10px] font-normal text-muted-foreground">{t("settings.modes.optional")}</span>
        <textarea
          value={draft.prompt}
          onChange={(e) => onChange({ ...draft, prompt: e.target.value })}
          rows={2}
          placeholder={t("settings.modes.promptPlaceholder")}
          className="mt-1 w-full resize-y rounded-md border border-border/60 bg-card px-2.5 py-1.5 text-[13px] text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
        />
      </label>

      <div className="flex items-center justify-end gap-2">
        <Button size="sm" variant="ghost" onClick={onCancel} disabled={busy} className="rounded-full">
          {t("settings.modes.cancel")}
        </Button>
        <Button size="sm" onClick={onSave} disabled={busy} className="rounded-full">
          <Check className="mr-1 h-3.5 w-3.5" aria-hidden />
          {t("settings.modes.save")}
        </Button>
      </div>
    </div>
  );
}

/** Checklist of the real tool catalog + free-text escape hatch for names not in it. */
function ToolPicker({
  tools,
  selected,
  accessKind,
  onChange,
}: {
  tools: ToolInfo[];
  selected: string[];
  accessKind: AccessKind;
  onChange: (next: string[]) => void;
}) {
  const { t } = useTranslation();
  const [query, setQuery] = useState("");
  const [custom, setCustom] = useState("");

  const selectedSet = useMemo(() => new Set(selected), [selected]);
  const catalogNames = useMemo(() => new Set(tools.map((tl) => tl.name)), [tools]);

  const toggle = (name: string) => {
    onChange(selectedSet.has(name) ? selected.filter((x) => x !== name) : [...selected, name]);
  };
  const addCustom = (raw: string) => {
    const name = raw.trim();
    if (name && !selectedSet.has(name)) onChange([...selected, name]);
    setCustom("");
  };

  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase();
    const rows = tools.filter(
      (tl) => !q || tl.name.toLowerCase().includes(q) || tl.description.toLowerCase().includes(q),
    );
    return {
      builtin: rows.filter((tl) => tl.source === "builtin"),
      mcp: rows.filter((tl) => tl.source === "mcp"),
    };
  }, [tools, query]);

  // Names the mode already references that aren't in the live catalog (e.g. an
  // MCP tool from a server that's currently down, or a hand-typed name).
  const extras = useMemo(() => selected.filter((n) => !catalogNames.has(n)), [selected, catalogNames]);

  // Drift visibility (allowlist modes only): read-only built-ins left unselected.
  const readOnlyMissing = useMemo(() => {
    if (accessKind !== "only") return 0;
    return tools.filter((tl) => tl.source === "builtin" && tl.read_only && !selectedSet.has(tl.name)).length;
  }, [tools, accessKind, selectedSet]);

  if (tools.length === 0) {
    // Catalog unavailable — fall back to free-text entry so the editor still works.
    return (
      <div className="mb-1 rounded-md border border-border/50 p-2">
        <div className="mb-1.5 text-[11px] text-muted-foreground">{t("settings.modes.catalogEmpty")}</div>
        <div className="flex flex-wrap gap-1.5">
          {selected.map((name) => (
            <SelectedChip key={name} name={name} onRemove={() => toggle(name)} />
          ))}
          <input
            value={custom}
            onChange={(e) => setCustom(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter" || e.key === ",") {
                e.preventDefault();
                addCustom(custom);
              }
            }}
            placeholder={t("settings.modes.toolPlaceholder")}
            className="min-w-[8rem] flex-1 bg-transparent px-1 py-0.5 font-mono text-[12px] text-foreground focus:outline-none"
          />
        </div>
      </div>
    );
  }

  return (
    <div className="mb-1 rounded-md border border-border/50">
      <div className="flex items-center justify-between gap-2 border-b border-border/40 px-2 py-1.5">
        <input
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          placeholder={t("settings.modes.toolSearchPlaceholder")}
          className="min-w-0 flex-1 bg-transparent px-1 py-0.5 text-[12px] text-foreground focus:outline-none"
        />
        <span className="shrink-0 text-[11px] text-muted-foreground">
          {t("settings.modes.selectedCount").replace("{n}", String(selected.length))}
        </span>
      </div>

      <div className="px-2 pb-1 pt-1.5 text-[11px] text-muted-foreground">
        {accessKind === "only" ? t("settings.modes.onlyHelp") : t("settings.modes.exceptHelp")}
      </div>

      <div className="max-h-64 overflow-auto px-1 pb-2">
        <ToolGroup label={t("settings.modes.groupBuiltin")} rows={filtered.builtin} selectedSet={selectedSet} onToggle={toggle} t={t} />
        <ToolGroup label={t("settings.modes.groupMcp")} rows={filtered.mcp} selectedSet={selectedSet} onToggle={toggle} t={t} />
      </div>

      {extras.length > 0 ? (
        <div className="border-t border-border/40 px-2 py-1.5">
          <div className="mb-1 text-[11px] text-muted-foreground">{t("settings.modes.notInCatalog")}</div>
          <div className="flex flex-wrap gap-1.5">
            {extras.map((name) => (
              <SelectedChip key={name} name={name} onRemove={() => toggle(name)} />
            ))}
          </div>
        </div>
      ) : null}

      <div className="flex items-center gap-2 border-t border-border/40 px-2 py-1.5">
        <input
          value={custom}
          onChange={(e) => setCustom(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter" || e.key === ",") {
              e.preventDefault();
              addCustom(custom);
            }
          }}
          placeholder={t("settings.modes.addByName")}
          className="min-w-0 flex-1 bg-transparent px-1 py-0.5 font-mono text-[12px] text-foreground focus:outline-none"
        />
        {accessKind === "only" && readOnlyMissing > 0 ? (
          <span className="shrink-0 text-[11px] text-muted-foreground">
            {t("settings.modes.readOnlyMissing").replace("{n}", String(readOnlyMissing))}
          </span>
        ) : null}
      </div>
    </div>
  );
}

function ToolGroup({
  label,
  rows,
  selectedSet,
  onToggle,
  t,
}: {
  label: string;
  rows: ToolInfo[];
  selectedSet: Set<string>;
  onToggle: (name: string) => void;
  t: (k: string) => string;
}) {
  if (rows.length === 0) return null;
  return (
    <div className="mb-1">
      <div className="px-1.5 pb-0.5 pt-1 text-[10px] font-medium uppercase tracking-wide text-muted-foreground">
        {label}
      </div>
      {rows.map((tl) => {
        const on = selectedSet.has(tl.name);
        return (
          <button
            key={tl.name}
            type="button"
            onClick={() => onToggle(tl.name)}
            className={cn(
              "flex w-full items-start gap-2 rounded px-1.5 py-1 text-left transition-colors hover:bg-accent/40",
              on ? "bg-primary/5" : "",
            )}
          >
            <span
              className={cn(
                "mt-0.5 flex h-3.5 w-3.5 shrink-0 items-center justify-center rounded-[4px] border",
                on ? "border-primary bg-primary text-primary-foreground" : "border-border",
              )}
              aria-hidden
            >
              {on ? <Check className="h-2.5 w-2.5" /> : null}
            </span>
            <span className="min-w-0 flex-1">
              <span className="flex items-center gap-1.5">
                <span className="font-mono text-[12px] text-foreground">{tl.name}</span>
                {tl.read_only ? (
                  <span className="rounded-sm bg-emerald-500/15 px-1 text-[9px] uppercase text-emerald-600 dark:text-emerald-400">
                    {t("settings.modes.readOnlyBadge")}
                  </span>
                ) : null}
              </span>
              {tl.description ? (
                <span className="block truncate text-[11px] text-muted-foreground">{tl.description}</span>
              ) : null}
            </span>
          </button>
        );
      })}
    </div>
  );
}

function SelectedChip({ name, onRemove }: { name: string; onRemove: () => void }) {
  const { t } = useTranslation();
  return (
    <span className="inline-flex items-center gap-1 rounded-full bg-primary/10 px-2 py-0.5 font-mono text-[11.5px] text-primary">
      {name}
      <button type="button" onClick={onRemove} aria-label={t("settings.modes.removeTool")}>
        <X className="h-3 w-3" aria-hidden />
      </button>
    </span>
  );
}

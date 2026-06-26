import { useCallback, useEffect, useMemo, useState } from "react";
import { Check, Copy, Pencil, Plus, SlidersHorizontal, Trash2, X } from "lucide-react";
import { useTranslation } from "react-i18next";

import { Button } from "@/components/ui/button";
import {
  ApiError,
  deleteMode as apiDeleteMode,
  upsertMode as apiUpsertMode,
  listModes,
  type ModeInfo,
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
  const [error, setError] = useState<string | null>(null);
  const [draft, setDraft] = useState<Draft | null>(null);
  const [busy, setBusy] = useState(false);
  const [confirmDelete, setConfirmDelete] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    try {
      setModes(await listModes(token));
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
          {modes.map((mode) => (
            <div key={mode.name} className="flex items-center gap-3 px-5 py-3">
              <SlidersHorizontal className="h-4 w-4 shrink-0 text-muted-foreground" aria-hidden />
              <div className="min-w-0 flex-1">
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
              </div>
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
          ))}
        </div>
      </div>

      {draft ? (
        <ModeEditor
          draft={draft}
          busy={busy}
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

function ModeEditor({
  draft,
  busy,
  readOnlyTools,
  planTools,
  onChange,
  onSave,
  onCancel,
}: {
  draft: Draft;
  busy: boolean;
  readOnlyTools: string[];
  planTools: string[];
  onChange: (d: Draft) => void;
  onSave: () => void;
  onCancel: () => void;
}) {
  const { t } = useTranslation();
  const [toolInput, setToolInput] = useState("");

  const addTool = (raw: string) => {
    const tool = raw.trim();
    if (tool && !draft.tools.includes(tool)) onChange({ ...draft, tools: [...draft.tools, tool] });
    setToolInput("");
  };

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
        <div className="mb-3 rounded-md border border-border/50 p-2">
          <div className="flex flex-wrap gap-1.5">
            {draft.tools.map((tool) => (
              <span key={tool} className="inline-flex items-center gap-1 rounded-full bg-primary/10 px-2 py-0.5 text-[11.5px] text-primary">
                {tool}
                <button type="button" onClick={() => onChange({ ...draft, tools: draft.tools.filter((x) => x !== tool) })} aria-label={t("settings.modes.removeTool")}>
                  <X className="h-3 w-3" aria-hidden />
                </button>
              </span>
            ))}
            <input
              value={toolInput}
              onChange={(e) => setToolInput(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter" || e.key === ",") {
                  e.preventDefault();
                  addTool(toolInput);
                }
              }}
              placeholder={t("settings.modes.toolPlaceholder")}
              className="min-w-[8rem] flex-1 bg-transparent px-1 py-0.5 font-mono text-[12px] text-foreground focus:outline-none"
            />
          </div>
        </div>
      ) : null}

      <label className="mb-4 block text-[12px] font-medium text-muted-foreground">
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

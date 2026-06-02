import { useCallback, useEffect, useState } from "react";
import { Loader2, Sparkles } from "lucide-react";
import { useTranslation } from "react-i18next";

import { MarkdownText, preloadMarkdownText } from "@/components/MarkdownText";
import { Button } from "@/components/ui/button";
import { Textarea } from "@/components/ui/textarea";
import {
  ApiError,
  getSkill,
  listSkills,
  saveSkill,
  setSkillMode,
  type SkillDetail,
  type SkillRow,
} from "@/lib/api";
import { useClient } from "@/providers/ClientProvider";
import { cn } from "@/lib/utils";

function errMsg(e: unknown): string {
  return e instanceof ApiError ? `HTTP ${e.status}` : (e as Error).message;
}

/** Drop the leading YAML frontmatter block so the View renders just the body. */
function stripFrontmatter(md: string): string {
  const m = /^---\s*\r?\n[\s\S]*?\r?\n---\s*\r?\n?/.exec(md);
  return m ? md.slice(m[0].length) : md;
}

function ModeBadge({ mode }: { mode: "auto" | "manual" }) {
  return (
    <span
      className={cn(
        "shrink-0 rounded-full px-2 py-0.5 text-[11px] font-medium leading-none",
        mode === "auto"
          ? "bg-primary/10 text-primary"
          : "bg-muted text-muted-foreground",
      )}
    >
      {mode}
    </span>
  );
}

/**
 * Skills — a top-level surface (peer of Chat and the Memory graph), not a
 * settings section. Skills are procedural memory: a library the user reads,
 * edits, and the agent evolves. Master-detail: the list stays on the left, the
 * selected skill's detail fills the right pane (View renders markdown via the
 * chat's MarkdownText; Edit is the raw source, manual skills only). On narrow
 * screens it drills in. A skill is conceptually a directory; today the surface
 * edits its SKILL.md — a per-skill file tree (plugins with scripts) is a later
 * phase that this workspace layout leaves room for.
 */
export function SkillsView() {
  const { token } = useClient();
  const { t } = useTranslation();
  const [rows, setRows] = useState<SkillRow[] | null>(null);
  const [selected, setSelected] = useState<string | null>(null);
  const [detail, setDetail] = useState<SkillDetail | null>(null);
  const [draft, setDraft] = useState("");
  const [tab, setTab] = useState<"view" | "edit">("view");
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    preloadMarkdownText();
  }, []);

  const refresh = useCallback(async () => {
    setError(null);
    try {
      setRows(await listSkills(token));
    } catch (e) {
      setError(errMsg(e));
    } finally {
      setLoading(false);
    }
  }, [token]);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  const dirty = detail != null && draft !== detail.content;

  const open = useCallback(
    async (name: string) => {
      setError(null);
      try {
        const d = await getSkill(token, name);
        setSelected(name);
        setDetail(d);
        setDraft(d.content);
        setTab("view");
      } catch (e) {
        setError(errMsg(e));
      }
    },
    [token],
  );

  const guardDirty = useCallback(
    () => !dirty || window.confirm(t("skills.discardPrompt")),
    [dirty, t],
  );

  const select = useCallback(
    (name: string) => {
      if (name === selected) return;
      if (!guardDirty()) return;
      void open(name);
    },
    [selected, guardDirty, open],
  );

  const back = useCallback(() => {
    if (!guardDirty()) return;
    setSelected(null);
    setDetail(null);
  }, [guardDirty]);

  const save = useCallback(async () => {
    if (!detail) return;
    setBusy(true);
    setError(null);
    try {
      await saveSkill(token, detail.name, draft);
      setDetail({ ...detail, content: draft });
      await refresh();
    } catch (e) {
      setError(errMsg(e));
    } finally {
      setBusy(false);
    }
  }, [token, detail, draft, refresh]);

  const toggleMode = useCallback(async () => {
    if (!detail) return;
    const next = detail.mode === "auto" ? "manual" : "auto";
    setBusy(true);
    setError(null);
    try {
      await setSkillMode(token, detail.name, next);
      const d = await getSkill(token, detail.name);
      setDetail(d);
      setDraft(d.content);
      if (next !== "manual") setTab("view");
      await refresh();
    } catch (e) {
      setError(errMsg(e));
    } finally {
      setBusy(false);
    }
  }, [token, detail, refresh]);

  const list = rows ?? [];
  const current = list.find((r) => r.name === selected) ?? null;
  const editable = detail?.mode === "manual";

  return (
    <div className="flex h-full min-h-0 flex-col bg-background">
      <header className="flex shrink-0 items-center gap-2 border-b border-border/40 px-3 py-2">
        <Sparkles className="h-4 w-4 text-muted-foreground" aria-hidden />
        <h1 className="text-sm font-semibold">{t("skills.title")}</h1>
        {rows ? (
          <span className="text-xs text-muted-foreground">{list.length}</span>
        ) : null}
        {error ? (
          <span className="ml-auto truncate text-xs text-destructive">{error}</span>
        ) : null}
      </header>

      {loading ? (
        <div className="flex flex-1 items-center justify-center text-sm text-muted-foreground">
          <Loader2 className="mr-2 h-4 w-4 animate-spin" />
          {t("settings.status.loading")}
        </div>
      ) : (
        <div className="grid min-h-0 flex-1 grid-cols-1 md:grid-cols-[minmax(0,20rem)_1fr]">
          {/* Master — skill list */}
          <aside
            className={cn(
              "min-h-0 overflow-y-auto md:border-r md:border-border/40",
              selected ? "hidden md:block" : "block",
            )}
          >
            {list.length === 0 ? (
              <p className="p-4 text-[13px] text-muted-foreground">
                {t("skills.empty")}
              </p>
            ) : (
              list.map((row) => (
                <button
                  key={row.name}
                  type="button"
                  onClick={() => select(row.name)}
                  className={cn(
                    "flex w-full flex-col gap-1 border-b border-border/30 px-4 py-3 text-left transition-colors",
                    row.name === selected ? "bg-primary/10" : "hover:bg-muted/40",
                  )}
                >
                  <span className="flex items-center justify-between gap-2">
                    <span className="truncate text-[14px] font-medium text-foreground">
                      {row.name}
                    </span>
                    <ModeBadge mode={row.mode} />
                  </span>
                  <span className="truncate text-[12px] text-muted-foreground">
                    {row.source}
                    {row.provenance?.source ? ` · ${row.provenance.source}` : ""}
                  </span>
                </button>
              ))
            )}
          </aside>

          {/* Detail — selected skill */}
          <section
            className={cn(
              "min-h-0 min-w-0 flex-col",
              selected ? "flex" : "hidden md:flex",
            )}
          >
            {!detail ? (
              <div className="flex flex-1 items-center justify-center text-[13px] text-muted-foreground">
                {t("skills.selectPrompt")}
              </div>
            ) : (
              <>
                <div className="flex shrink-0 flex-wrap items-center gap-x-3 gap-y-2 border-b border-border/30 px-4 py-3 sm:px-6">
                  <button
                    type="button"
                    onClick={back}
                    className="text-[13px] text-muted-foreground hover:text-foreground md:hidden"
                  >
                    &larr; {t("skills.back")}
                  </button>
                  <span className="text-[15px] font-semibold text-foreground">
                    {detail.name}
                  </span>
                  <ModeBadge mode={detail.mode} />
                  {current?.provenance?.source ? (
                    <span className="truncate text-[12px] text-muted-foreground">
                      from {current.provenance.source}
                      {current.provenance.created_at
                        ? ` · ${current.provenance.created_at}`
                        : ""}
                    </span>
                  ) : null}

                  <div className="ml-auto flex items-center gap-2">
                    <div className="inline-flex rounded-[8px] border border-border/60 p-0.5">
                      <button
                        type="button"
                        onClick={() => setTab("view")}
                        className={cn(
                          "rounded-[6px] px-2.5 py-1 text-[12px] transition-colors",
                          tab === "view"
                            ? "bg-primary/10 text-primary"
                            : "text-muted-foreground hover:text-foreground",
                        )}
                      >
                        {t("skills.view")}
                      </button>
                      <button
                        type="button"
                        onClick={() => editable && setTab("edit")}
                        disabled={!editable}
                        title={editable ? undefined : t("skills.editHint")}
                        className={cn(
                          "rounded-[6px] px-2.5 py-1 text-[12px] transition-colors disabled:opacity-40",
                          tab === "edit"
                            ? "bg-primary/10 text-primary"
                            : "text-muted-foreground hover:text-foreground",
                        )}
                      >
                        {t("settings.actions.edit")}
                      </button>
                    </div>
                    <Button
                      variant="outline"
                      size="sm"
                      disabled={busy}
                      onClick={() => void toggleMode()}
                    >
                      {detail.mode === "auto"
                        ? t("skills.makeManual")
                        : t("skills.makeAuto")}
                    </Button>
                  </div>
                </div>

                <div className="min-h-0 flex-1 overflow-y-auto px-4 py-4 sm:px-6">
                  {tab === "view" ? (
                    <div className="max-w-[78ch] text-[14px] leading-relaxed">
                      <MarkdownText>{stripFrontmatter(detail.content)}</MarkdownText>
                    </div>
                  ) : (
                    <Textarea
                      value={draft}
                      onChange={(e) => setDraft(e.target.value)}
                      spellCheck={false}
                      className="h-full min-h-[24rem] w-full resize-none font-mono text-[12px] leading-relaxed"
                    />
                  )}
                </div>

                {tab === "edit" && editable ? (
                  <div className="flex shrink-0 items-center gap-3 border-t border-border/30 px-4 py-3 sm:px-6">
                    <Button size="sm" disabled={!dirty || busy} onClick={() => void save()}>
                      {t("settings.actions.save")}
                    </Button>
                    {dirty ? (
                      <span className="text-[12px] text-muted-foreground">
                        {t("settings.status.unsaved")}
                      </span>
                    ) : null}
                  </div>
                ) : null}

                {!editable ? (
                  <div className="shrink-0 border-t border-border/30 px-4 py-3 text-[12px] text-muted-foreground sm:px-6">
                    {t("skills.autoReadonly")}
                  </div>
                ) : null}
              </>
            )}
          </section>
        </div>
      )}
    </div>
  );
}

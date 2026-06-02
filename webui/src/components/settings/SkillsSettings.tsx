import { useCallback, useEffect, useState } from "react";
import { Loader2 } from "lucide-react";
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
import { cn } from "@/lib/utils";

import { settingsCardClass, SettingsSectionTitle } from "./primitives";

function errMsg(e: unknown): string {
  return e instanceof ApiError ? `HTTP ${e.status}` : (e as Error).message;
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
 * Skills panel — master-detail. The list (master) stays on the left; the
 * selected skill's detail renders in a fixed right pane, so clicking a row
 * never jumps the viewport. The detail offers a View (rendered markdown,
 * reusing the chat's MarkdownText) / Edit (raw source) toggle; editing is
 * only enabled for `manual` skills. On narrow screens it drills in: the list
 * is replaced by the detail with a Back affordance.
 */
export function SkillsSettings({ token }: { token: string }) {
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
    setLoading(true);
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
    () => !dirty || window.confirm(t("settings.skills.discardPrompt")),
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

  if (loading) {
    return (
      <div className="flex h-40 items-center justify-center text-sm text-muted-foreground">
        <Loader2 className="mr-2 h-4 w-4 animate-spin" />
        {t("settings.status.loading")}
      </div>
    );
  }

  const list = rows ?? [];
  const current = list.find((r) => r.name === selected) ?? null;
  const editable = detail?.mode === "manual";

  return (
    <div>
      <SettingsSectionTitle>{t("settings.nav.skills")}</SettingsSectionTitle>
      {error ? <p className="mb-2 px-1 text-sm text-destructive">{error}</p> : null}

      <div className={cn(settingsCardClass, "grid md:grid-cols-[minmax(0,15rem)_1fr]")}>
        {/* Master — skill list */}
        <aside
          className={cn(
            "md:border-r md:border-border/45",
            selected ? "hidden md:block" : "block",
          )}
        >
          <div className="max-h-[30rem] overflow-y-auto">
            {list.length === 0 ? (
              <p className="p-4 text-[13px] text-muted-foreground">
                {t("settings.skills.empty")}
              </p>
            ) : (
              list.map((row) => (
                <button
                  key={row.name}
                  type="button"
                  onClick={() => select(row.name)}
                  className={cn(
                    "flex w-full flex-col gap-1 border-b border-border/40 px-4 py-3 text-left transition-colors last:border-b-0",
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
          </div>
        </aside>

        {/* Detail — selected skill */}
        <section
          className={cn("min-w-0 p-4 sm:p-5", selected ? "block" : "hidden md:block")}
        >
          {!detail ? (
            <div className="flex h-full min-h-[12rem] items-center justify-center text-[13px] text-muted-foreground">
              {t("settings.skills.selectPrompt")}
            </div>
          ) : (
            <div className="space-y-3">
              <div className="flex flex-wrap items-center gap-x-3 gap-y-2">
                <button
                  type="button"
                  onClick={back}
                  className="text-[13px] text-muted-foreground hover:text-foreground md:hidden"
                >
                  &larr; {t("settings.skills.back")}
                </button>
                <span className="text-[15px] font-semibold text-foreground">
                  {detail.name}
                </span>
                <ModeBadge mode={detail.mode} />

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
                      {t("settings.skills.view")}
                    </button>
                    <button
                      type="button"
                      onClick={() => editable && setTab("edit")}
                      disabled={!editable}
                      title={editable ? undefined : t("settings.skills.editHint")}
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
                      ? t("settings.skills.makeManual")
                      : t("settings.skills.makeAuto")}
                  </Button>
                </div>
              </div>

              {current?.provenance?.source ? (
                <p className="text-[12px] text-muted-foreground">
                  from {current.provenance.source}
                  {current.provenance.created_at
                    ? ` · ${current.provenance.created_at}`
                    : ""}
                </p>
              ) : null}

              {tab === "view" ? (
                <div className="max-h-[26rem] overflow-y-auto rounded-[8px] border border-border/45 bg-background/40 px-4 py-3 text-[14px] leading-relaxed">
                  <MarkdownText>{detail.content}</MarkdownText>
                </div>
              ) : (
                <Textarea
                  value={draft}
                  onChange={(e) => setDraft(e.target.value)}
                  spellCheck={false}
                  className="h-[26rem] resize-none font-mono text-[12px] leading-relaxed"
                />
              )}

              {tab === "edit" ? (
                <div className="flex items-center gap-3">
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
                <p className="text-[12px] text-muted-foreground">
                  {t("settings.skills.autoReadonly")}
                </p>
              ) : null}
            </div>
          )}
        </section>
      </div>
    </div>
  );
}

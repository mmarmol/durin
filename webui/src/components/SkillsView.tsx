import { useCallback, useEffect, useState } from "react";
import { Loader2, Sparkles } from "lucide-react";
import { useTranslation } from "react-i18next";

import { MarkdownText, preloadMarkdownText } from "@/components/MarkdownText";
import { Button } from "@/components/ui/button";
import { Textarea } from "@/components/ui/textarea";
import {
  ApiError,
  approveSkill,
  getSkill,
  importSource,
  listQuarantine,
  listSkills,
  rejectSkill,
  saveSkill,
  setSkillMode,
  type QuarantineRow,
  type SkillCandidate,
  type SkillDetail,
  type SkillFinding,
  type SkillRow,
  type SkillVerdict,
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

/** The §8.C verdict, shown only when it warrants attention (caution|dangerous).
 * Safe skills get no badge — absence of a warning IS the "safe" signal, and a
 * green chip on every row would be noise. */
function VerdictBadge({ verdict }: { verdict?: SkillVerdict }) {
  const { t } = useTranslation();
  if (verdict !== "caution" && verdict !== "dangerous") return null;
  const label =
    verdict === "dangerous"
      ? t("skills.verdict.dangerous")
      : t("skills.verdict.caution");
  return (
    <span
      className={cn(
        "shrink-0 rounded-full px-2 py-0.5 text-[11px] font-medium leading-none",
        verdict === "dangerous"
          ? "bg-destructive/10 text-destructive"
          : "bg-amber-500/10 text-amber-600 dark:text-amber-400",
      )}
    >
      {label}
    </span>
  );
}

function severityClass(sev: SkillFinding["severity"]): string {
  if (sev === "dangerous" || sev === "high") return "text-destructive";
  if (sev === "caution") return "text-amber-600 dark:text-amber-400";
  return "text-muted-foreground";
}

/** The reasons behind a verdict: one line per §8.C finding (category, location,
 * detail), colored by severity. Used in the active-skill detail and inline on
 * each quarantine row. */
function FindingsList({ findings }: { findings: SkillFinding[] }) {
  const { t } = useTranslation();
  if (findings.length === 0) {
    return (
      <p className="text-[12px] text-muted-foreground">{t("skills.noFindings")}</p>
    );
  }
  return (
    <ul className="flex flex-col gap-1.5">
      {findings.map((f, i) => (
        <li key={`${f.category}-${f.where}-${i}`} className="text-[12px] leading-snug">
          <span className={cn("font-medium", severityClass(f.severity))}>
            {f.category}
          </span>
          <span className="text-muted-foreground"> · {f.where}</span>
          <div className="text-muted-foreground">{f.detail}</div>
        </li>
      ))}
    </ul>
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
  const [quarantine, setQuarantine] = useState<QuarantineRow[] | null>(null);
  const [pane, setPane] = useState<"active" | "quarantine">("active");
  const [selected, setSelected] = useState<string | null>(null);
  const [detail, setDetail] = useState<SkillDetail | null>(null);
  const [draft, setDraft] = useState("");
  const [tab, setTab] = useState<"view" | "edit">("view");
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [importSrc, setImportSrc] = useState("");
  const [importing, setImporting] = useState(false);
  const [importMsg, setImportMsg] = useState<string | null>(null);
  const [picker, setPicker] = useState<SkillCandidate[] | null>(null);
  const [acting, setActing] = useState<string | null>(null);
  const [gate, setGate] = useState<{ name: string; action: "confirm" | "block" } | null>(null);

  useEffect(() => {
    preloadMarkdownText();
  }, []);

  const refresh = useCallback(async () => {
    setError(null);
    try {
      const [skills, quar] = await Promise.all([
        listSkills(token),
        listQuarantine(token),
      ]);
      setRows(skills);
      setQuarantine(quar);
    } catch (e) {
      setError(errMsg(e));
    } finally {
      setLoading(false);
    }
  }, [token]);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  const doImport = useCallback(
    async (source: string) => {
      const src = source.trim();
      if (!src) return;
      setImporting(true);
      setImportMsg(null);
      setPicker(null);
      try {
        const res = await importSource(token, src);
        if (res.candidates && res.candidates.length > 0) {
          setPicker(res.candidates);
        } else if (res.quarantined) {
          setImportSrc("");
          setPane("quarantine"); // show where it landed
          await refresh();
        } else {
          setImportMsg(res.unresolved_reason || t("skills.import.unresolved"));
        }
      } catch (e) {
        setImportMsg(errMsg(e));
      } finally {
        setImporting(false);
      }
    },
    [token, refresh, t],
  );

  // The gate is server-side: approve, and react to what it asks for. A safe,
  // trusted skill installs straight away; otherwise the server says it needs
  // confirmation (code/caution/out-of-allowlist) or a dangerous override, and
  // we surface that as an INLINE prompt on the row (no native dialog).
  const approve = useCallback(
    async (name: string) => {
      setActing(name);
      setImportMsg(null);
      try {
        const res = await approveSkill(token, name);
        if (res.ok) {
          setGate(null);
          await refresh();
        } else if (res.refused === "confirm" || res.refused === "block") {
          setGate({ name, action: res.refused });
        } else if (res.message || res.error) {
          setImportMsg(res.message || res.error || null);
        }
      } catch (e) {
        setImportMsg(errMsg(e));
      } finally {
        setActing(null);
      }
    },
    [token, refresh],
  );

  const confirmGate = useCallback(async () => {
    if (!gate) return;
    const { name, action } = gate;
    setActing(name);
    try {
      const res = await approveSkill(
        token, name, action === "block" ? { override: true } : { confirm: true });
      if (res.ok) {
        setGate(null);
        await refresh();
      } else if (res.message || res.error) {
        setImportMsg(res.message || res.error || null);
      }
    } catch (e) {
      setImportMsg(errMsg(e));
    } finally {
      setActing(null);
    }
  }, [token, gate, refresh]);

  const reject = useCallback(
    async (name: string) => {
      setActing(name);
      try {
        await rejectSkill(token, name);
        await refresh();
      } catch (e) {
        setImportMsg(errMsg(e));
      } finally {
        setActing(null);
      }
    },
    [token, refresh],
  );

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
  const quarList = quarantine ?? [];
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
            {/* Import — a surface action (bring a skill in); the result lands
                in Quarantine. Source-agnostic: path, URL, or github:owner/repo. */}
            <div className="border-b border-border/30 p-3">
              <form
                className="flex flex-col gap-2"
                onSubmit={(e) => {
                  e.preventDefault();
                  void doImport(importSrc);
                }}
              >
                <div className="flex gap-2">
                  <input
                    type="text"
                    value={importSrc}
                    onChange={(e) => setImportSrc(e.target.value)}
                    placeholder={t("skills.import.placeholder")}
                    className="min-w-0 flex-1 rounded-[8px] border border-border/60 bg-background px-2.5 py-1.5 text-[12px] outline-none focus:border-primary/60"
                  />
                  <Button type="submit" size="sm" disabled={importing || !importSrc.trim()}>
                    {importing ? (
                      <Loader2 className="h-4 w-4 animate-spin" />
                    ) : (
                      t("skills.import.button")
                    )}
                  </Button>
                </div>
                {importMsg ? (
                  <p className="text-[12px] text-muted-foreground">{importMsg}</p>
                ) : null}
              </form>

              {/* Picker: when a source resolves to several skills, choose one. */}
              {picker && picker.length > 0 ? (
                <div className="mt-2 flex flex-col gap-1 rounded-[8px] border border-border/40 bg-muted/20 p-2">
                  <p className="mb-1 text-[11px] font-semibold uppercase tracking-wide text-muted-foreground">
                    {t("skills.import.pick")}
                  </p>
                  {picker.map((c) => (
                    <button
                      key={c.ref}
                      type="button"
                      onClick={() => void doImport(c.ref)}
                      disabled={importing}
                      className="flex flex-col items-start rounded-[6px] px-2 py-1.5 text-left hover:bg-muted/50 disabled:opacity-50"
                    >
                      <span className="text-[13px] font-medium text-foreground">{c.name}</span>
                      <span className="truncate text-[11px] text-muted-foreground">{c.ref}</span>
                    </button>
                  ))}
                </div>
              ) : null}
            </div>

            {/* Tabs: the active inventory vs the §8.C import quarantine. */}
            <div className="sticky top-0 z-10 flex gap-1 border-b border-border/30 bg-background/95 p-2 backdrop-blur supports-[backdrop-filter]:bg-background/80">
              <button
                type="button"
                onClick={() => setPane("active")}
                className={cn(
                  "flex flex-1 items-center justify-center gap-1.5 rounded-[6px] px-2 py-1 text-[12px] transition-colors",
                  pane === "active"
                    ? "bg-primary/10 text-primary"
                    : "text-muted-foreground hover:text-foreground",
                )}
              >
                {t("skills.tab.active")}
                <span className="opacity-60">{list.length}</span>
              </button>
              <button
                type="button"
                onClick={() => setPane("quarantine")}
                className={cn(
                  "flex flex-1 items-center justify-center gap-1.5 rounded-[6px] px-2 py-1 text-[12px] transition-colors",
                  pane === "quarantine"
                    ? "bg-primary/10 text-primary"
                    : "text-muted-foreground hover:text-foreground",
                )}
              >
                {t("skills.tab.quarantine")}
                <span className={cn(quarList.length > 0 ? "text-destructive" : "opacity-60")}>
                  {quarList.length}
                </span>
              </button>
            </div>

            {pane === "active" ? (
              list.length === 0 ? (
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
                      <span className="flex shrink-0 items-center gap-1.5">
                        <VerdictBadge verdict={row.verdict} />
                        <ModeBadge mode={row.mode} />
                      </span>
                    </span>
                    <span className="truncate text-[12px] text-muted-foreground">
                      {row.source}
                      {row.provenance?.source ? ` · ${row.provenance.source}` : ""}
                    </span>
                  </button>
                ))
              )
            ) : quarList.length === 0 ? (
              <p className="p-4 text-[13px] text-muted-foreground">
                {t("skills.quarantineEmpty")}
              </p>
            ) : (
              quarList.map((q) => (
                <div
                  key={q.name}
                  className="flex flex-col gap-1.5 border-b border-border/30 px-4 py-3"
                >
                  <span className="flex items-center justify-between gap-2">
                    <span className="truncate text-[14px] font-medium text-foreground">
                      {q.name}
                    </span>
                    <VerdictBadge verdict={q.verdict} />
                  </span>
                  {q.source ? (
                    <span className="truncate text-[12px] text-muted-foreground">
                      {q.source}
                    </span>
                  ) : null}
                  <FindingsList findings={q.findings} />
                  {gate?.name === q.name ? (
                    // Inline gate prompt — no native dialog. The button is
                    // destructive when forcing a dangerous-verdict install.
                    <div className="flex flex-col gap-2 rounded-[8px] border border-border/60 bg-muted/40 p-2">
                      <p className="text-[12px] text-foreground">
                        {gate.action === "block"
                          ? t("skills.import.forceDangerous")
                          : t("skills.import.confirmInstall")}
                      </p>
                      <div className="flex gap-2">
                        <Button
                          size="sm"
                          variant={gate.action === "block" ? "destructive" : "default"}
                          disabled={acting === q.name}
                          onClick={() => void confirmGate()}
                        >
                          {gate.action === "block"
                            ? t("skills.import.force")
                            : t("skills.import.confirm")}
                        </Button>
                        <Button
                          size="sm"
                          variant="outline"
                          disabled={acting === q.name}
                          onClick={() => setGate(null)}
                        >
                          {t("skills.import.cancel")}
                        </Button>
                      </div>
                    </div>
                  ) : (
                    <div className="flex gap-2 pt-1">
                      <Button
                        size="sm"
                        disabled={acting === q.name}
                        onClick={() => void approve(q.name)}
                      >
                        {t("skills.import.approve")}
                      </Button>
                      <Button
                        variant="outline"
                        size="sm"
                        disabled={acting === q.name}
                        onClick={() => void reject(q.name)}
                      >
                        {t("skills.import.reject")}
                      </Button>
                    </div>
                  )}
                </div>
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
                  <VerdictBadge verdict={current?.verdict} />
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

                {current && current.findings && current.findings.length > 0 ? (
                  <div className="shrink-0 border-b border-border/30 bg-amber-500/5 px-4 py-3 sm:px-6">
                    <p className="mb-1.5 text-[11px] font-semibold uppercase tracking-wide text-muted-foreground">
                      {t("skills.security")}
                    </p>
                    <FindingsList findings={current.findings} />
                  </div>
                ) : null}

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

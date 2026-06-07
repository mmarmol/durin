import { useCallback, useEffect, useState } from "react";
import {
  ChevronDown,
  ChevronRight,
  Loader2,
  Plus,
  Shield,
  ShieldAlert,
  ShieldCheck,
  ShieldX,
  Sparkles,
} from "lucide-react";
import { useTranslation } from "react-i18next";

import { MarkdownText, preloadMarkdownText } from "@/components/MarkdownText";
import { Button } from "@/components/ui/button";
import { Textarea } from "@/components/ui/textarea";
import {
  addTrustPattern,
  ApiError,
  approveSkill,
  describeSkill,
  getSkill,
  importSource,
  listQuarantine,
  listSkills,
  rejectSkill,
  saveSkill,
  searchSkills,
  setSkillMode,
  type QuarantineRow,
  type SkillCandidate,
  type SkillDetail,
  type SkillFinding,
  type SkillRow,
  type SkillSearchHit,
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

/** Which pane fills the right side. The left column is navigation only; every
 * piece of work — reading/editing a skill, triaging a pending import, or
 * acquiring a new one — happens here with room to breathe. */
type RightPane =
  | { kind: "empty" }
  | { kind: "acquire" }
  | { kind: "skill"; name: string }
  | { kind: "triage"; name: string };

function ModeBadge({ mode }: { mode: "auto" | "manual" }) {
  const { t } = useTranslation();
  return (
    <span
      title={t(mode === "auto" ? "skills.modeAutoHint" : "skills.modeManualHint")}
      className={cn(
        "shrink-0 cursor-help rounded-full px-2 py-0.5 text-[11px] font-medium leading-none",
        mode === "auto"
          ? "bg-primary/10 text-primary"
          : "bg-muted text-muted-foreground",
      )}
    >
      {mode}
    </span>
  );
}

/** The §8.C verdict for an ACTIVE skill, shown only when it warrants attention
 * (caution|dangerous). Safe skills get no badge — absence of a warning IS the
 * "safe" signal, and a chip on every active row would be noise. Pending imports
 * use {@link SecurityChip} instead, where the safety answer is the decision. */
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

/** The full four-state safety answer for a PENDING import: during triage the
 * user needs to see "is this safe?" explicitly, including the positive "safe"
 * case (an affirmation, not the absence of a badge) and the rare "not analyzed"
 * case (no `.scan.json`). Staying within durin's one-accent palette, the states
 * are distinguished by icon + label, with colour only for the two that warn. */
function SecurityChip({ verdict }: { verdict?: SkillVerdict }) {
  const { t } = useTranslation();
  const v = verdict || "unscanned";
  const map = {
    safe: { Icon: ShieldCheck, label: t("skills.verdict.safe"), cls: "bg-primary/10 text-primary" },
    caution: { Icon: ShieldAlert, label: t("skills.verdict.caution"), cls: "bg-amber-500/10 text-amber-600 dark:text-amber-400" },
    dangerous: { Icon: ShieldX, label: t("skills.verdict.dangerous"), cls: "bg-destructive/10 text-destructive" },
    unscanned: { Icon: Shield, label: t("skills.verdict.unscanned"), cls: "bg-muted text-muted-foreground" },
  } as const;
  const { Icon, label, cls } = map[v as keyof typeof map] ?? map.unscanned;
  return (
    <span
      className={cn(
        "inline-flex shrink-0 items-center gap-1 rounded-full px-2 py-0.5 text-[11px] font-medium leading-none",
        cls,
      )}
    >
      <Icon className="h-3 w-3" aria-hidden />
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
 * detail), colored by severity. */
function FindingsList({ findings }: { findings: SkillFinding[] }) {
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

/** The security report for a pending import, picking the right shape for the
 * verdict: concrete findings, a clean-scan affirmation, or a not-analyzed hint. */
function SecurityReport({ row }: { row: QuarantineRow }) {
  const { t } = useTranslation();
  if (row.findings.length > 0) return <FindingsList findings={row.findings} />;
  if (row.verdict === "safe") {
    return (
      <p className="flex items-center gap-1.5 text-[12px] text-muted-foreground">
        <ShieldCheck className="h-3.5 w-3.5 text-primary" aria-hidden />
        {t("skills.scanClean")}
      </p>
    );
  }
  if (!row.verdict) {
    return <p className="text-[12px] text-muted-foreground">{t("skills.unscannedHint")}</p>;
  }
  return <p className="text-[12px] text-muted-foreground">{t("skills.noFindings")}</p>;
}

/**
 * Skills — a top-level surface (peer of Chat and the Memory graph), not a
 * settings section. Skills are procedural memory: a library the user reads,
 * edits, and the agent evolves.
 *
 * Master-detail with one spatial model: the LEFT column is navigation only —
 * the Active library and the Pending import queue, switched by a segment. The
 * RIGHT pane is where work happens, in one of four modes: read/edit a skill,
 * triage a pending import (security report + actions, with room), acquire a new
 * skill (import + registry search), or the empty prompt. On narrow screens it
 * drills in. A skill is conceptually a directory; today the surface edits its
 * SKILL.md — a per-skill file tree is a later phase this layout leaves room for.
 */
export function SkillsView() {
  const { token, client } = useClient();
  const { t } = useTranslation();
  const [rows, setRows] = useState<SkillRow[] | null>(null);
  const [quarantine, setQuarantine] = useState<QuarantineRow[] | null>(null);
  const [listTab, setListTab] = useState<"active" | "pending">("active");
  const [pane, setPane] = useState<RightPane>({ kind: "empty" });
  const [detail, setDetail] = useState<SkillDetail | null>(null);
  const [draft, setDraft] = useState("");
  const [tab, setTab] = useState<"view" | "edit">("view");
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [importSrc, setImportSrc] = useState("");
  const [importing, setImporting] = useState(false);
  const [importMsg, setImportMsg] = useState<string | null>(null);
  const [auditMsg, setAuditMsg] = useState<{ kind: "summary" | "error"; text: string } | null>(null);
  const [auditLive, setAuditLive] = useState<string>("");
  const [picker, setPicker] = useState<SkillCandidate[] | null>(null);
  const [searchQuery, setSearchQuery] = useState("");
  const [searching, setSearching] = useState(false);
  const [searchMsg, setSearchMsg] = useState<string | null>(null);
  const [hits, setHits] = useState<SkillSearchHit[] | null>(null);
  const [sortBy, setSortBy] = useState<"installs" | "name" | "relevance">("installs");
  const [searchLimit, setSearchLimit] = useState(10);
  const [expanded, setExpanded] = useState<string | null>(null);
  const [descCache, setDescCache] = useState<Record<string, string | null>>({}); // null = loading
  const [importByRefOpen, setImportByRefOpen] = useState(false);
  const [acting, setActing] = useState<string | null>(null);
  const [gate, setGate] = useState<{
    name: string;
    confirm: boolean;
    override: boolean;
    replace: boolean;
    ask: "confirm" | "block" | "exists";
  } | null>(null);

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
          setListTab("pending"); // show where it landed
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

  // Registry search is read-only: it never installs. Each hit's "Import"
  // button feeds its `ref` into the same `doImport` flow the manual input
  // uses (resolve → quarantine → gate), so there's one import path.
  const doSearch = useCallback(
    async (query: string, limit = 10) => {
      const q = query.trim();
      if (!q) return;
      setSearching(true);
      setSearchMsg(null);
      try {
        const res = await searchSkills(token, q, limit);
        setHits(res.hits);
        setSearchLimit(limit);
        if (res.hits.length === 0) setSearchMsg(t("skills.search.empty"));
      } catch (e) {
        setHits(null);
        setSearchMsg(errMsg(e));
      } finally {
        setSearching(false);
      }
    },
    [token, t],
  );

  // Lazy SKILL.md description peek on expand: clawhub hits already carry one;
  // github hits fetch it once and cache it per ref (null while loading).
  const toggleExpand = useCallback(
    async (hit: SkillSearchHit) => {
      if (expanded === hit.ref) {
        setExpanded(null);
        return;
      }
      setExpanded(hit.ref);
      if (hit.registry === "clawhub" || !hit.ref.startsWith("github:")) return;
      if (descCache[hit.ref] !== undefined) return;
      setDescCache((c) => ({ ...c, [hit.ref]: null }));
      const r = await describeSkill(token, hit.ref);
      setDescCache((c) => ({ ...c, [hit.ref]: r.description }));
    },
    [token, expanded, descCache],
  );

  // The gate is server-side: approve, and react to what it asks for. A safe,
  // trusted skill installs straight away; otherwise the server says it needs
  // confirmation (code/caution/out-of-allowlist) or a dangerous override, and
  // we surface that as an INLINE prompt in the triage pane (no native dialog).
  const approve = useCallback(
    async (name: string) => {
      setActing(name);
      setImportMsg(null);
      try {
        const res = await approveSkill(token, name);
        if (res.ok) {
          setGate(null);
          setPane({ kind: "empty" });
          await refresh();
        } else if (res.refused === "confirm" || res.refused === "block" || res.refused === "exists") {
          setGate({ name, confirm: false, override: false, replace: false, ask: res.refused });
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

  // Accept the gate's current ask, accumulate the matching flag, and retry —
  // chaining if the server then asks for another (e.g. confirm -> exists).
  const confirmGate = useCallback(async () => {
    if (!gate) return;
    const next = {
      ...gate,
      confirm: gate.confirm || gate.ask === "confirm",
      override: gate.override || gate.ask === "block",
      replace: gate.replace || gate.ask === "exists",
    };
    setActing(next.name);
    try {
      const res = await approveSkill(token, next.name, {
        confirm: next.confirm,
        override: next.override,
        replace: next.replace,
      });
      if (res.ok) {
        setGate(null);
        setPane({ kind: "empty" });
        await refresh();
      } else if (res.refused === "confirm" || res.refused === "block" || res.refused === "exists") {
        setGate({ ...next, ask: res.refused });
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
        setGate(null);
        setPane({ kind: "empty" });
        await refresh();
      } catch (e) {
        setImportMsg(errMsg(e));
      } finally {
        setActing(null);
      }
    },
    [token, refresh],
  );

  // On-demand "Audit with LLM": stream the judge over the websocket. Reasoning
  // arrives live on ``audit:<name>`` (latest line shown while it runs); the
  // terminal ``skill_audit_done`` carries the structured result.
  const judgeOne = useCallback(
    (name: string) => {
      setActing(name);
      setAuditMsg(null);
      setAuditLive("");
      const id = `audit:${name}`;
      const off = client.onChat(
        id,
        (ev: {
          event?: string;
          text?: string;
          judged?: boolean;
          summary?: string;
          error_code?: string;
        }) => {
          if (ev.event === "reasoning_delta" && ev.text) {
            setAuditLive((prev) => (prev + ev.text).slice(-280));
          } else if (ev.event === "skill_audit_done") {
            off();
            setAuditLive("");
            setActing(null);
            if (ev.judged) {
              setAuditMsg({ kind: "summary", text: ev.summary?.trim() || t("skills.audit.clean") });
            } else {
              setAuditMsg({ kind: "error", text: t(`skills.audit.${ev.error_code ?? "unreachable"}`) });
            }
            void refresh();
          }
        },
      );
      client.judgeStream(name);
    },
    [client, refresh, t],
  );

  // One-click "trust this source": append the suggested prefix to the allowlist
  // so future safe imports from it skip the confirm. Refine/remove in settings.
  const trust = useCallback(
    async (prefix: string) => {
      if (!prefix) return;
      setActing(prefix);
      setImportMsg(null);
      try {
        await addTrustPattern(token, prefix);
        setImportMsg(t("skills.import.trusted", { prefix }));
      } catch (e) {
        setImportMsg(errMsg(e));
      } finally {
        setActing(null);
      }
    },
    [token, t],
  );

  const dirty = detail != null && draft !== detail.content;

  const guardDirty = useCallback(
    () => !dirty || window.confirm(t("skills.discardPrompt")),
    [dirty, t],
  );

  const openSkill = useCallback(
    async (name: string) => {
      if (pane.kind === "skill" && pane.name === name) return;
      if (!guardDirty()) return;
      setError(null);
      try {
        const d = await getSkill(token, name);
        setDetail(d);
        setDraft(d.content);
        setTab("view");
        setPane({ kind: "skill", name });
      } catch (e) {
        setError(errMsg(e));
      }
    },
    [token, pane, guardDirty],
  );

  const openTriage = useCallback(
    (name: string) => {
      if (!guardDirty()) return;
      setGate(null);
      setImportMsg(null);
      setAuditMsg(null);
      setDetail(null);
      setPane({ kind: "triage", name });
    },
    [guardDirty],
  );

  const openAcquire = useCallback(() => {
    if (!guardDirty()) return;
    setDetail(null);
    setImportMsg(null);
    setPane({ kind: "acquire" });
  }, [guardDirty]);

  const back = useCallback(() => {
    if (!guardDirty()) return;
    setDetail(null);
    setGate(null);
    setPane({ kind: "empty" });
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
  const skillRow =
    pane.kind === "skill" ? (list.find((r) => r.name === pane.name) ?? null) : null;
  const triageRow =
    pane.kind === "triage" ? (quarList.find((q) => q.name === pane.name) ?? null) : null;
  const editable = detail?.mode === "manual";

  // Client-side sort over the loaded hits. Missing installs sort last.
  const sortedHits = (hits ?? []).slice().sort((a, b) => {
    if (sortBy === "installs") return (b.signals?.installs ?? -1) - (a.signals?.installs ?? -1);
    if (sortBy === "name") return a.name.localeCompare(b.name);
    return 0; // relevance = registry order
  });

  return (
    <div className="flex h-full min-h-0 flex-col bg-background">
      <header className="flex shrink-0 items-center gap-2 border-b border-border/40 px-3 py-2">
        <Sparkles className="h-4 w-4 text-muted-foreground" aria-hidden />
        <h1 className="text-sm font-semibold">{t("skills.title")}</h1>
        {rows ? (
          <span className="text-xs text-muted-foreground">{list.length}</span>
        ) : null}
        {error ? (
          <span className="truncate text-xs text-destructive">{error}</span>
        ) : null}
        <Button
          size="sm"
          variant={pane.kind === "acquire" ? "default" : "outline"}
          className="ml-auto"
          onClick={openAcquire}
        >
          <Plus className="mr-1 h-4 w-4" aria-hidden />
          {t("skills.add")}
        </Button>
      </header>

      {loading ? (
        <div className="flex flex-1 items-center justify-center text-sm text-muted-foreground">
          <Loader2 className="mr-2 h-4 w-4 animate-spin" />
          {t("settings.status.loading")}
        </div>
      ) : (
        <div className="grid min-h-0 flex-1 grid-cols-1 md:grid-cols-[minmax(0,20rem)_1fr]">
          {/* Master — navigation only: the Active library and Pending queue. */}
          <aside
            className={cn(
              "min-h-0 overflow-y-auto md:border-r md:border-border/40",
              pane.kind === "empty" ? "block" : "hidden md:block",
            )}
          >
            <div className="sticky top-0 z-10 flex gap-1 border-b border-border/30 bg-background/95 p-2 backdrop-blur supports-[backdrop-filter]:bg-background/80">
              <button
                type="button"
                onClick={() => setListTab("active")}
                className={cn(
                  "flex flex-1 items-center justify-center gap-1.5 rounded-[6px] px-2 py-1 text-[12px] transition-colors",
                  listTab === "active"
                    ? "bg-primary/10 text-primary"
                    : "text-muted-foreground hover:text-foreground",
                )}
              >
                {t("skills.tab.active")}
                <span className="opacity-60">{list.length}</span>
              </button>
              <button
                type="button"
                onClick={() => setListTab("pending")}
                className={cn(
                  "flex flex-1 items-center justify-center gap-1.5 rounded-[6px] px-2 py-1 text-[12px] transition-colors",
                  listTab === "pending"
                    ? "bg-primary/10 text-primary"
                    : "text-muted-foreground hover:text-foreground",
                )}
              >
                {t("skills.tab.pending")}
                <span className={cn(quarList.length > 0 ? "text-amber-600 dark:text-amber-400" : "opacity-60")}>
                  {quarList.length}
                </span>
              </button>
            </div>

            {listTab === "active" ? (
              list.length === 0 ? (
                <p className="p-4 text-[13px] text-muted-foreground">{t("skills.empty")}</p>
              ) : (
                list.map((row) => (
                  <button
                    key={row.name}
                    type="button"
                    onClick={() => void openSkill(row.name)}
                    className={cn(
                      "flex w-full flex-col gap-1 border-b border-border/30 px-4 py-3 text-left transition-colors",
                      pane.kind === "skill" && pane.name === row.name
                        ? "bg-primary/10"
                        : "hover:bg-muted/40",
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
              <p className="p-4 text-[13px] text-muted-foreground">{t("skills.pendingEmpty")}</p>
            ) : (
              quarList.map((q) => (
                <button
                  key={q.name}
                  type="button"
                  onClick={() => openTriage(q.name)}
                  className={cn(
                    "flex w-full flex-col gap-1 border-b border-border/30 px-4 py-3 text-left transition-colors",
                    pane.kind === "triage" && pane.name === q.name
                      ? "bg-primary/10"
                      : "hover:bg-muted/40",
                  )}
                >
                  <span className="flex items-center justify-between gap-2">
                    <span className="truncate text-[14px] font-medium text-foreground">
                      {q.name}
                    </span>
                    <SecurityChip verdict={q.verdict} />
                  </span>
                  <span className="truncate text-[12px] text-muted-foreground">
                    {q.source
                      ? t("skills.pendingReason", { source: q.source })
                      : t("skills.pendingReasonBare")}
                  </span>
                </button>
              ))
            )}
          </aside>

          {/* Detail — the work pane. */}
          <section
            className={cn(
              "min-h-0 min-w-0 flex-col",
              pane.kind === "empty" ? "hidden md:flex" : "flex",
            )}
          >
            {pane.kind === "empty" ? (
              <div className="flex flex-1 items-center justify-center text-[13px] text-muted-foreground">
                {t("skills.selectPrompt")}
              </div>
            ) : pane.kind === "acquire" ? (
              <div className="min-h-0 flex-1 overflow-y-auto px-4 py-4 sm:px-6">
                <button
                  type="button"
                  onClick={back}
                  className="mb-3 text-[13px] text-muted-foreground hover:text-foreground md:hidden"
                >
                  &larr; {t("skills.back")}
                </button>
                <h2 className="text-[15px] font-semibold text-foreground">
                  {t("skills.acquireTitle")}
                </h2>
                <p className="mt-1 max-w-[60ch] text-[12px] text-muted-foreground">
                  {t("skills.search.acquireExplainer")}
                </p>

                {/* Primary: search the registry. */}
                <form
                  className="mt-4 flex gap-2"
                  onSubmit={(e) => {
                    e.preventDefault();
                    void doSearch(searchQuery);
                  }}
                >
                  <input
                    type="search"
                    value={searchQuery}
                    onChange={(e) => setSearchQuery(e.target.value)}
                    placeholder={t("skills.search.placeholder")}
                    className="min-w-0 flex-1 rounded-[8px] border border-border/60 bg-background px-2.5 py-1.5 text-[12px] outline-none focus:border-primary/60"
                  />
                  <Button type="submit" size="sm" disabled={searching || !searchQuery.trim()}>
                    {searching ? (
                      <Loader2 className="h-4 w-4 animate-spin" />
                    ) : (
                      t("skills.search.button")
                    )}
                  </Button>
                </form>
                {searchMsg ? (
                  <p className="mt-1.5 text-[12px] text-muted-foreground">{searchMsg}</p>
                ) : null}

                {hits ? (
                  <div className="mt-3">
                    <div className="mb-2 flex items-center justify-between">
                      <span className="text-[12px] text-muted-foreground">
                        {t("skills.search.resultsCount", { count: hits.length })}
                      </span>
                      <label className="flex items-center gap-1.5 text-[12px] text-muted-foreground">
                        {t("skills.search.sortLabel")}
                        <select
                          value={sortBy}
                          onChange={(e) => setSortBy(e.target.value as typeof sortBy)}
                          className="rounded-[6px] border border-border/60 bg-background px-1.5 py-0.5 text-[12px]"
                        >
                          <option value="installs">{t("skills.search.sortInstalls")}</option>
                          <option value="name">{t("skills.search.sortName")}</option>
                          <option value="relevance">{t("skills.search.sortRelevance")}</option>
                        </select>
                      </label>
                    </div>
                    <div className="flex flex-col gap-1">
                      {sortedHits.map((h) => {
                        const desc =
                          h.registry === "clawhub" || !h.ref.startsWith("github:")
                            ? h.description
                            : descCache[h.ref];
                        const open = expanded === h.ref;
                        return (
                          <div
                            key={h.ref}
                            className="rounded-[8px] border border-border/40 bg-muted/20 p-2"
                          >
                            <div className="flex items-start gap-2">
                              <button
                                type="button"
                                aria-label={`expand ${h.name}`}
                                onClick={() => void toggleExpand(h)}
                                className="mt-0.5 text-muted-foreground hover:text-foreground"
                              >
                                {open ? (
                                  <ChevronDown className="h-3.5 w-3.5" />
                                ) : (
                                  <ChevronRight className="h-3.5 w-3.5" />
                                )}
                              </button>
                              <div className="flex min-w-0 flex-1 flex-col">
                                <span className="flex items-center gap-1.5">
                                  <span
                                    data-testid="hit-name"
                                    className="truncate text-[13px] font-medium text-foreground"
                                  >
                                    {h.name}
                                  </span>
                                  <span className="shrink-0 rounded-full bg-muted px-1.5 py-0.5 text-[10px] text-muted-foreground">
                                    {h.registry}
                                  </span>
                                  {typeof h.signals?.installs === "number" ? (
                                    <span className="shrink-0 text-[11px] text-muted-foreground">
                                      {t("skills.search.installs", { count: h.signals.installs })}
                                    </span>
                                  ) : null}
                                </span>
                                {open ? (
                                  <span className="mt-1 text-[12px] text-muted-foreground">
                                    {desc === null ? (
                                      <Loader2 className="h-3.5 w-3.5 animate-spin" />
                                    ) : desc ? (
                                      desc
                                    ) : (
                                      t("skills.search.noDescription")
                                    )}
                                  </span>
                                ) : null}
                                <span className="truncate text-[11px] text-muted-foreground/70">
                                  {h.ref}
                                </span>
                              </div>
                              <Button
                                type="button"
                                size="sm"
                                variant="ghost"
                                disabled={importing}
                                onClick={() => void doImport(h.ref)}
                              >
                                {t("skills.import.button")}
                              </Button>
                            </div>
                          </div>
                        );
                      })}
                    </div>
                    {hits.length >= searchLimit ? (
                      <Button
                        type="button"
                        size="sm"
                        variant="outline"
                        className="mt-2"
                        disabled={searching}
                        onClick={() => void doSearch(searchQuery, searchLimit + 10)}
                      >
                        {t("skills.search.showMore")}
                      </Button>
                    ) : null}
                  </div>
                ) : null}

                {/* Secondary: import by an explicit reference (path/URL/repo). */}
                <div className="mt-4 border-t border-border/30 pt-3">
                  <button
                    type="button"
                    onClick={() => setImportByRefOpen((v) => !v)}
                    className="flex items-center gap-1.5 text-[12px] text-muted-foreground hover:text-foreground"
                  >
                    {importByRefOpen ? (
                      <ChevronDown className="h-3.5 w-3.5" />
                    ) : (
                      <ChevronRight className="h-3.5 w-3.5" />
                    )}
                    {t("skills.search.importByRef")}
                  </button>
                  {importByRefOpen ? (
                    <>
                      <form
                        className="mt-2 flex flex-col gap-2"
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
                    </>
                  ) : null}
                </div>
              </div>
            ) : pane.kind === "triage" ? (
              !triageRow ? (
                <div className="flex flex-1 items-center justify-center text-[13px] text-muted-foreground">
                  {t("skills.selectPrompt")}
                </div>
              ) : (
                <div className="min-h-0 flex-1 overflow-y-auto px-4 py-4 sm:px-6">
                  <button
                    type="button"
                    onClick={back}
                    className="mb-3 text-[13px] text-muted-foreground hover:text-foreground md:hidden"
                  >
                    &larr; {t("skills.back")}
                  </button>
                  <div className="flex flex-wrap items-center gap-x-3 gap-y-2">
                    <span className="text-[15px] font-semibold text-foreground">
                      {triageRow.name}
                    </span>
                    <SecurityChip verdict={triageRow.verdict} />
                  </div>
                  <p className="mt-1 text-[12px] text-muted-foreground">
                    {triageRow.source
                      ? t("skills.pendingReason", { source: triageRow.source })
                      : t("skills.pendingReasonBare")}
                  </p>

                  {triageRow.reasons && triageRow.reasons.length > 0 ? (
                    <div className="mt-4">
                      <p className="mb-1.5 text-[11px] font-semibold uppercase tracking-wide text-muted-foreground">
                        {t("skills.whyHere")}
                      </p>
                      <ul className="flex flex-col gap-1">
                        {triageRow.reasons.map((r) => (
                          <li key={r.code} className="text-[12px] text-muted-foreground">
                            {t(`skills.reason.${r.code}`, { detail: r.detail ?? "" })}
                          </li>
                        ))}
                      </ul>
                    </div>
                  ) : null}

                  <div className="mt-4">
                    <p className="mb-1.5 text-[11px] font-semibold uppercase tracking-wide text-muted-foreground">
                      {t("skills.security")}
                    </p>
                    <SecurityReport row={triageRow} />
                  </div>

                  {acting === triageRow.name ? (
                    <div className="mt-3">
                      <p className="flex items-center gap-1.5 text-[12px] text-muted-foreground">
                        <Loader2 className="h-3.5 w-3.5 animate-spin" aria-hidden />
                        {t("skills.audit.running")}
                      </p>
                      {auditLive ? (
                        <p className="mt-1 line-clamp-2 text-[11px] italic text-muted-foreground/80">
                          {auditLive}
                        </p>
                      ) : null}
                    </div>
                  ) : auditMsg ? (
                    auditMsg.kind === "summary" ? (
                      <div className="mt-3">
                        <p className="text-[11px] font-semibold uppercase tracking-wide text-muted-foreground">
                          {t("skills.audit.summaryLabel")}
                        </p>
                        <p className="text-[12px] text-muted-foreground">{auditMsg.text}</p>
                      </div>
                    ) : (
                      <div className="mt-3 flex flex-wrap items-center gap-2">
                        <span className="text-[12px] text-destructive">{auditMsg.text}</span>
                        <Button
                          size="sm"
                          variant="outline"
                          disabled={acting === triageRow.name}
                          onClick={() => void judgeOne(triageRow.name)}
                        >
                          {t("skills.audit.retry")}
                        </Button>
                      </div>
                    )
                  ) : null}

                  {triageRow.install_specs && triageRow.install_specs.length > 0 ? (
                    <p className="mt-3 text-[11px] text-muted-foreground">
                      {t("skills.import.declaredDeps", {
                        deps: triageRow.install_specs.join(", "),
                      })}
                    </p>
                  ) : null}

                  {importMsg ? (
                    <p className="mt-3 text-[12px] text-muted-foreground">{importMsg}</p>
                  ) : null}

                  {gate?.name === triageRow.name ? (
                    // Inline gate prompt — no native dialog. The button is
                    // destructive when forcing a dangerous-verdict install.
                    <div className="mt-4 flex flex-col gap-2 rounded-[8px] border border-border/60 bg-muted/40 p-3">
                      <p className="text-[12px] text-foreground">
                        {gate.ask === "block"
                          ? t("skills.import.forceDangerous")
                          : gate.ask === "exists"
                            ? t("skills.import.replaceExists")
                            : t("skills.import.confirmInstall")}
                      </p>
                      <div className="flex gap-2">
                        <Button
                          size="sm"
                          variant={gate.ask === "block" ? "destructive" : "default"}
                          disabled={acting === triageRow.name}
                          onClick={() => void confirmGate()}
                        >
                          {gate.ask === "block"
                            ? t("skills.import.force")
                            : gate.ask === "exists"
                              ? t("skills.import.replace")
                              : t("skills.import.confirm")}
                        </Button>
                        <Button
                          size="sm"
                          variant="outline"
                          disabled={acting === triageRow.name}
                          onClick={() => setGate(null)}
                        >
                          {t("skills.import.cancel")}
                        </Button>
                      </div>
                    </div>
                  ) : (
                    <div className="mt-4 flex flex-wrap gap-2">
                      <Button
                        size="sm"
                        disabled={acting === triageRow.name}
                        onClick={() => void approve(triageRow.name)}
                      >
                        {t("skills.import.approve")}
                      </Button>
                      <Button
                        variant="outline"
                        size="sm"
                        disabled={acting === triageRow.name}
                        onClick={() => void reject(triageRow.name)}
                      >
                        {t("skills.import.reject")}
                      </Button>
                      {triageRow.trust_prefix ? (
                        <Button
                          variant="ghost"
                          size="sm"
                          disabled={acting === triageRow.trust_prefix}
                          onClick={() => void trust(triageRow.trust_prefix!)}
                          title={triageRow.trust_prefix}
                        >
                          {t("skills.import.trust")}
                        </Button>
                      ) : null}
                      <Button
                        variant="ghost"
                        size="sm"
                        disabled={acting === triageRow.name}
                        onClick={() => void judgeOne(triageRow.name)}
                      >
                        {t("skills.import.judge")}
                      </Button>
                    </div>
                  )}
                </div>
              )
            ) : !detail ? (
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
                  <VerdictBadge verdict={skillRow?.verdict} />
                  {skillRow?.provenance?.source ? (
                    <span className="truncate text-[12px] text-muted-foreground">
                      from {skillRow.provenance.source}
                      {skillRow.provenance.created_at
                        ? ` · ${skillRow.provenance.created_at}`
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

                {skillRow && skillRow.findings && skillRow.findings.length > 0 ? (
                  <div className="shrink-0 border-b border-border/30 bg-amber-500/5 px-4 py-3 sm:px-6">
                    <p className="mb-1.5 text-[11px] font-semibold uppercase tracking-wide text-muted-foreground">
                      {t("skills.security")}
                    </p>
                    <FindingsList findings={skillRow.findings} />
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

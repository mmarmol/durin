import { useCallback, useEffect, useState } from "react";
import {
  ChevronDown,
  ChevronRight,
  Globe,
  Loader2,
  Package,
  Plus,
  Shield,
  ShieldAlert,
  ShieldCheck,
  ShieldX,
  Sparkles,
} from "lucide-react";
import { useTranslation } from "react-i18next";

import { CodeBlock } from "@/components/CodeBlock";
import { MarkdownText, preloadMarkdownText } from "@/components/MarkdownText";
import { ApproveBlockedModal } from "@/components/ApproveBlockedModal";
import { SkillFileTree } from "@/components/SkillFileTree";
import { SkillHistory } from "@/components/SkillHistory";
import { TriageRequirements } from "@/components/TriageRequirements";
import { Button } from "@/components/ui/button";
import { Textarea } from "@/components/ui/textarea";
import { relativeTime } from "@/lib/format";
import {
  addTrustPattern,
  ApiError,
  approveSkill,
  describeSkill,
  fetchSkillObservations,
  getSkill,
  getSkillFile,
  getSkillHistory,
  importSource,
  listQuarantine,
  listSkillFiles,
  listSkills,
  rejectSkill,
  repairSkill,
  type SkillRepairResult,
  removeSkill,
  resolveSkillObservation,
  reviewSkill,
  saveSkillFile,
  searchSkills,
  setSkillMode,
  unreviewSkill,
  type QuarantineRow,
  type SkillCandidate,
  type SkillDescribeResult,
  type SkillDetail,
  type SkillFile,
  type SkillFinding,
  type SkillHistory as SkillHistoryData,
  type SkillObservation,
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

/** Map a skill file's extension to a Prism language for syntax highlighting.
 * Every value here is bundled in react-syntax-highlighter's full Prism build,
 * so no extra registration is needed; unknown extensions fall back to plain text. */
const LANGUAGE_BY_EXT: Record<string, string> = {
  py: "python",
  sh: "bash",
  bash: "bash",
  js: "javascript",
  jsx: "jsx",
  mjs: "javascript",
  cjs: "javascript",
  ts: "typescript",
  tsx: "tsx",
  json: "json",
  yaml: "yaml",
  yml: "yaml",
  toml: "toml",
  css: "css",
  html: "markup",
  xml: "markup",
  sql: "sql",
};

function languageForFile(name: string): string {
  const dot = name.lastIndexOf(".");
  const ext = dot >= 0 ? name.slice(dot + 1).toLowerCase() : "";
  return LANGUAGE_BY_EXT[ext] ?? "text";
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

/** Usage summary for an active skill's row: 30-day call count + last-used
 * relative time, when there's any usage to report. A skill never called in
 * the window renders nothing here — silence, not a "0" — to keep a healthy
 * catalog's list from being cluttered with zeros. */
function UsageLine({ useCount, lastUsedMs }: { useCount?: number | null; lastUsedMs?: number | null }) {
  const { t } = useTranslation();
  if (!useCount) return null;
  return (
    <span className="truncate text-[11px] text-muted-foreground/70">
      {t("skills.usage", { count: useCount })}
      {lastUsedMs ? (
        <span title={t("skills.lastUsedApprox")}>
          {` · ${t("skills.lastUsed", { when: relativeTime(lastUsedMs) })}`}
        </span>
      ) : (
        ""
      )}
    </span>
  );
}

/** Open-observation backlog badge — only shown when there's something to see,
 * same "absence is the healthy signal" convention as {@link VerdictBadge}. */
function ObservationsBadge({ count }: { count?: number }) {
  const { t } = useTranslation();
  if (!count) return null;
  return (
    <span className="shrink-0 rounded-full bg-amber-500/10 px-2 py-0.5 text-[11px] font-medium leading-none text-amber-600 dark:text-amber-400">
      {t("skills.openObservations", { count })}
    </span>
  );
}

/** The records behind {@link ObservationsBadge}, with manual resolution.
 * Fetches its own data (the list rows only carry the count) so the parent's
 * state stays untouched; `onResolved` lets the parent refresh the badge. */
function ObservationsPanel({
  skill,
  token,
  onResolved,
}: {
  skill: string;
  token: string;
  onResolved: () => void | Promise<void>;
}) {
  const { t } = useTranslation();
  const [obs, setObs] = useState<SkillObservation[] | null>(null);
  const [busyId, setBusyId] = useState<number | null>(null);
  const [err, setErr] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      setObs(await fetchSkillObservations(token, skill));
    } catch (e) {
      setErr(errMsg(e));
    }
  }, [token, skill]);

  useEffect(() => {
    void load();
  }, [load]);

  const resolve = async (id: number, disposition: "applied" | "declined") => {
    setBusyId(id);
    setErr(null);
    try {
      await resolveSkillObservation(token, id, disposition);
      await load();
      await onResolved();
    } catch (e) {
      setErr(errMsg(e));
    } finally {
      setBusyId(null);
    }
  };

  if (err) {
    return (
      <div className="shrink-0 border-b border-border/30 px-4 py-3 sm:px-6">
        <p className="text-[12px] text-destructive">{err}</p>
      </div>
    );
  }
  if (!obs || obs.length === 0) return null;
  return (
    <div className="shrink-0 border-b border-border/30 bg-amber-500/5 px-4 py-3 sm:px-6">
      <p className="mb-1.5 text-[11px] font-semibold uppercase tracking-wide text-muted-foreground">
        {t("skills.observations.title")}
      </p>
      <ul className="flex flex-col gap-3">
        {obs.map((o) => (
          <li key={o.id} className="flex flex-col gap-1">
            <div className="flex flex-wrap items-center gap-2">
              <span className="rounded-full bg-amber-500/10 px-2 py-0.5 text-[11px] font-medium leading-none text-amber-600 dark:text-amber-400">
                {t(`skills.observations.kinds.${o.kind}`, o.kind)}
              </span>
              <span className="text-[11px] text-muted-foreground">
                {o.count > 1
                  ? t("skills.observations.recurred", {
                      count: o.count,
                      date: o.last_seen,
                    })
                  : t("skills.observations.loggedOn", { date: o.first_seen })}
              </span>
            </div>
            <p className="text-[13px] text-foreground">{o.issue}</p>
            <p className="text-[12px] text-muted-foreground">
              {t("skills.observations.proposal")}: {o.improvement}
            </p>
            <div className="mt-1 flex gap-2">
              <Button
                variant="outline"
                size="sm"
                disabled={busyId === o.id}
                onClick={() => void resolve(o.id, "applied")}
              >
                {t("skills.observations.resolve")}
              </Button>
              <Button
                variant="ghost"
                size="sm"
                disabled={busyId === o.id}
                title={t("skills.observations.dismissHint")}
                onClick={() => void resolve(o.id, "declined")}
              >
                {t("skills.observations.dismiss")}
              </Button>
            </div>
          </li>
        ))}
      </ul>
    </div>
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

/** A user/LLM "Revisada" override badge — shown instead of the verdict badge
 * once a flagged active skill has been cleared (the underlying findings remain
 * visible in the report below). */
function ReviewedChip({ by }: { by: "user" | "llm" }) {
  const { t } = useTranslation();
  const who = by === "llm" ? t("skills.review.byLlm") : t("skills.review.byUser");
  return (
    <span className="inline-flex shrink-0 items-center gap-1 rounded-full bg-primary/10 px-2 py-0.5 text-[11px] font-medium leading-none text-primary">
      <ShieldCheck className="h-3 w-3" aria-hidden />
      {t("skills.review.reviewedBy", { by: who })}
    </span>
  );
}

/** Source registry tag shown on every search result + its detail view. durin
 * keeps a one-accent palette, so the two registries are distinguished by icon +
 * name (neutral), never by a per-source accent colour. */
function RegistryTag({ registry }: { registry: string }) {
  const Icon = registry === "clawhub" ? Package : Globe;
  return (
    <span
      title={registry}
      data-registry={registry}
      className="inline-flex shrink-0 items-center gap-1 rounded-full border border-border/60 px-1.5 py-0.5 text-[10px] text-muted-foreground"
    >
      <Icon className="h-3 w-3" aria-hidden />
      {registry}
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
// Detail/preview of a registry hit: full description + the SKILL.md body
// rendered as markdown + the complete declared requirements. Shown in place of
// the search results when a result is clicked; Import reuses the normal flow.
function SkillPreview({
  hit,
  detail,
  importing,
  onImport,
  onBack,
}: {
  hit: SkillSearchHit;
  detail: SkillDescribeResult | null | undefined;
  importing: boolean;
  onImport: () => void;
  onBack: () => void;
}) {
  const { t } = useTranslation();
  const loading = detail === null;
  const desc = detail?.description || hit.description;
  const body = detail?.body || "";
  const req = detail?.requires;
  const platforms = detail?.platforms;
  const hasReq = !!req && (req.bins.length > 0 || req.env.length > 0);
  const hasPlat = !!platforms && platforms.length > 0;
  const platLabel = (p: string) =>
    p === "macos" ? "macOS" : p === "linux" ? "Linux" : p;

  return (
    <div className="mt-4 flex flex-col gap-3">
      <div className="flex items-center justify-between">
        <button
          type="button"
          onClick={onBack}
          className="text-[12px] text-muted-foreground hover:text-foreground"
        >
          {t("skills.preview.back")}
        </button>
        <Button type="button" size="sm" disabled={importing} onClick={onImport}>
          {importing ? (
            <Loader2 className="h-3.5 w-3.5 animate-spin" />
          ) : hit.installed ? (
            t("skills.import.reinstall")
          ) : (
            t("skills.import.button")
          )}
        </Button>
      </div>

      <div className="flex flex-col">
        <span className="flex items-center gap-1.5">
          <span className="text-[14px] font-medium text-foreground">{hit.name}</span>
          <RegistryTag registry={hit.registry} />
          {hit.installed ? (
            <span className="shrink-0 rounded-full border border-border/40 bg-muted/40 px-1.5 py-0.5 text-[10px] text-muted-foreground">
              {t("skills.search.installed")}
            </span>
          ) : null}
          {typeof hit.signals?.installs === "number" ? (
            <span className="text-[11px] text-muted-foreground">
              {t("skills.search.installs", { count: hit.signals.installs })}
            </span>
          ) : null}
        </span>
        <span className="break-all text-[11px] text-muted-foreground/70">{hit.ref}</span>
      </div>

      {loading ? (
        <Loader2 className="h-4 w-4 animate-spin" />
      ) : (
        <>
          {desc ? <p className="text-[13px] text-foreground">{desc}</p> : null}

          {hasReq || hasPlat ? (
            <div className="rounded-[8px] border border-border/40 bg-muted/20 p-2 text-[12px]">
              <p className="font-medium text-foreground">{t("skills.requirements.title")}</p>
              {req && req.bins.length > 0 ? (
                <p className="text-muted-foreground">
                  {t("skills.requirements.tools")}: {req.bins.join(", ")}
                </p>
              ) : null}
              {req && req.env.length > 0 ? (
                <p className="text-muted-foreground">
                  {t("skills.requirements.environment")}: {req.env.join(", ")}
                </p>
              ) : null}
              {hasPlat ? (
                <p className="text-muted-foreground">
                  {t("skills.requirements.platform")}: {platforms!.map(platLabel).join(", ")}
                </p>
              ) : null}
            </div>
          ) : null}

          {body ? (
            <div className="rounded-[8px] border border-border/40 bg-background p-3 text-[13px]">
              <MarkdownText>{body}</MarkdownText>
            </div>
          ) : !desc && !hasReq && !hasPlat ? (
            <p className="text-[12px] text-muted-foreground">{t("skills.preview.unavailable")}</p>
          ) : null}
        </>
      )}
    </div>
  );
}

export function SkillsView({ onAskDurin }: { onAskDurin?: (binName: string) => void }) {
  const { token, client } = useClient();
  const { t } = useTranslation();
  const [rows, setRows] = useState<SkillRow[] | null>(null);
  const [quarantine, setQuarantine] = useState<QuarantineRow[] | null>(null);
  const [listTab, setListTab] = useState<"active" | "pending">("active");
  const [pane, setPane] = useState<RightPane>({ kind: "empty" });
  const [detail, setDetail] = useState<SkillDetail | null>(null);
  const [tab, setTab] = useState<"view" | "edit" | "history">("view");
  const [files, setFiles] = useState<SkillFile[]>([]);
  const [selFile, setSelFile] = useState<string>("SKILL.md");
  const [fileBody, setFileBody] = useState<string>(""); // last-loaded content of selFile
  const [fileText, setFileText] = useState<boolean>(true); // selFile is text?
  const [drafts, setDrafts] = useState<Record<string, string>>({}); // per-file unsaved edits
  const [history, setHistory] = useState<SkillHistoryData | null>(null);
  const [lintErr, setLintErr] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [importSrc, setImportSrc] = useState("");
  const [importing, setImporting] = useState(false);
  const [importingRef, setImportingRef] = useState<string | null>(null);
  const [reinstallSrc, setReinstallSrc] = useState<string | null>(null);
  const [importMsg, setImportMsg] = useState<string | null>(null);
  const [auditMsg, setAuditMsg] = useState<{ kind: "summary" | "error"; text: string } | null>(null);
  const [auditLive, setAuditLive] = useState<string>("");
  const [picker, setPicker] = useState<SkillCandidate[] | null>(null);
  const [searchQuery, setSearchQuery] = useState("");
  const [searching, setSearching] = useState(false);
  const [searchMsg, setSearchMsg] = useState<string | null>(null);
  const [hits, setHits] = useState<SkillSearchHit[] | null>(null);
  // Default to relevance: it preserves the server's rank-fair cross-source merge
  // order. Sorting by `installs` buries registries that don't report an install
  // count (e.g. clawhub) regardless of how well they match the query.
  const [sortBy, setSortBy] = useState<"installs" | "name" | "relevance">("relevance");
  const [searchLimit, setSearchLimit] = useState(10);
  const [previewHit, setPreviewHit] = useState<SkillSearchHit | null>(null);
  const [descCache, setDescCache] = useState<Record<string, SkillDescribeResult | null>>({}); // null = loading
  const [importByRefOpen, setImportByRefOpen] = useState(false);
  const [acting, setActing] = useState<string | null>(null);
  const [removeConfirm, setRemoveConfirm] = useState(false);
  const [reviewConfirm, setReviewConfirm] = useState(false);
  const [reviewNote, setReviewNote] = useState("");
  const [gate, setGate] = useState<{
    name: string;
    confirm: boolean;
    override: boolean;
    replace: boolean;
    ask: "confirm" | "block" | "exists";
  } | null>(null);
  const [showBlockedModal, setShowBlockedModal] = useState<{ name: string; bins: string[] } | null>(null);

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

  useEffect(() => {
    setRemoveConfirm(false);
  }, [pane]);

  const doImport = useCallback(
    async (source: string, replace = false) => {
      const src = source.trim();
      if (!src) return;
      setImporting(true);
      setImportingRef(src);
      setImportMsg(null);
      setReinstallSrc(null);
      setPicker(null);
      try {
        const res = await importSource(token, src, "", replace);
        if (res.candidates && res.candidates.length > 0) {
          setPicker(res.candidates);
        } else if (res.installed) {
          // gate cleared it (`allow`) → auto-installed, no manual second step
          setImportSrc("");
          setImportMsg(t("skills.import.installedOk", { name: res.installed }));
          setListTab("active");
          await refresh();
        } else if (res.already_installed) {
          // present locally — offer a re-install/override
          setImportMsg(t("skills.import.alreadyInstalled", { name: res.already_installed }));
          setReinstallSrc(src);
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
        setImportingRef(null);
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

  // Clicking a result opens its detail view; the SKILL.md description+body is
  // fetched once per ref and cached (null while loading). github + clawhub refs
  // both resolve to a fetchable SKILL.md; other refs carry their summary inline.
  const openPreview = useCallback(
    async (hit: SkillSearchHit) => {
      setPreviewHit(hit);
      if (!hit.ref.startsWith("github:") && !hit.ref.startsWith("clawhub:")) return;
      if (descCache[hit.ref] !== undefined) return;
      setDescCache((c) => ({ ...c, [hit.ref]: null }));
      const r = await describeSkill(token, hit.ref);
      setDescCache((c) => ({ ...c, [hit.ref]: r }));
    },
    [token, descCache],
  );

  // The gate is server-side: approve, and react to what it asks for. A safe,
  // trusted skill installs straight away; otherwise the server says it needs
  // confirmation (code/caution/out-of-allowlist) or a dangerous override, and
  // we surface that as an INLINE prompt in the triage pane (no native dialog).
  const approve = useCallback(
    async (name: string, opts?: { install_deps?: boolean }) => {
      setActing(name);
      setImportMsg(null);
      try {
        const res = await approveSkill(token, name, opts);
        if (res.ok) {
          setGate(null);
          setPane({ kind: "empty" });
          if (res.deps_results && res.deps_results.length > 0) {
            const failed = res.deps_results.filter((r) => !r.success);
            const ok = res.deps_results.filter((r) => r.success);
            setImportMsg(
              failed.length > 0
                ? t("skills.import.depsPartial", { ok: ok.length, failed: failed.length })
                : t("skills.import.depsInstalled", { count: ok.length }),
            );
          }
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
    [token, refresh, t],
  );

  // Repair preview for an invalid quarantined skill: preview (diff) first,
  // then apply + refresh so the cleared validation re-enables approve.
  const [repairPreview, setRepairPreview] = useState<(SkillRepairResult & { name: string }) | null>(null);
  const runRepair = useCallback(
    async (name: string, apply: boolean) => {
      setActing(name);
      try {
        const res = await repairSkill(token, name, apply);
        if (apply) {
          setRepairPreview(null);
          await refresh();
        } else {
          setRepairPreview({ ...res, name });
        }
      } finally {
        setActing(null);
      }
    },
    [token, refresh],
  );

  const triageApprove = (name: string) => {
    const row = quarList.find((r) => r.name === name);
    const req = row?.requirements;
    if (req && req.bins.some((b) => !b.available && !b.installable)) {
      setShowBlockedModal({ name, bins: req.bins.filter((b) => !b.available && !b.installable).map((b) => b.name) });
    } else {
      void approve(name, { install_deps: true });
    }
  };

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

  // Remove a workspace skill (or revert a forked builtin). The button is only
  // shown when the server marked the row `removable`, so 400/404 are defensive.
  const doRemove = useCallback(async () => {
    if (pane.kind !== "skill") return;
    const name = pane.name;
    setBusy(true);
    setError(null);
    try {
      await removeSkill(token, name);
      setRemoveConfirm(false);
      setDetail(null);
      setPane({ kind: "empty" });
      await refresh();
    } catch (e) {
      setError(errMsg(e));
    } finally {
      setBusy(false);
    }
  }, [pane, token, refresh]);

  const doReview = useCallback(async () => {
    if (pane.kind !== "skill") return;
    const name = pane.name;
    setBusy(true);
    setError(null);
    try {
      await reviewSkill(token, name, reviewNote);
      setReviewConfirm(false);
      setReviewNote("");
      await refresh();
    } catch (e) {
      setError(errMsg(e));
    } finally {
      setBusy(false);
    }
  }, [pane, token, reviewNote, refresh]);

  const doReopen = useCallback(async () => {
    if (pane.kind !== "skill") return;
    const name = pane.name;
    setBusy(true);
    setError(null);
    try {
      await unreviewSkill(token, name);
      await refresh();
    } catch (e) {
      setError(errMsg(e));
    } finally {
      setBusy(false);
    }
  }, [pane, token, refresh]);

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

  const curDraft = drafts[selFile] ?? fileBody;
  const dirty = curDraft !== fileBody;
  // Both modes are user-editable: `auto` means dream may auto-improve it, not
  // that the user is locked out. Only binary files are non-editable.
  const editable = !!fileText;

  const setCurDraft = useCallback(
    (v: string) => setDrafts((d) => ({ ...d, [selFile]: v })),
    [selFile],
  );

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
        const [d, fl] = await Promise.all([getSkill(token, name), listSkillFiles(token, name)]);
        setDetail(d);
        setFiles(fl);
        setSelFile("SKILL.md");
        setFileBody(d.content);
        setFileText(true);
        setDrafts({});
        setHistory(null);
        setLintErr(null);
        setTab("view");
        setPane({ kind: "skill", name });
      } catch (e) {
        setError(errMsg(e));
      }
    },
    [token, pane, guardDirty],
  );

  const selectFile = useCallback(
    async (path: string) => {
      if (path === selFile || pane.kind !== "skill") return;
      setLintErr(null);
      if (path === "SKILL.md" && detail) {
        setSelFile(path);
        setFileBody(detail.content);
        setFileText(true);
        setTab("view");
        return;
      }
      try {
        const f = await getSkillFile(token, pane.name, path);
        setSelFile(path);
        setFileBody(f.content);
        setFileText(f.text);
        setTab("view");
      } catch (e) {
        setError(errMsg(e));
      }
    },
    [token, pane, selFile, detail],
  );

  const loadHistory = useCallback(async () => {
    if (pane.kind !== "skill") return;
    try {
      setHistory(await getSkillHistory(token, pane.name));
    } catch (e) {
      setError(errMsg(e));
    }
  }, [token, pane]);

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
    if (pane.kind !== "skill") return;
    setBusy(true);
    setError(null);
    setLintErr(null);
    try {
      const res = await saveSkillFile(token, pane.name, selFile, curDraft);
      if (res.error === "syntax") {
        setLintErr(t("skills.lintError", { lang: res.lang, line: res.line, detail: res.detail }));
        return;
      }
      if (res.error) {
        setError(res.error);
        return;
      }
      setFileBody(curDraft);
      setDrafts((d) => {
        const n = { ...d };
        delete n[selFile];
        return n;
      });
      if (selFile === "SKILL.md") setDetail((dd) => (dd ? { ...dd, content: curDraft } : dd));
      await refresh();
    } catch (e) {
      setError(errMsg(e));
    } finally {
      setBusy(false);
    }
  }, [token, pane, selFile, curDraft, refresh, t]);

  const toggleMode = useCallback(async () => {
    if (!detail) return;
    const next = detail.mode === "auto" ? "manual" : "auto";
    setBusy(true);
    setError(null);
    try {
      await setSkillMode(token, detail.name, next);
      const d = await getSkill(token, detail.name);
      setDetail(d);
      setSelFile("SKILL.md");
      setFileBody(d.content);
      setFileText(true);
      setDrafts((dr) => {
        const n = { ...dr };
        delete n["SKILL.md"];
        return n;
      });
      setTab("view");
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
                        <ObservationsBadge count={row.open_observations} />
                        {row.review ? <ReviewedChip by={row.review.by} /> : <VerdictBadge verdict={row.verdict} />}
                        <ModeBadge mode={row.mode} />
                      </span>
                    </span>
                    <span className="truncate text-[12px] text-muted-foreground">
                      {row.source}
                      {row.provenance?.source ? ` · ${row.provenance.source}` : ""}
                    </span>
                    <UsageLine useCount={row.use_count} lastUsedMs={row.last_used_ms} />
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

                {/* Primary: search the registry, or preview a clicked result. */}
                {previewHit ? (
                  <SkillPreview
                    hit={previewHit}
                    detail={descCache[previewHit.ref]}
                    importing={importing}
                    onImport={() => void doImport(previewHit.ref, previewHit.installed === true)}
                    onBack={() => setPreviewHit(null)}
                  />
                ) : (
                  <>
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
                          {sortedHits.map((h) => (
                            <div
                              key={h.ref}
                              className="rounded-[8px] border border-border/40 bg-muted/20 p-2"
                            >
                              <div className="flex items-start gap-2">
                                <div className="flex min-w-0 flex-1 flex-col">
                                  <span className="flex items-center gap-1.5">
                                    <button
                                      type="button"
                                      data-testid="hit-name"
                                      onClick={() => void openPreview(h)}
                                      className="truncate text-left text-[13px] font-medium text-foreground hover:underline"
                                    >
                                      {h.name}
                                    </button>
                                    <RegistryTag registry={h.registry} />
                                    {h.installed ? (
                                      <span className="shrink-0 rounded-full border border-border/40 bg-muted/40 px-1.5 py-0.5 text-[10px] text-muted-foreground">
                                        {t("skills.search.installed")}
                                      </span>
                                    ) : null}
                                    {typeof h.signals?.installs === "number" ? (
                                      <span className="shrink-0 text-[11px] text-muted-foreground">
                                        {t("skills.search.installs", { count: h.signals.installs })}
                                      </span>
                                    ) : null}
                                  </span>
                                  {h.description ? (
                                    <span className="mt-0.5 truncate text-[12px] text-muted-foreground">
                                      {h.description}
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
                                  onClick={() => void doImport(h.ref, h.installed === true)}
                                >
                                  {importingRef === h.ref ? (
                                    <Loader2 className="h-3.5 w-3.5 animate-spin" />
                                  ) : h.installed ? (
                                    t("skills.import.reinstall")
                                  ) : (
                                    t("skills.import.button")
                                  )}
                                </Button>
                              </div>
                            </div>
                          ))}
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
                  </>
                )}

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
                        {reinstallSrc ? (
                          <Button
                            type="button"
                            size="sm"
                            variant="outline"
                            disabled={importing}
                            onClick={() => void doImport(reinstallSrc, true)}
                          >
                            {importing ? (
                              <Loader2 className="h-3.5 w-3.5 animate-spin" />
                            ) : (
                              t("skills.import.reinstall")
                            )}
                          </Button>
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

                  {triageRow.requirements ? (
                    <div className="mt-4">
                      <p className="mb-1.5 text-[11px] font-semibold uppercase tracking-wide text-muted-foreground">
                        {t("skills.requirements.title")}
                      </p>
                      <TriageRequirements
                        requirements={triageRow.requirements}
                        skillName={triageRow.name}
                        token={token}
                        onResolved={refresh}
                        onAskDurin={onAskDurin}
                      />
                    </div>
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
                      {(triageRow.validation_errors?.length ?? 0) > 0 ? (
                        <Button
                          size="sm"
                          disabled={acting === triageRow.name}
                          onClick={() => void runRepair(triageRow.name, false)}
                        >
                          {t("skills.repair.button")}
                        </Button>
                      ) : null}
                      <Button
                        size="sm"
                        variant={(triageRow.validation_errors?.length ?? 0) > 0 ? "outline" : "default"}
                        disabled={acting === triageRow.name || (triageRow.validation_errors?.length ?? 0) > 0}
                        title={(triageRow.validation_errors?.length ?? 0) > 0 ? t("skills.repair.approveGated") : undefined}
                        onClick={() => triageApprove(triageRow.name)}
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

                  {repairPreview && repairPreview.name === triageRow.name ? (
                    <div className="mt-4 rounded-md border border-border/50 p-3" data-testid="repair-preview">
                      <p className="text-[12px] font-semibold">{t("skills.repair.previewTitle")}</p>
                      <ul className="mt-1 list-disc pl-4 text-[12px] text-muted-foreground">
                        {repairPreview.changes.map((c) => (
                          <li key={c}>{c}</li>
                        ))}
                      </ul>
                      {repairPreview.errors_after.length > 0 ? (
                        <p className="mt-1 text-[12px] text-destructive">
                          {t("skills.repair.stillInvalid", { errors: repairPreview.errors_after.join("; ") })}
                        </p>
                      ) : null}
                      <pre className="mt-2 max-h-64 overflow-auto rounded bg-muted p-2 text-[11px] leading-tight">
                        {repairPreview.diff}
                      </pre>
                      <div className="mt-2 flex gap-2">
                        <Button
                          size="sm"
                          disabled={acting === triageRow.name || !repairPreview.repaired || repairPreview.errors_after.length > 0}
                          onClick={() => void runRepair(triageRow.name, true)}
                        >
                          {t("skills.repair.apply")}
                        </Button>
                        <Button size="sm" variant="outline" onClick={() => setRepairPreview(null)}>
                          {t("skills.repair.cancel")}
                        </Button>
                      </div>
                    </div>
                  ) : null}
                </div>
              )
            ) : !detail ? (
              <div className="flex flex-1 items-center justify-center text-[13px] text-muted-foreground">
                {t("skills.selectPrompt")}
              </div>
            ) : (
              <div className="flex min-h-0 flex-1">
                {files.length > 1 ? (
                  <div className="w-44 shrink-0 overflow-y-auto border-r border-border/30">
                    <SkillFileTree files={files} selected={selFile} onSelect={(p) => void selectFile(p)} />
                  </div>
                ) : null}
                <div className="flex min-h-0 min-w-0 flex-1 flex-col">
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
                  {selFile !== "SKILL.md" ? (
                    <span className="font-mono text-[12px] text-muted-foreground">{selFile}</span>
                  ) : null}
                  <ModeBadge mode={detail.mode} />
                  {skillRow?.review ? <ReviewedChip by={skillRow.review.by} /> : <VerdictBadge verdict={skillRow?.verdict} />}
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
                        title={editable ? undefined : t("skills.binaryFile")}
                        className={cn(
                          "rounded-[6px] px-2.5 py-1 text-[12px] transition-colors disabled:opacity-40",
                          tab === "edit"
                            ? "bg-primary/10 text-primary"
                            : "text-muted-foreground hover:text-foreground",
                        )}
                      >
                        {t("settings.actions.edit")}
                      </button>
                      <button
                        type="button"
                        onClick={() => {
                          setTab("history");
                          void loadHistory();
                        }}
                        className={cn(
                          "rounded-[6px] px-2.5 py-1 text-[12px] transition-colors",
                          tab === "history"
                            ? "bg-primary/10 text-primary"
                            : "text-muted-foreground hover:text-foreground",
                        )}
                      >
                        {t("skills.history.tab")}
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
                    {skillRow?.removable ? (
                      removeConfirm ? (
                        <div className="inline-flex items-center gap-2">
                          <span className="text-[12px] text-muted-foreground">
                            {skillRow.removable === "revert"
                              ? t("skills.revertConfirm")
                              : t("skills.removeConfirm")}
                          </span>
                          <Button
                            variant="destructive"
                            size="sm"
                            disabled={busy}
                            onClick={() => void doRemove()}
                          >
                            {t("skills.confirmRemove")}
                          </Button>
                          <Button
                            variant="outline"
                            size="sm"
                            disabled={busy}
                            onClick={() => setRemoveConfirm(false)}
                          >
                            {t("skills.cancel")}
                          </Button>
                        </div>
                      ) : (
                        <Button
                          variant="outline"
                          size="sm"
                          disabled={busy}
                          onClick={() => setRemoveConfirm(true)}
                        >
                          {skillRow.removable === "revert"
                            ? t("skills.revert")
                            : t("skills.remove")}
                        </Button>
                      )
                    ) : null}
                  </div>
                </div>

                {skillRow && skillRow.verdict !== "safe" && skillRow.findings && skillRow.findings.length > 0 ? (
                  skillRow.review ? (
                    // Reviewed: the findings were acknowledged — collapse them
                    // behind a toggle and drop the warning tint.
                    <div className="shrink-0 border-b border-border/30 bg-emerald-500/5 px-4 py-3 sm:px-6">
                      <p className="mb-1.5 text-[11px] font-semibold uppercase tracking-wide text-muted-foreground">
                        {t("skills.security")}
                      </p>
                      <div className="flex flex-wrap items-center gap-2">
                        <span className="text-[12px] text-muted-foreground">
                          {t("skills.review.reviewedAt", {
                            by:
                              skillRow.review.by === "llm"
                                ? t("skills.review.byLlm")
                                : t("skills.review.byUser"),
                            at: skillRow.review.at,
                          })}
                          {skillRow.review.note ? ` — ${skillRow.review.note}` : ""}
                        </span>
                        <Button variant="outline" size="sm" disabled={busy} onClick={() => void doReopen()}>
                          {t("skills.review.reopen")}
                        </Button>
                      </div>
                      <details className="mt-2">
                        <summary className="cursor-pointer select-none text-[12px] text-muted-foreground hover:text-foreground">
                          {t("skills.review.showFindings", { count: skillRow.findings.length })}
                        </summary>
                        <div className="mt-2">
                          <FindingsList findings={skillRow.findings} />
                        </div>
                      </details>
                    </div>
                  ) : (
                  <div className="shrink-0 border-b border-border/30 bg-amber-500/5 px-4 py-3 sm:px-6">
                    <p className="mb-1.5 text-[11px] font-semibold uppercase tracking-wide text-muted-foreground">
                      {t("skills.security")}
                    </p>
                    <FindingsList findings={skillRow.findings} />

                    {reviewConfirm ? (
                      <div className="mt-3 flex flex-col gap-2 rounded-[8px] border border-border/60 bg-muted/40 p-3">
                        <p className="text-[12px] text-foreground">
                          {skillRow.verdict === "dangerous"
                            ? t("skills.review.confirmDangerous")
                            : t("skills.review.confirmSafe")}
                        </p>
                        <input
                          type="text"
                          value={reviewNote}
                          onChange={(e) => setReviewNote(e.target.value)}
                          placeholder={t("skills.review.notePlaceholder")}
                          className="rounded-[6px] border border-border/60 bg-background px-2 py-1 text-[12px]"
                        />
                        <div className="flex gap-2">
                          <Button
                            variant={skillRow.verdict === "dangerous" ? "destructive" : "default"}
                            size="sm"
                            disabled={busy}
                            onClick={() => void doReview()}
                          >
                            {t("skills.review.confirmAction")}
                          </Button>
                          <Button variant="outline" size="sm" disabled={busy} onClick={() => setReviewConfirm(false)}>
                            {t("skills.cancel")}
                          </Button>
                        </div>
                      </div>
                    ) : (
                      <div className="mt-3 flex flex-wrap gap-2">
                        <Button
                          variant="outline"
                          size="sm"
                          disabled={busy || acting === detail.name}
                          onClick={() => judgeOne(detail.name)}
                        >
                          {t("skills.review.auditLlm")}
                        </Button>
                        <Button variant="outline" size="sm" disabled={busy} onClick={() => setReviewConfirm(true)}>
                          {t("skills.review.markReviewed")}
                        </Button>
                      </div>
                    )}
                  </div>
                  )
                ) : null}

                {(skillRow?.open_observations ?? 0) > 0 ? (
                  <ObservationsPanel
                    skill={detail.name}
                    token={token}
                    onResolved={refresh}
                  />
                ) : null}

                {skillRow?.requirements && (
                  <div className="shrink-0 border-b border-border/30 px-4 py-3 sm:px-6">
                    <p className="mb-1.5 text-[11px] font-semibold uppercase tracking-wide text-muted-foreground">
                      {t("skills.requirements.title")}
                    </p>
                    {skillRow.requirements.bins.length === 0 &&
                     skillRow.requirements.env.length === 0 &&
                     skillRow.requirements.platforms.length === 0 ? (
                      <p className="text-[12px] text-muted-foreground">{t("skills.requirements.none")}</p>
                    ) : (
                      <TriageRequirements
                        requirements={skillRow.requirements}
                        skillName={skillRow.name}
                        token={token}
                        onAskDurin={onAskDurin}
                      />
                    )}
                  </div>
                )}

                <div className="min-h-0 flex-1 overflow-y-auto px-4 py-4 sm:px-6">
                  {tab === "history" ? (
                    history ? (
                      <SkillHistory data={history} skillName={pane.name} token={token} />
                    ) : (
                      <div className="flex flex-1 items-center justify-center text-[13px] text-muted-foreground">
                        <Loader2 className="mr-2 size-4 animate-spin" /> {t("skills.history.loading")}
                      </div>
                    )
                  ) : tab === "view" ? (
                    !fileText ? (
                      <p className="text-[13px] text-muted-foreground">{t("skills.binaryFile")}</p>
                    ) : selFile.endsWith(".md") ? (
                      <div className="max-w-[78ch] text-[14px] leading-relaxed">
                        <MarkdownText>{stripFrontmatter(fileBody)}</MarkdownText>
                      </div>
                    ) : (
                      <CodeBlock
                        language={languageForFile(selFile)}
                        code={fileBody}
                      />
                    )
                  ) : (
                    <Textarea
                      value={curDraft}
                      onChange={(e) => setCurDraft(e.target.value)}
                      spellCheck={false}
                      className="h-full min-h-[24rem] w-full resize-none font-mono text-[12px] leading-relaxed"
                    />
                  )}
                </div>

                {tab === "edit" && editable ? (
                  <div className="flex shrink-0 flex-col gap-2 border-t border-border/30 px-4 py-3 sm:px-6">
                    {lintErr ? (
                      <p className="rounded-md bg-destructive/10 px-3 py-2 font-mono text-[12px] text-destructive">
                        {lintErr}
                      </p>
                    ) : null}
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
                  </div>
                ) : null}

                </div>
              </div>
            )}
          </section>
        </div>
      )}
      {showBlockedModal && (
        <ApproveBlockedModal
          skillName={showBlockedModal.name}
          nonInstallableBins={showBlockedModal.bins}
          onApprove={() => {
            const m = showBlockedModal;
            setShowBlockedModal(null);
            void approve(m.name, { install_deps: true });
          }}
          onCancel={() => setShowBlockedModal(null)}
        />
      )}
    </div>
  );
}

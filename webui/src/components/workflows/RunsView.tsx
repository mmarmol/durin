import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { ChevronLeft, CornerLeftUp, HelpCircle, ListTree, Loader2 } from "lucide-react";
import { useTranslation } from "react-i18next";

import { CopyableKey, RunDetail, RunStatusIcon } from "@/components/workflows/RunDetail";
import {
  ApiError,
  getWorkflowRunManifest,
  listAllWorkflowRuns,
  runWorkflow,
  type WorkflowGlobalRun,
  type WorkflowRunResult,
} from "@/lib/api";
import { relativeTime } from "@/lib/format";
import { useClient } from "@/providers/ClientProvider";
import { cn } from "@/lib/utils";

function errMsg(e: unknown): string {
  if (e instanceof ApiError) return e.detail ? `HTTP ${e.status}: ${e.detail}` : `HTTP ${e.status}`;
  return (e as Error).message;
}

const STATUS_FILTERS = ["all", "completed", "needs_input", "exhausted", "aborted", "crashed", "cancelled"] as const;
type StatusFilter = (typeof STATUS_FILTERS)[number];

const selectCls = "h-7 min-w-0 rounded-md border border-border bg-background px-1.5 text-[11px]";

// Every stranded needs_input run that is actually resumable — the ones this tab's
// tray (and the Workflows sidebar badge) surfaces so a user can resume them without
// hunting down which workflow/session they belong to. A needs_input run written
// before the resume feature shipped has no needs_input_node and can't be resumed;
// it still shows up in the ordinary feed, just not here.
export function strandedRuns(runs: WorkflowGlobalRun[]): WorkflowGlobalRun[] {
  return runs.filter((r) => r.status === "needs_input" && !!r.needs_input_node);
}

export type RunTree = { entry: WorkflowGlobalRun; children: RunTree[] };

// Nests every run under its parent (parent_run_id) so a pipeline and the sub-runs
// it spawned read as one unit instead of interleaved siblings. A run whose parent
// is not itself in the list (outside the fetched window, or removed by the active
// filters) stays at the top level — its row still carries a "sub of" marker. Top
// level preserves the feed's newest-first order; children sort oldest-first so a
// pipeline's sub-runs read in execution order. Defensive: a malformed parent cycle
// is broken by never visiting a run twice.
export function groupRuns(feed: WorkflowGlobalRun[]): RunTree[] {
  const ids = new Set(feed.map((r) => r.run_id));
  const byParent = new Map<string, WorkflowGlobalRun[]>();
  const roots: WorkflowGlobalRun[] = [];
  for (const r of feed) {
    if (r.parent_run_id && r.parent_run_id !== r.run_id && ids.has(r.parent_run_id)) {
      const siblings = byParent.get(r.parent_run_id);
      if (siblings) siblings.push(r);
      else byParent.set(r.parent_run_id, [r]);
    } else {
      roots.push(r);
    }
  }
  const visited = new Set<string>();
  const build = (entry: WorkflowGlobalRun): RunTree => {
    visited.add(entry.run_id);
    const children = (byParent.get(entry.run_id) ?? [])
      .filter((c) => !visited.has(c.run_id))
      .sort((a, b) => (a.started_at ?? 0) - (b.started_at ?? 0))
      .map(build);
    return { entry, children };
  };
  return roots.map(build);
}

// A compact, clickable "waiting for input" entry: selecting it opens the run's
// detail, where the questions and the resume form live.
function TrayRow({ entry, onClick }: { entry: WorkflowGlobalRun; onClick: () => void }) {
  const { t } = useTranslation();
  return (
    <button
      type="button"
      onClick={onClick}
      className="flex w-full flex-col gap-0.5 rounded-md border border-accent bg-accent/40 px-2.5 py-1.5 text-left text-accent-foreground hover:bg-accent/60"
    >
      <span className="flex w-full items-center gap-1.5">
        <HelpCircle className="h-3.5 w-3.5 shrink-0" aria-hidden />
        <span className="min-w-0 flex-1 truncate font-mono text-[12px] font-medium">{entry.workflow}</span>
        {!!entry.started_at && (
          <span className="shrink-0 text-[10px] opacity-70">
            {t("runs.pausedAt", { when: relativeTime(entry.started_at * 1000) })}
          </span>
        )}
      </span>
      {entry.questions && (
        <span className="line-clamp-2 w-full whitespace-pre-wrap pl-5 text-[11px] opacity-80">
          {entry.questions}
        </span>
      )}
    </button>
  );
}

function FeedRow({
  entry,
  subOfUnlisted,
  active,
  onClick,
}: {
  entry: WorkflowGlobalRun;
  // Set when this run has a parent that is not in the rendered list — the nesting
  // can't show the relationship, so the row says it instead.
  subOfUnlisted: boolean;
  active: boolean;
  onClick: () => void;
}) {
  const { t } = useTranslation();
  const startedMs = entry.started_at ? entry.started_at * 1000 : null;
  return (
    <button
      type="button"
      onClick={onClick}
      aria-current={active ? "true" : undefined}
      className={cn(
        "flex w-full flex-col gap-0.5 rounded-md px-2 py-1.5 text-left",
        active ? "bg-muted" : "hover:bg-muted/50",
      )}
    >
      <span className="flex w-full items-center gap-2">
        <RunStatusIcon status={entry.status} />
        <span className="min-w-0 flex-1 truncate font-mono text-[12.5px] font-medium">
          {entry.workflow}
        </span>
        {startedMs != null && (
          <span
            className="shrink-0 text-[10px] tabular-nums text-muted-foreground"
            title={new Date(startedMs).toLocaleString()}
          >
            {relativeTime(startedMs)}
          </span>
        )}
      </span>
      <span className="sr-only">{t("workflows.runStatus." + entry.status, entry.status)}</span>
      {subOfUnlisted && (
        <span className="w-full truncate pl-[22px] text-[10px] text-muted-foreground/70">
          {t("runs.subOf", { id: entry.parent_run_id })}
        </span>
      )}
      {entry.task && (
        <span className="w-full truncate pl-[22px] text-[11px] text-muted-foreground">{entry.task}</span>
      )}
    </button>
  );
}

// One tree node in the list: the run's row plus its nested sub-runs behind a left
// rail, recursively. The rail sits under the status-icon column so children read
// as branches of the parent, the same idiom the work panel uses for branches. A
// nested row never shows the "sub of" caption — its rail already says so; only a
// top-level row whose parent is outside the rendered list needs the words.
function FeedTree({
  node,
  nested = false,
  selectedId,
  onSelect,
}: {
  node: RunTree;
  nested?: boolean;
  selectedId: string | null;
  onSelect: (entry: WorkflowGlobalRun) => void;
}) {
  return (
    <div className="flex flex-col">
      <FeedRow
        entry={node.entry}
        subOfUnlisted={!nested && !!node.entry.parent_run_id}
        active={selectedId === node.entry.run_id}
        onClick={() => onSelect(node.entry)}
      />
      {node.children.length > 0 && (
        <div className="ml-[15px] flex flex-col border-l border-border/70 pl-1">
          {node.children.map((child) => (
            <FeedTree
              key={child.entry.run_id}
              node={child}
              nested
              selectedId={selectedId}
              onSelect={onSelect}
            />
          ))}
        </div>
      )}
    </div>
  );
}

// The selected run's status word as a soft-filled chip for the detail header.
function statusChipTone(status: string): string {
  if (status === "running") return "bg-amber-500/10 text-amber-700 dark:text-amber-400";
  if (status === "completed") return "bg-emerald-500/10 text-emerald-700 dark:text-emerald-400";
  if (status === "needs_input") return "bg-accent text-accent-foreground";
  if (status === "exhausted") return "bg-warn/10 text-warn";
  if (status === "aborted" || status === "crashed") return "bg-destructive/10 text-destructive";
  return "bg-muted text-muted-foreground";
}

// The run's task text, clamped: run inputs are frequently multi-paragraph blobs
// (a pasted ticket, a JSON payload) that would push the actual trace below the
// fold. Click toggles the full text.
function TaskText({ text }: { text: string }) {
  const [expanded, setExpanded] = useState(false);
  return (
    <button
      type="button"
      onClick={() => setExpanded((e) => !e)}
      aria-expanded={expanded}
      className={cn(
        "max-w-[80ch] whitespace-pre-wrap break-words text-left text-[12px] text-muted-foreground",
        !expanded && "line-clamp-2",
      )}
    >
      {text}
    </button>
  );
}

export function RunsView() {
  const { token } = useClient();
  const { t } = useTranslation();
  const [runs, setRuns] = useState<WorkflowGlobalRun[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [statusFilter, setStatusFilter] = useState<StatusFilter>("all");
  const [workflowFilter, setWorkflowFilter] = useState<string>("all");
  const [selected, setSelected] = useState<WorkflowGlobalRun | null>(null);
  const [manifest, setManifest] = useState<WorkflowRunResult | null>(null);
  const [manifestLoading, setManifestLoading] = useState(false);
  const [resumingId, setResumingId] = useState<string | null>(null);

  // Latest selection/manifest, read from inside the poll interval below without
  // making that interval's own effect depend on either — both change far more
  // often (every row click, every manifest refresh) than the interval itself
  // should be torn down and rebuilt.
  const selectedRef = useRef(selected);
  selectedRef.current = selected;
  const manifestRef = useRef(manifest);
  manifestRef.current = manifest;

  const refresh = useCallback(async () => {
    setError(null);
    try {
      const got = await listAllWorkflowRuns(token);
      setRuns(got);
    } catch (e) {
      setError(errMsg(e));
    } finally {
      setLoading(false);
    }
  }, [token]);

  // Re-fetches the currently-open run's manifest in place so its node rows,
  // durations and artifacts advance while the user is watching — but only while
  // that specific run is still "running". Skips entirely when no detail is open
  // or the last fetch already came back terminal, so an open detail on a
  // finished run settles into making no further requests.
  const refreshOpenManifest = useCallback(async () => {
    const entry = selectedRef.current;
    if (!entry || manifestRef.current?.status !== "running") return;
    try {
      const got = await getWorkflowRunManifest(token, entry.workflow, entry.run_id);
      // The selection can change while this request is in flight (the user
      // clicks a different run before this poll tick's fetch resolves). Apply
      // the response only if the fetched run is still the one selected —
      // otherwise it's a stale reply that would overwrite a newer selection's
      // data.
      if (selectedRef.current?.run_id !== entry.run_id) return;
      setManifest(got);
    } catch (e) {
      setError(errMsg(e));
    }
  }, [token]);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  const anyRunning = useMemo(() => runs.some((r) => r.status === "running"), [runs]);

  // While any listed run is still "running", poll the feed every 4 seconds — the
  // same cadence useWorkState already uses, so the side panel and this screen never
  // disagree about how fresh they are. The interval is torn down the moment a poll
  // finds nothing running, so a screen left open on an all-finished feed doesn't
  // keep polling forever. The same tick also refreshes an open run's manifest
  // (see refreshOpenManifest) so an expanded detail advances in place instead of
  // freezing at whatever it showed when the row was clicked.
  useEffect(() => {
    if (!anyRunning) return;
    const id = setInterval(() => {
      void refresh();
      void refreshOpenManifest();
    }, 4000);
    return () => clearInterval(id);
  }, [anyRunning, refresh, refreshOpenManifest]);

  const tray = useMemo(() => strandedRuns(runs), [runs]);

  const workflowNames = useMemo(
    () => Array.from(new Set(runs.map((r) => r.workflow))).sort(),
    [runs],
  );

  const feed = useMemo(() => {
    return runs.filter((r) => {
      if (statusFilter !== "all" && r.status !== statusFilter) return false;
      if (workflowFilter !== "all" && r.workflow !== workflowFilter) return false;
      return true;
    });
  }, [runs, statusFilter, workflowFilter]);

  const tree = useMemo(() => groupRuns(feed), [feed]);

  // The selected run's lineage, resolved against the UNFILTERED feed: its parent
  // entry (for the breadcrumb) and its sub-runs in execution order (for the
  // detail's navigable section) must not vanish just because a filter hides them
  // from the list.
  const parentEntry = useMemo(
    () =>
      selected?.parent_run_id != null
        ? (runs.find((r) => r.run_id === selected.parent_run_id) ?? null)
        : null,
    [runs, selected],
  );
  const childEntries = useMemo(
    () =>
      selected == null
        ? []
        : runs
            .filter((r) => r.parent_run_id === selected.run_id)
            .sort((a, b) => (a.started_at ?? 0) - (b.started_at ?? 0)),
    [runs, selected],
  );

  const onResume = useCallback(
    async (entry: WorkflowGlobalRun, answers: string) => {
      if (!answers.trim()) return;
      setResumingId(entry.run_id);
      setError(null);
      try {
        await runWorkflow(token, entry.workflow, answers, [], "", "", entry.run_id);
        await refresh();
        setSelected(null);
        setManifest(null);
      } catch (e) {
        setError(errMsg(e));
      } finally {
        setResumingId(null);
      }
    },
    [token, refresh],
  );

  const onSelectEntry = useCallback(
    async (entry: WorkflowGlobalRun) => {
      setSelected(entry);
      setManifest(null);
      setManifestLoading(true);
      setError(null);
      try {
        const got = await getWorkflowRunManifest(token, entry.workflow, entry.run_id);
        // A later click on a different row can resolve before this one. Apply
        // the result only while it's still for the selected run — otherwise
        // it's a stale reply for a run the user has since left, and applying
        // it would clobber that other run's already-loaded (or still-loading)
        // detail.
        if (selectedRef.current?.run_id === entry.run_id) setManifest(got);
      } catch (e) {
        setError(errMsg(e));
      } finally {
        if (selectedRef.current?.run_id === entry.run_id) setManifestLoading(false);
      }
    },
    [token],
  );

  // Row click: open the run's detail, or close it when it's already the open one
  // (toggling back to the empty pane / the list on small screens).
  const onRowClick = useCallback(
    (entry: WorkflowGlobalRun) => {
      if (selectedRef.current?.run_id === entry.run_id) {
        setSelected(null);
        setManifest(null);
        return;
      }
      void onSelectEntry(entry);
    },
    [onSelectEntry],
  );

  const onResumeFromDetail = useCallback(
    (answers: string) => {
      if (!selected) return;
      void onResume(selected, answers);
    },
    [selected, onResume],
  );

  const startedMs = selected?.started_at ? selected.started_at * 1000 : null;

  return (
    <div className="flex h-full w-full overflow-hidden">
      {/* List pane: filters + waiting tray + the grouped feed. On small screens it
          IS the view until a run is selected; from lg it is a fixed-width column
          beside the always-present detail pane. */}
      <div
        className={cn(
          "min-h-0 w-full flex-col border-border lg:flex lg:w-80 lg:shrink-0 lg:border-r xl:w-96",
          selected ? "hidden" : "flex",
        )}
      >
        <div className="flex flex-wrap items-center gap-1.5 border-b px-3 py-2">
          <span className="text-[11px] text-muted-foreground">
            {t("runs.countLabel", { count: feed.length })}
          </span>
          <div className="ml-auto flex min-w-0 items-center gap-1.5">
            <select
              value={statusFilter}
              onChange={(e) => setStatusFilter(e.target.value as StatusFilter)}
              className={selectCls}
              aria-label={t("runs.statusFilterAria")}
            >
              {STATUS_FILTERS.map((s) => (
                <option key={s} value={s}>
                  {s === "all" ? t("runs.statusAll") : t("workflows.runStatus." + s, s)}
                </option>
              ))}
            </select>
            <select
              value={workflowFilter}
              onChange={(e) => setWorkflowFilter(e.target.value)}
              className={selectCls}
              aria-label={t("runs.workflowFilterAria")}
            >
              <option value="all">{t("runs.allWorkflows")}</option>
              {workflowNames.map((name) => (
                <option key={name} value={name}>
                  {name}
                </option>
              ))}
            </select>
          </div>
        </div>
        <div className="min-h-0 flex-1 overflow-y-auto p-2">
          {error && (
            <div className="mb-2 rounded-md border border-destructive/40 bg-destructive/10 px-3 py-2 text-xs text-destructive">
              {error}
            </div>
          )}
          {loading ? (
            <div className="flex items-center gap-2 px-1 py-2 text-sm text-muted-foreground">
              <Loader2 className="h-4 w-4 animate-spin" /> {t("workflows.loading")}
            </div>
          ) : (
            <>
              {tray.length > 0 && (
                <div className="mb-2 flex flex-col gap-1">
                  <div className="flex items-center gap-1.5 px-1">
                    <h2 className="text-[10px] font-semibold uppercase tracking-wide text-muted-foreground">
                      {t("runs.trayTitle")}
                    </h2>
                    <span className="rounded-full bg-accent px-1.5 py-0.5 text-[10px] font-medium leading-none text-accent-foreground">
                      {tray.length}
                    </span>
                  </div>
                  {tray.map((entry) => (
                    <TrayRow key={entry.run_id} entry={entry} onClick={() => onRowClick(entry)} />
                  ))}
                </div>
              )}
              {feed.length === 0 && (
                <p className="px-1 py-1 text-xs text-muted-foreground">{t("runs.empty")}</p>
              )}
              <div className="flex flex-col gap-0.5">
                {tree.map((node) => (
                  <FeedTree
                    key={node.entry.run_id}
                    node={node}
                    selectedId={selected?.run_id ?? null}
                    onSelect={onRowClick}
                  />
                ))}
              </div>
            </>
          )}
        </div>
      </div>

      {/* Detail pane */}
      <div className={cn("min-h-0 flex-1 flex-col", selected ? "flex" : "hidden lg:flex")}>
        {selected == null ? (
          <div className="flex h-full flex-col items-center justify-center gap-3 px-6 text-center">
            <ListTree className="h-8 w-8 text-muted-foreground/50" aria-hidden />
            <div>
              <p className="text-sm font-medium">{t("runs.selectPrompt")}</p>
              <p className="mx-auto mt-1 max-w-xs text-xs text-muted-foreground">
                {t("runs.selectHint")}
              </p>
            </div>
          </div>
        ) : (
          <>
            <div className="flex flex-col gap-1.5 border-b px-4 py-3 lg:px-6">
              <div className="flex items-center gap-2 lg:hidden">
                <button
                  type="button"
                  onClick={() => {
                    setSelected(null);
                    setManifest(null);
                  }}
                  className="inline-flex items-center gap-1 rounded text-[11px] text-muted-foreground hover:text-foreground"
                >
                  <ChevronLeft className="h-3.5 w-3.5" aria-hidden />
                  {t("runs.backToList")}
                </button>
              </div>
              {parentEntry && (
                <button
                  type="button"
                  onClick={() => void onSelectEntry(parentEntry)}
                  className="inline-flex w-fit items-center gap-1 text-[11px] text-muted-foreground hover:text-foreground"
                >
                  <CornerLeftUp className="h-3 w-3" aria-hidden />
                  {t("runs.partOf", { workflow: parentEntry.workflow })}
                </button>
              )}
              <div className="flex flex-wrap items-center gap-2">
                <RunStatusIcon status={selected.status} className="h-4 w-4" />
                <h2 className="min-w-0 truncate font-mono text-sm font-semibold">
                  {selected.workflow}
                </h2>
                <span
                  className={cn(
                    "rounded px-1.5 py-0.5 text-[10px] font-medium",
                    statusChipTone(selected.status),
                  )}
                >
                  {t("workflows.runStatus." + selected.status, selected.status)}
                </span>
                {startedMs != null && (
                  <span
                    className="ml-auto shrink-0 text-[11px] tabular-nums text-muted-foreground"
                    title={new Date(startedMs).toLocaleString()}
                  >
                    {relativeTime(startedMs)}
                  </span>
                )}
              </div>
              <div className="flex min-w-0 items-center gap-2">
                <CopyableKey value={selected.run_id} />
              </div>
              {selected.task && <TaskText text={selected.task} />}
            </div>
            <div className="min-h-0 flex-1 overflow-y-auto px-4 py-3 lg:px-6">
              {manifestLoading ? (
                <div className="flex items-center gap-2 text-sm text-muted-foreground">
                  <Loader2 className="h-4 w-4 animate-spin" /> {t("workflows.loading")}
                </div>
              ) : (
                manifest && (
                  <RunDetail
                    result={manifest}
                    resuming={resumingId === selected.run_id}
                    onResume={onResumeFromDetail}
                    childRuns={childEntries}
                    onOpenRun={(r) => void onSelectEntry(r)}
                  />
                )
              )}
            </div>
          </>
        )}
      </div>
    </div>
  );
}

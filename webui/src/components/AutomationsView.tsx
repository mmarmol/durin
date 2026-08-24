import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Loader2, Plus } from "lucide-react";
import { useTranslation } from "react-i18next";

import { AutomationForm } from "@/components/automations/AutomationForm";
import { DetailView } from "@/components/automations/DetailView";
import { InboxView } from "@/components/automations/InboxView";
import { ListView } from "@/components/automations/ListView";
import { NeedsYouTray } from "@/components/automations/NeedsYouTray";
import { Button } from "@/components/ui/button";
import {
  ApiError,
  listAllAutomationRuns,
  listAutomations,
  listCronJobs,
  type AutomationDef,
  type AutomationRun,
  type AutomationSummary,
  type CronJobRow,
} from "@/lib/api";
import { useClient } from "@/providers/ClientProvider";

function errMsg(e: unknown): string {
  if (e instanceof ApiError) return e.detail ? `HTTP ${e.status}: ${e.detail}` : `HTTP ${e.status}`;
  return (e as Error).message;
}

export function AutomationsView({
  onOpenWorkflowRun,
  initialDetailName,
}: {
  // Drills into the executions screen for a run this automation launched
  // (RunDetailCard's "Ver ejecución completa →"). Optional so the many
  // existing list/editor tests that never reach the detail view don't need
  // to stub it — App.tsx always supplies a real one in production.
  onOpenWorkflowRun?: (workflow: string, runId: string) => void;
  // A deep link from the cron settings screen's "Abrir automatización →"
  // button (C6): the automation to open DetailView on as soon as the list
  // has loaded. Consumed once — see RunsView's initialSelection for the
  // same pattern (and the same "outside the loaded set" no-op behavior).
  initialDetailName?: string | null;
}) {
  const { token } = useClient();
  const { t } = useTranslation();
  const [automations, setAutomations] = useState<AutomationSummary[]>([]);
  const [runs, setRuns] = useState<AutomationRun[]>([]);
  const [cronJobs, setCronJobs] = useState<CronJobRow[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [selectedRunId, setSelectedRunId] = useState<string | null>(null);
  // undefined = editor closed (showing the list); null = creating a new
  // automation; an AutomationDef = editing that one. A row's own
  // AutomationSummary satisfies AutomationDef structurally (it's a superset),
  // so ListView's onEdit can hand it straight in without a getAutomation
  // round-trip.
  const [editing, setEditing] = useState<AutomationDef | null | undefined>(undefined);
  // The automation whose DetailView is open, or null for the list. Checked
  // after `editing` so the two destinations stay mutually exclusive without
  // folding into one combined union.
  const [detail, setDetail] = useState<AutomationSummary | null>(null);

  // Reusable for both the initial mount and a post-save/delete reload from
  // the editor — the editor's own onDone calls this directly, so a save
  // doesn't need a page nav to see its own effect in the list.
  const refresh = useCallback(async () => {
    setError(null);
    try {
      const [defs, runFeed, jobs] = await Promise.all([
        listAutomations(token),
        listAllAutomationRuns(token),
        listCronJobs(token),
      ]);
      setAutomations(defs);
      setRuns(runFeed);
      setCronJobs(jobs);
    } catch (e) {
      setError(errMsg(e));
    } finally {
      setLoading(false);
    }
  }, [token]);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  // A `running` run polls fast for the same reason DetailView's own
  // anyRunning does (an AutomationRun's status only ever changes via a
  // fresh fetch); a `paused` one is included too, since answering it from
  // this screen only returns answer_nowait's immediate `running` snapshot —
  // the actual outcome lands a poll later, and this is the screen where an
  // operator answers from the inbox and then watches it settle.
  const anyRunningOrPausedPending = useMemo(
    () => runs.some((r) => r.status === "running" || r.status === "paused"),
    [runs],
  );

  // Self-refresh — 4s while anyRunningOrPausedPending, else the 30s cadence
  // App.tsx's sidebar badges already poll the same two feeds at, so a user
  // sitting on this section sees runs and life-state changes (a schedule
  // tick, an operator answering elsewhere) without needing to navigate away
  // and back. Skipped entirely while the editor is open
  // (`editing !== undefined`): a background refresh doesn't touch `editing`
  // itself, but re-fetching mid-edit is pure risk for no benefit here, so
  // the interval just doesn't run rather than needing to reconcile with
  // in-progress form state. Re-armed the moment the editor closes.
  useEffect(() => {
    if (editing !== undefined) return;
    const id = setInterval(() => void refresh(), anyRunningOrPausedPending ? 4000 : 30_000);
    return () => clearInterval(id);
  }, [editing, refresh, anyRunningOrPausedPending]);

  // Consume a deep-linked initialDetailName exactly once: find the matching
  // definition in the just-loaded list and open its DetailView, the same way
  // a row click would. Guarded on `loading` so it waits for the mount fetch
  // to land instead of searching an empty array, and on the ref so a later
  // re-render never re-triggers it. A name outside the loaded set is
  // silently not found — same no-op precedent as RunsView's own
  // initialSelection.
  const initialDetailConsumed = useRef(false);
  useEffect(() => {
    if (initialDetailConsumed.current || !initialDetailName || loading) return;
    initialDetailConsumed.current = true;
    const match = automations.find((a) => a.name === initialDetailName);
    if (match) setDetail(match);
  }, [initialDetailName, loading, automations]);

  // Keep `detail` pointed at the live copy of whatever automation its
  // DetailView has open: `automations` and `detail` are separate state, so
  // without this a save made from inside DetailView (pause/resume) — or
  // simply the next 30s poll below — would refresh the list while
  // DetailView's own `automation` prop stayed frozen at whatever snapshot
  // was open when the user clicked in. A name missing from the fresh list
  // (deleted concurrently) leaves `detail` as-is rather than clearing it out
  // from under the user.
  useEffect(() => {
    if (!detail) return;
    const fresh = automations.find((a) => a.name === detail.name);
    if (fresh && fresh !== detail) setDetail(fresh);
  }, [automations, detail]);

  // The run NeedsYouTray's Revisar/Responder selected, if it is still
  // paused — filtered on status (not just id) so InboxView disappears the
  // moment a refresh reports it resolved, with no separate "clear the
  // selection" step required on the happy path (onResolved below still
  // clears it explicitly, for tidy state rather than correctness).
  const selectedRun = useMemo(
    () => runs.find((r) => r.run_id === selectedRunId && r.status === "paused") ?? null,
    [runs, selectedRunId],
  );

  // A transient confirmation line shown once InboxView resolves a run —
  // cleared automatically after a few seconds. Lives here rather than in
  // InboxView itself because resolving is exactly what makes InboxView
  // unmount (selectedRun above goes null on the next refresh), so nothing
  // survives inside that card to show a message once the action succeeds.
  const [resolutionNotice, setResolutionNotice] = useState<string | null>(null);
  const noticeTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  useEffect(() => {
    return () => {
      if (noticeTimerRef.current) clearTimeout(noticeTimerRef.current);
    };
  }, []);

  const onResolved = useCallback(
    (message: string) => {
      setResolutionNotice(message);
      setSelectedRunId(null);
      void refresh();
      if (noticeTimerRef.current) clearTimeout(noticeTimerRef.current);
      noticeTimerRef.current = setTimeout(() => setResolutionNotice(null), 4000);
    },
    [refresh],
  );

  if (editing !== undefined) {
    return (
      <div className="flex h-full w-full flex-col overflow-hidden">
        <div className="flex items-center gap-2 border-b px-3 py-2">
          <button
            type="button"
            onClick={() => setEditing(undefined)}
            className="text-xs text-muted-foreground hover:text-foreground"
          >
            {t("automations.form.back")}
          </button>
          <span className="text-xs font-medium text-foreground/80">
            {editing ? t("automations.form.editTitle", { name: editing.name }) : t("automations.form.newTitle")}
          </span>
        </div>
        <div className="min-h-0 flex-1 overflow-y-auto">
          <AutomationForm
            token={token}
            editAutomation={editing}
            onDone={() => {
              setEditing(undefined);
              void refresh();
            }}
            onCancel={() => setEditing(undefined)}
          />
        </div>
      </div>
    );
  }

  if (detail) {
    return (
      <DetailView
        automation={detail}
        onBack={() => setDetail(null)}
        onOpenWorkflowRun={onOpenWorkflowRun ?? (() => {})}
        onAutomationSaved={refresh}
        feedShowsActivity={runs.some(
          (r) =>
            r.automation === detail.name &&
            (r.status === "running" || r.status === "paused"),
        )}
      />
    );
  }

  return (
    <div className="flex h-full w-full flex-col overflow-hidden">
      <div className="min-h-0 flex-1 overflow-y-auto">
        <div className="mx-auto flex w-full max-w-3xl flex-col gap-3 px-4 py-4">
          <div className="flex items-start justify-between gap-4">
            <div>
              <h1 className="text-[17px] font-semibold">{t("automations.title")}</h1>
              <p className="mt-0.5 text-[12.5px] text-muted-foreground">{t("automations.subtitle")}</p>
            </div>
            <Button size="sm" className="shrink-0 gap-1.5" onClick={() => setEditing(null)}>
              <Plus className="h-3.5 w-3.5" aria-hidden />
              {t("automations.list.new")}
            </Button>
          </div>
          {error && (
            <div className="rounded-md border border-destructive/40 bg-destructive/10 px-3 py-2 text-xs text-destructive">
              {error}
            </div>
          )}
          {loading ? (
            <div className="flex items-center gap-2 text-sm text-muted-foreground">
              <Loader2 className="h-4 w-4 animate-spin" /> {t("automations.list.loading")}
            </div>
          ) : (
            <>
              <NeedsYouTray
                runs={runs}
                selectedRunId={selectedRunId}
                onSelect={(run) => setSelectedRunId(run.run_id)}
              />
              {resolutionNotice && (
                <div className="rounded-md border border-emerald-500/30 bg-emerald-500/10 px-3 py-2 text-[12.5px] text-emerald-700 dark:text-emerald-400">
                  {resolutionNotice}
                </div>
              )}
              {selectedRun && <InboxView key={selectedRun.run_id} run={selectedRun} onResolved={onResolved} />}
              <ListView
                automations={automations}
                runs={runs}
                cronJobs={cronJobs}
                onEdit={setEditing}
                onOpenDetail={setDetail}
              />
            </>
          )}
        </div>
      </div>
    </div>
  );
}

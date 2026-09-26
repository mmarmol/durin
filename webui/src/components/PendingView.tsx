import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Inbox, Loader2, RefreshCw } from "lucide-react";
import { useTranslation } from "react-i18next";

import { InboxView } from "@/components/automations/InboxView";
import { DreamDrawer, type DrawerTarget } from "@/components/DreamDrawer";
import { FlaggedPairCard, flaggedResolveErrorMessage } from "@/components/FlaggedPairCard";
import { QuarantineCard } from "@/components/QuarantineCard";
import { SkillSuggestionCard, suggestionErrorMessage } from "@/components/SkillSuggestionCard";
import { ApprovalCard } from "@/components/thread/ApprovalCard";
import { Button } from "@/components/ui/button";
import { NeedsInputForm } from "@/components/workflows/NeedsInputForm";
import {
  ApiError,
  acceptSkillSuggestion,
  decideApproval,
  listPending,
  rejectSkillSuggestion,
  resolveFlaggedPair,
  runWorkflow,
  type AutomationRun,
  type FlaggedPair,
  type PendingItem,
  type PendingList,
  type QuarantineRow,
  type ResolveFlaggedBody,
  type SkillSuggestion,
  type WorkflowGlobalRun,
} from "@/lib/api";
import { relativeTime } from "@/lib/format";
import type { ApprovalDecision, ApprovalDecisionResult, PendingApproval } from "@/lib/types";
import { cn } from "@/lib/utils";
import { useClient } from "@/providers/ClientProvider";

// The order the sections read in: the order the server lists its sources.
const SOURCE_ORDER = [
  "approval",
  "skill_quarantine",
  "automation_run",
  "workflow_run",
  "flagged_pair",
  "skill_suggestion",
] as const;

// How often an open page re-reads the list, so an item resolved somewhere
// else (in a chat, with the CLI, by a channel reply) leaves it.
const REFRESH_MS = 30_000;

// Decision outcomes that did what was asked. Any other (failed: approved but
// running it failed; stale: expired, or its target changed) is a problem to
// report, even though the request was acted on.
const DECIDED_OK = new Set(["applied", "rejected", "pending"]);

type Notice = { text: string; tone: "ok" | "error" };

function errMsg(e: unknown): string {
  if (e instanceof ApiError) return e.detail ? `HTTP ${e.status}: ${e.detail}` : `HTTP ${e.status}`;
  return e instanceof Error ? e.message : String(e);
}

/**
 * Everything that waits on the person, in one place, grouped by where it
 * comes from: approval requests, skill imports in quarantine, paused
 * automation runs, workflow runs waiting for input, memory pairs the dream
 * flagged, and skill suggestions.
 *
 * Each item renders with the card its own section uses, fed the record the
 * server returns for it, and resolves through the same routes; an approval
 * request decides through the REST decision route. Resolving anything
 * re-reads the list, and every read reports the count up (the sidebar badge).
 * The domain sections keep their own lists.
 */
export function PendingView({
  onCountChange,
  onOpenSkill,
  onOpenWorkflowRun,
}: {
  onCountChange?: (count: number) => void;
  // Opens a quarantined import's triage in Skills (the install gate lives there).
  onOpenSkill?: (name: string) => void;
  // Opens a workflow run's full detail in Workflows.
  onOpenWorkflowRun?: (workflow: string, runId: string) => void;
}) {
  const { token } = useClient();
  const { t } = useTranslation();
  const [list, setList] = useState<PendingList | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<Notice | null>(null);
  const [busy, setBusy] = useState<Set<string>>(new Set());
  const [itemErrors, setItemErrors] = useState<Record<string, string>>({});
  const [drawerTarget, setDrawerTarget] = useState<DrawerTarget | null>(null);
  const noticeTimer = useRef<ReturnType<typeof setTimeout> | null>(null);

  const refresh = useCallback(async () => {
    try {
      const got = await listPending(token);
      setList(got);
      setError(null);
      onCountChange?.(got.count);
    } catch (e) {
      setError(errMsg(e));
    }
  }, [token, onCountChange]);

  useEffect(() => {
    void refresh();
    const id = setInterval(() => void refresh(), REFRESH_MS);
    return () => clearInterval(id);
  }, [refresh]);

  useEffect(
    () => () => {
      if (noticeTimer.current) clearTimeout(noticeTimer.current);
    },
    [],
  );

  // Say what a resolution came to, then re-read the list. The card that was
  // acted on usually leaves with the re-read, so the message lives here. A
  // problem stays until the next message replaces it.
  const resolved = useCallback(
    (text: string, tone: Notice["tone"] = "ok") => {
      setNotice({ text, tone });
      if (noticeTimer.current) clearTimeout(noticeTimer.current);
      if (tone === "ok") noticeTimer.current = setTimeout(() => setNotice(null), 4000);
      void refresh();
    },
    [refresh],
  );

  // Run one resolution for the item under `key`: mark it busy, and show a
  // failure on its card in the words `describe` gives it.
  const track = useCallback(
    async (key: string, work: () => Promise<void>, describe: (e: unknown) => string) => {
      setBusy((prev) => new Set(prev).add(key));
      setItemErrors((prev) => {
        const next = { ...prev };
        delete next[key];
        return next;
      });
      try {
        await work();
      } catch (e) {
        setItemErrors((prev) => ({ ...prev, [key]: describe(e) }));
      } finally {
        setBusy((prev) => {
          const next = new Set(prev);
          next.delete(key);
          return next;
        });
      }
    },
    [],
  );

  const decide = useCallback(
    async (approvalId: string, decision: ApprovalDecision): Promise<ApprovalDecisionResult> => {
      let res: ApprovalDecisionResult;
      try {
        res = await decideApproval(token, approvalId, decision);
      } catch (e) {
        // Refused and left as it was (a 409 says why), or not allowed: the
        // card shows the reason and the request stays.
        void refresh();
        throw new Error(e instanceof ApiError && e.detail ? e.detail : errMsg(e));
      }
      if (!DECIDED_OK.has(res.status)) {
        resolved(res.message, "error");
        throw new Error(res.message);
      }
      const done: Record<string, string> = {
        applied: t("message.approval.applied"),
        rejected: t("message.approval.rejected"),
        pending: t("message.approval.handedOff"),
      };
      resolved(done[res.status] ?? res.message);
      return res;
    },
    [token, refresh, resolved, t],
  );

  const groups = useMemo(
    () =>
      SOURCE_ORDER.map((source) => ({
        source,
        items: (list?.items ?? []).filter((item) => item.source === source),
      })).filter((group) => group.items.length > 0),
    [list],
  );

  function renderItem(item: PendingItem, key: string) {
    switch (item.source) {
      case "approval": {
        const data = item.data as {
          kind?: string;
          summary?: string;
          detail?: Record<string, unknown>;
        };
        const approval: PendingApproval = {
          approval_id: item.id,
          kind: data.kind ?? item.kind,
          summary: data.summary ?? item.title,
          detail: data.detail ?? {},
        };
        return <ApprovalCard approval={approval} onDecide={(d) => decide(item.id, d)} />;
      }
      case "skill_quarantine":
        return (
          <QuarantineCard
            skill={item.data as unknown as QuarantineRow}
            onOpen={setDrawerTarget}
            onOpenSkills={onOpenSkill}
          />
        );
      case "automation_run":
        return <InboxView run={item.data as unknown as AutomationRun} onResolved={(m) => resolved(m)} />;
      case "workflow_run": {
        const run = item.data as unknown as WorkflowGlobalRun;
        return (
          <div className="rounded-lg border border-border">
            <div className="flex items-center gap-2 border-b border-border px-3.5 py-2.5 text-[13px]">
              <span className="min-w-0 truncate font-mono font-medium">{run.workflow}</span>
              {run.started_at ? (
                <span className="shrink-0 text-[11px] text-muted-foreground">
                  {t("runs.pausedAt", { when: relativeTime((run.finished_at ?? run.started_at) * 1000) })}
                </span>
              ) : null}
              {onOpenWorkflowRun ? (
                <Button
                  size="sm"
                  variant="ghost"
                  className="ml-auto h-6 px-1.5 text-[11px] font-normal"
                  onClick={() => onOpenWorkflowRun(run.workflow, run.run_id)}
                >
                  {t("pending.openRun")}
                </Button>
              ) : null}
            </div>
            <div className="px-3.5 py-3">
              <NeedsInputForm
                runId={run.run_id}
                needsInputNode={run.needs_input_node}
                questions={run.questions ?? ""}
                resuming={busy.has(key)}
                onResume={(answers) =>
                  void track(
                    key,
                    async () => {
                      await runWorkflow(token, run.workflow, answers, [], "", "", run.run_id);
                      resolved(t("pending.notice.resumed"));
                    },
                    errMsg,
                  )
                }
              />
            </div>
          </div>
        );
      }
      case "flagged_pair":
        return (
          <FlaggedPairCard
            pair={item.data as unknown as FlaggedPair}
            onOpen={setDrawerTarget}
            resolving={busy.has(key)}
            onResolve={(_pair: FlaggedPair, body: ResolveFlaggedBody) =>
              void track(
                key,
                async () => {
                  await resolveFlaggedPair(token, body);
                  resolved(t("pending.notice.resolved"));
                },
                (e) => flaggedResolveErrorMessage(e, t),
              )
            }
          />
        );
      case "skill_suggestion":
        return (
          <SkillSuggestionCard
            suggestion={item.data as unknown as SkillSuggestion}
            busy={busy.has(key)}
            onResolve={(action) =>
              void track(
                key,
                async () => {
                  if (action === "accept") await acceptSkillSuggestion(token, item.id);
                  else await rejectSkillSuggestion(token, item.id);
                  resolved(t("pending.notice.resolved"));
                },
                (e) => suggestionErrorMessage(e, t),
              )
            }
          />
        );
      default:
        return null;
    }
  }

  return (
    // position:relative scopes the drawer's absolute positioning to this view.
    <div className="relative flex h-full min-h-0 flex-col overflow-hidden bg-background">
      <header className="flex shrink-0 items-center gap-2 border-b border-border/40 px-3 py-2">
        <Inbox className="h-4 w-4 text-muted-foreground" aria-hidden />
        <h1 className="text-sm font-semibold">{t("pending.title")}</h1>
        {list ? <span className="text-xs text-muted-foreground">{list.count}</span> : null}
        <Button
          type="button"
          size="sm"
          variant="ghost"
          className="ml-auto h-7 w-7 p-0"
          aria-label={t("pending.refresh")}
          title={t("pending.refresh")}
          onClick={() => void refresh()}
        >
          <RefreshCw className="h-3.5 w-3.5" aria-hidden />
        </Button>
      </header>
      <div className="min-h-0 flex-1 overflow-y-auto">
        <div className="mx-auto flex w-full max-w-3xl flex-col gap-5 px-4 py-4">
          <p className="text-[12.5px] text-muted-foreground">{t("pending.subtitle")}</p>
          {notice ? (
            <div
              role="status"
              className={cn(
                "rounded-md border px-3 py-2 text-[12.5px]",
                notice.tone === "ok"
                  ? "border-emerald-500/30 bg-emerald-500/10 text-emerald-700 dark:text-emerald-400"
                  : "border-destructive/40 bg-destructive/10 text-destructive",
              )}
            >
              {notice.text}
            </div>
          ) : null}
          {error ? (
            <div className="rounded-md border border-destructive/40 bg-destructive/10 px-3 py-2 text-xs text-destructive">
              {error}
            </div>
          ) : null}
          {(list?.errors ?? []).map((e) => (
            <p key={e.source} className="text-[12px] text-destructive">
              {t("pending.sourceError", {
                source: t(`pending.source.${e.source}`, { defaultValue: e.source }),
                detail: e.detail,
              })}
            </p>
          ))}
          {list === null ? (
            error ? null : (
              <div className="flex items-center gap-2 text-sm text-muted-foreground">
                <Loader2 className="h-4 w-4 animate-spin" aria-hidden /> {t("pending.loading")}
              </div>
            )
          ) : groups.length === 0 ? (
            <p className="text-sm text-muted-foreground">{t("pending.empty")}</p>
          ) : (
            groups.map(({ source, items }) => {
              const label = t(`pending.source.${source}`);
              return (
                <section key={source} aria-label={label} className="flex flex-col gap-2">
                  <h2 className="text-[11px] font-semibold uppercase tracking-wide text-muted-foreground">
                    {label} · {items.length}
                  </h2>
                  {items.map((item) => {
                    const key = `${item.source}:${item.id}`;
                    return (
                      <div key={key} className="flex flex-col gap-1">
                        {renderItem(item, key)}
                        {itemErrors[key] ? (
                          <p role="alert" className="text-[12px] text-destructive">
                            {itemErrors[key]}
                          </p>
                        ) : null}
                      </div>
                    );
                  })}
                </section>
              );
            })
          )}
        </div>
      </div>
      <DreamDrawer target={drawerTarget} onClose={() => setDrawerTarget(null)} />
    </div>
  );
}

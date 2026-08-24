import { useCallback, useEffect, useState } from "react";
import { Loader2, Plus } from "lucide-react";
import { useTranslation } from "react-i18next";

import { AutomationForm } from "@/components/automations/AutomationForm";
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

export function AutomationsView() {
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
              <ListView automations={automations} runs={runs} cronJobs={cronJobs} onEdit={setEditing} />
            </>
          )}
        </div>
      </div>
    </div>
  );
}

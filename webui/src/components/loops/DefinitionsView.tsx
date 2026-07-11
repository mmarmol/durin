import { useCallback, useEffect, useState } from "react";
import { Loader2, Pencil, Play, Plus, Trash2 } from "lucide-react";
import { useTranslation } from "react-i18next";

import { Button } from "@/components/ui/button";
import { DeleteConfirm } from "@/components/DeleteConfirm";
import { ApiError, deleteLoop, fireLoop, listLoops, type LoopDef, type LoopSummary } from "@/lib/api";
import { useClient } from "@/providers/ClientProvider";
import { cn } from "@/lib/utils";

function errMsg(e: unknown): string {
  if (e instanceof ApiError) return e.detail ? `HTTP ${e.status}: ${e.detail}` : `HTTP ${e.status}`;
  return (e as Error).message;
}

export function DefinitionsView({
  onEdit,
}: {
  onEdit: (def: LoopDef | null) => void;
}) {
  const { token } = useClient();
  const { t } = useTranslation();
  const [loops, setLoops] = useState<LoopSummary[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [pendingDelete, setPendingDelete] = useState<LoopSummary | null>(null);
  const [runningLoop, setRunningLoop] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    setError(null);
    try {
      const got = await listLoops(token);
      setLoops(got);
    } catch (e) {
      setError(errMsg(e));
    } finally {
      setLoading(false);
    }
  }, [token]);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  const onRunNow = useCallback(
    async (name: string) => {
      setError(null);
      setRunningLoop(name);
      try {
        await fireLoop(token, name);
        await refresh();
      } catch (e) {
        setError(errMsg(e));
      } finally {
        setRunningLoop(null);
      }
    },
    [token, refresh],
  );

  const onConfirmDelete = useCallback(async () => {
    if (!pendingDelete) return;
    const name = pendingDelete.name;
    setPendingDelete(null);
    try {
      await deleteLoop(token, name);
      await refresh();
    } catch (e) {
      setError(errMsg(e));
    }
  }, [pendingDelete, token, refresh]);

  return (
    <div className="flex h-full w-full flex-col overflow-hidden">
      <div className="flex min-h-0 flex-1 overflow-y-auto">
        <div className="mx-auto flex w-full max-w-3xl flex-col gap-3 px-4 py-4">
          <div className="flex items-center justify-between">
            <span className="text-xs text-muted-foreground">
              {t("loops.definitions.countLabel", { count: loops.length })}
            </span>
            <Button size="sm" onClick={() => onEdit(null)} className="gap-1.5">
              <Plus className="h-3.5 w-3.5" aria-hidden />
              {t("loops.definitions.new")}
            </Button>
          </div>
          {error && (
            <div className="rounded-md border border-destructive/40 bg-destructive/10 px-3 py-2 text-xs text-destructive">
              {error}
            </div>
          )}
          {loading ? (
            <div className="flex items-center gap-2 text-sm text-muted-foreground">
              <Loader2 className="h-4 w-4 animate-spin" /> {t("loops.definitions.loading")}
            </div>
          ) : loops.length === 0 ? (
            <p className="text-xs text-muted-foreground">{t("loops.definitions.empty")}</p>
          ) : (
            <table className="w-full text-left text-xs">
              <thead>
                <tr className="text-[10px] uppercase tracking-wide text-muted-foreground">
                  <th className="px-2 py-1.5 font-medium">{t("loops.definitions.columns.name")}</th>
                  <th className="px-2 py-1.5 font-medium">{t("loops.definitions.columns.workflow")}</th>
                  <th className="px-2 py-1.5 font-medium">{t("loops.definitions.columns.triggers")}</th>
                  <th className="px-2 py-1.5 font-medium">{t("loops.definitions.columns.active")}</th>
                  <th className="px-2 py-1.5 font-medium">{t("loops.definitions.columns.needsOperator")}</th>
                  <th className="px-2 py-1.5" />
                </tr>
              </thead>
              <tbody>
                {loops.map((def) => (
                  <tr key={def.name} className="border-t border-border">
                    <td className="px-2 py-2 font-mono">
                      <div className="flex items-center gap-1.5">
                        {def.name}
                        <span
                          className={cn(
                            "rounded-full px-1.5 py-0.5 text-[10px]",
                            def.enabled
                              ? "bg-accent text-accent-foreground"
                              : "text-muted-foreground",
                          )}
                        >
                          {def.enabled
                            ? t("loops.definitions.enabled")
                            : t("loops.definitions.disabled")}
                        </span>
                      </div>
                    </td>
                    <td className="px-2 py-2 text-muted-foreground">{def.workflow}</td>
                    <td className="px-2 py-2 text-muted-foreground">{def.triggers.length}</td>
                    <td className="px-2 py-2 text-muted-foreground">{def.active_runs}</td>
                    <td className="px-2 py-2 text-muted-foreground">{def.needs_operator}</td>
                    <td className="px-2 py-2">
                      <div className="flex items-center justify-end gap-1">
                        <Button
                          size="sm"
                          variant="outline"
                          className="h-7 gap-1 px-2"
                          disabled={!def.enabled || runningLoop === def.name}
                          onClick={() => onRunNow(def.name)}
                        >
                          {runningLoop === def.name ? (
                            <Loader2 className="h-3.5 w-3.5 animate-spin" aria-hidden />
                          ) : (
                            <Play className="h-3.5 w-3.5" aria-hidden />
                          )}
                          {t("loops.definitions.runNow")}
                        </Button>
                        <Button
                          size="sm"
                          variant="ghost"
                          className="h-7 gap-1 px-2"
                          onClick={() => onEdit(def)}
                        >
                          <Pencil className="h-3.5 w-3.5" aria-hidden />
                          {t("loops.definitions.edit")}
                        </Button>
                        <Button
                          size="sm"
                          variant="ghost"
                          className="h-7 gap-1 px-2 text-destructive hover:text-destructive"
                          onClick={() => setPendingDelete(def)}
                        >
                          <Trash2 className="h-3.5 w-3.5" aria-hidden />
                          {t("loops.definitions.delete")}
                        </Button>
                      </div>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>
      </div>
      <DeleteConfirm
        open={!!pendingDelete}
        title={pendingDelete?.name ?? ""}
        titleKey="loops.definitions.deleteTitle"
        onCancel={() => setPendingDelete(null)}
        onConfirm={onConfirmDelete}
      />
    </div>
  );
}

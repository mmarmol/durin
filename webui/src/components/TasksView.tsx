import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { Bot, GitBranch } from "lucide-react";
import { listBackgroundTasks, type BackgroundTask } from "@/lib/api";
import { useClient } from "@/providers/ClientProvider";
import { cn } from "@/lib/utils";

function statusTone(status: BackgroundTask["status"]): string {
  if (status === "failed") return "bg-destructive/10 text-destructive";
  if (status === "done") return "bg-muted text-muted-foreground";
  if (status === "needs_input") return "bg-amber-500/10 text-amber-700 dark:text-amber-400";
  return "bg-emerald-500/10 text-emerald-700 dark:text-emerald-400";
}

export function TasksView({ session }: { session: string | null }) {
  const { token } = useClient();
  const { t } = useTranslation();
  const [tasks, setTasks] = useState<BackgroundTask[]>([]);
  const [showFinished, setShowFinished] = useState(false);

  useEffect(() => {
    if (!session) return;
    let cancelled = false;
    const load = () => {
      listBackgroundTasks(token, session)
        .then((rows) => { if (!cancelled) setTasks(rows); })
        .catch(() => { if (!cancelled) setTasks([]); });
    };
    load();
    const id = setInterval(load, 4000);
    return () => { cancelled = true; clearInterval(id); };
  }, [token, session]);

  const live = tasks.filter((x) => x.status === "running" || x.status === "needs_input");
  const finished = tasks.filter((x) => x.status === "done" || x.status === "failed");

  const Row = (x: BackgroundTask) => (
    <div
      key={`${x.kind}:${x.id}`}
      className="flex w-full items-center gap-2 rounded-md px-2 py-1.5 text-[12.5px]"
    >
      {x.kind === "subagent" ? <Bot className="h-3.5 w-3.5" aria-hidden /> : <GitBranch className="h-3.5 w-3.5" aria-hidden />}
      <span className="truncate font-medium">{x.label}</span>
      <span className={cn("ml-auto shrink-0 rounded px-1.5 py-0.5 text-[10px]", statusTone(x.status))}>
        {t(`tasks.status.${x.status}`)}
      </span>
    </div>
  );

  if (!session) return <div className="p-4 text-[12.5px] text-muted-foreground">{t("tasks.empty")}</div>;

  return (
    <div className="flex flex-col gap-1 overflow-y-auto p-2">
      {live.length === 0 && finished.length === 0 ? (
        <div className="p-2 text-[12.5px] text-muted-foreground">{t("tasks.empty")}</div>
      ) : null}
      {live.map(Row)}
      {finished.length > 0 ? (
        <button
          type="button"
          onClick={() => setShowFinished((v) => !v)}
          className="mt-1 px-2 py-1 text-left text-[11px] text-muted-foreground hover:text-foreground"
        >
          {t("tasks.finished", { count: finished.length })}
        </button>
      ) : null}
      {showFinished ? finished.map(Row) : null}
    </div>
  );
}

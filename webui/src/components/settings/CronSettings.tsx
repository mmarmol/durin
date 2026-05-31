import { useCallback, useEffect, useState } from "react";
import { Clock, Loader2, Trash2 } from "lucide-react";
import { useTranslation } from "react-i18next";

import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";
import {
  ApiError,
  listCronJobs,
  removeCronJob,
  toggleCronJob,
  type CronJobRow,
} from "@/lib/api";

import {
  SettingsGroup,
  SettingsRow,
  SettingsSectionTitle,
} from "./primitives";

/** Settings → Cron section. Read+manage view over the cron scheduler:
 *  list every job (user-added + system), toggle enabled, remove the
 *  non-system ones. Adding is intentionally NOT exposed here — the
 *  `cron` agent tool already covers add (with its richer schema
 *  affordances around the message/channel/timezone interplay). The
 *  webui is the inspection + housekeeping surface. */
export function CronSettings({ token }: { token: string }) {
  const { t, i18n } = useTranslation();
  const [jobs, setJobs] = useState<CronJobRow[] | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [busyId, setBusyId] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const rows = await listCronJobs(token);
      setJobs(rows);
    } catch (e) {
      setError(e instanceof ApiError ? `HTTP ${e.status}` : (e as Error).message);
    } finally {
      setLoading(false);
    }
  }, [token]);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  const remove = async (id: string) => {
    setBusyId(id);
    try {
      await removeCronJob(token, id);
      await refresh();
    } catch (e) {
      setError(e instanceof ApiError ? `HTTP ${e.status}` : (e as Error).message);
    } finally {
      setBusyId(null);
    }
  };

  const toggle = async (id: string, next: boolean) => {
    setBusyId(id);
    try {
      await toggleCronJob(token, id, next);
      await refresh();
    } catch (e) {
      setError(e instanceof ApiError ? `HTTP ${e.status}` : (e as Error).message);
    } finally {
      setBusyId(null);
    }
  };

  return (
    <div className="space-y-8">
      <section>
        <SettingsSectionTitle>
          {t("settings.cron.title")}
        </SettingsSectionTitle>
        <p className="px-1 pb-3 text-[12px] text-muted-foreground">
          {t("settings.cron.description")}
        </p>
        <SettingsGroup>
          {loading ? (
            <SettingsRow title={t("settings.cron.loading")}>
              <Loader2 className="h-4 w-4 animate-spin text-muted-foreground" aria-hidden />
            </SettingsRow>
          ) : error ? (
            <SettingsRow title={t("settings.cron.loadError")}>
              <span className="text-[12px] text-destructive">{error}</span>
            </SettingsRow>
          ) : !jobs || jobs.length === 0 ? (
            <SettingsRow title={t("settings.cron.empty")}>
              <span className="text-[12px] text-muted-foreground">
                {t("settings.cron.emptyHint")}
              </span>
            </SettingsRow>
          ) : (
            jobs.map((job) => (
              <CronRow
                key={job.id}
                job={job}
                busy={busyId === job.id}
                onRemove={() => void remove(job.id)}
                onToggle={(next) => void toggle(job.id, next)}
                locale={i18n.language}
              />
            ))
          )}
        </SettingsGroup>
      </section>
    </div>
  );
}

function CronRow({
  job,
  busy,
  onRemove,
  onToggle,
  locale,
}: {
  job: CronJobRow;
  busy: boolean;
  onRemove: () => void;
  onToggle: (enabled: boolean) => void;
  locale: string;
}) {
  const { t } = useTranslation();
  const next = formatTimestamp(job.state.next_run_at_ms, locale);
  const last = formatTimestamp(job.state.last_run_at_ms, locale);
  const status = job.state.last_status;
  return (
    <SettingsRow
      title={
        <span className="flex items-center gap-2">
          <Clock className="h-3.5 w-3.5 text-muted-foreground" aria-hidden />
          <span>{job.name}</span>
          {job.is_system ? (
            <span className="rounded-full bg-muted px-1.5 py-0.5 text-[10px] uppercase tracking-wide text-muted-foreground">
              {t("settings.cron.systemBadge")}
            </span>
          ) : null}
        </span>
      }
      description={
        <span className="flex flex-col gap-0.5 text-[11px] text-muted-foreground">
          <span className="font-mono">{job.schedule.label}</span>
          {job.message ? <span className="truncate">{job.message}</span> : null}
          <span>
            {t("settings.cron.fieldNext")}: <span className="tabular-nums">{next}</span>
            {last ? (
              <>
                {" · "}
                {t("settings.cron.fieldLast")}: <span className="tabular-nums">{last}</span>
                {status ? (
                  <span
                    className={cn(
                      "ml-1",
                      status === "ok"
                        ? "text-emerald-600"
                        : status === "error"
                          ? "text-destructive"
                          : "",
                    )}
                  >
                    ({status})
                  </span>
                ) : null}
              </>
            ) : null}
          </span>
        </span>
      }
    >
      <div className="flex items-center gap-2">
        <Button
          size="sm"
          variant="ghost"
          disabled={busy}
          onClick={() => onToggle(!job.enabled)}
          className="rounded-full"
        >
          {job.enabled
            ? t("settings.models.enabled")
            : t("settings.models.disabled")}
        </Button>
        <Button
          size="sm"
          variant="ghost"
          disabled={busy || job.is_system}
          onClick={onRemove}
          className="rounded-full text-muted-foreground hover:text-destructive"
          title={
            job.is_system
              ? t("settings.cron.cantDeleteSystem")
              : t("settings.cron.remove")
          }
        >
          <Trash2 className="h-3.5 w-3.5" aria-hidden />
        </Button>
      </div>
    </SettingsRow>
  );
}

function formatTimestamp(ms: number | null, locale: string): string {
  if (!ms) return "—";
  try {
    return new Date(ms).toLocaleString(locale, {
      month: "short",
      day: "numeric",
      hour: "2-digit",
      minute: "2-digit",
    });
  } catch {
    return new Date(ms).toISOString();
  }
}

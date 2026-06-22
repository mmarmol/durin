import { useCallback, useEffect, useState } from "react";
import { ChevronDown, ChevronRight, Clock, ExternalLink, Loader2, Pencil, Play, Plus, Trash2 } from "lucide-react";
import { useTranslation } from "react-i18next";

import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";
import {
  addCronJob,
  ApiError,
  listChannels,
  listCronJobs,
  removeCronJob,
  runCronJob,
  toggleCronJob,
  updateCronJob,
  type ChannelInfo,
  type CronJobRow,
} from "@/lib/api";

import { ModelSelectField } from "@/components/ModelSelectField";

import {
  SettingsGroup,
  SettingsRow,
  SettingsSectionTitle,
} from "./primitives";

interface FormState {
  name: string;
  mode: string;
  message: string;
  schedule_kind: string;
  expr: string;
  every_seconds: string;
  model: string;
  deliver: boolean;
  channel: string;
  to: string;
  enabled: boolean;
}

const EMPTY_FORM: FormState = {
  name: "",
  mode: "reminder",
  message: "",
  schedule_kind: "cron",
  expr: "",
  every_seconds: "",
  model: "",
  deliver: false,
  channel: "",
  to: "",
  enabled: true,
};

function jobToForm(job: CronJobRow): FormState {
  // The select only offers "cron" and "every". One-shot "at" jobs aren't
  // creatable here; fall back to "cron" so the form can still edit the
  // non-schedule fields without crashing.
  const kind = job.schedule.kind === "every" ? "every" : "cron";
  return {
    name: job.name,
    mode: job.mode,
    message: job.message ?? "",
    schedule_kind: kind,
    expr: job.schedule.expr ?? "",
    every_seconds: job.schedule.every_ms != null ? String(job.schedule.every_ms / 1000) : "",
    model: job.model ?? "",
    deliver: false,
    channel: job.channel ?? "",
    to: "",
    enabled: job.enabled,
  };
}

/** Collapsible create/edit form for a cron job. */
function CronForm({
  token,
  editJob,
  onDone,
  onCancel,
}: {
  token: string;
  editJob: CronJobRow | null;
  onDone: () => void;
  onCancel: () => void;
}) {
  const { t } = useTranslation();
  const [form, setForm] = useState<FormState>(editJob ? jobToForm(editJob) : EMPTY_FORM);
  const [channels, setChannels] = useState<ChannelInfo[]>([]);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    listChannels(token).then((ch) => setChannels(ch.filter((c) => c.enabled))).catch(() => {});
  }, [token]);

  const set = <K extends keyof FormState>(key: K, value: FormState[K]) =>
    setForm((f) => ({ ...f, [key]: value }));

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setSaving(true);
    setError(null);
    try {
      const body = {
        name: form.name,
        message: form.message,
        mode: form.mode,
        model: form.model || null,
        schedule_kind: form.schedule_kind,
        expr: form.schedule_kind === "cron" ? form.expr || null : null,
        every_ms:
          form.schedule_kind === "every" && form.every_seconds
            ? Number(form.every_seconds) * 1000
            : null,
        deliver: form.deliver,
        channel: form.deliver && form.channel ? form.channel : null,
        to: form.deliver && form.to ? form.to : null,
      };
      if (editJob) {
        await updateCronJob(token, { id: editJob.id, ...body });
      } else {
        await addCronJob(token, body);
      }
      onDone();
    } catch (e) {
      setError(e instanceof ApiError ? `HTTP ${e.status}` : (e as Error).message);
    } finally {
      setSaving(false);
    }
  };

  const labelClass = "block text-[12px] font-medium text-foreground/80 mb-1";
  const inputClass =
    "w-full rounded-md border border-border/60 bg-background px-3 py-1.5 text-[13px] text-foreground " +
    "placeholder:text-muted-foreground focus:outline-none focus:ring-1 focus:ring-ring";
  const selectClass =
    "rounded-md border border-border/60 bg-background px-2 py-1.5 text-[13px] text-foreground " +
    "focus:outline-none focus:ring-1 focus:ring-ring";

  return (
    <form onSubmit={(e) => void handleSubmit(e)} className="space-y-4 px-5 py-4">
      {/* Name */}
      <div>
        <label htmlFor="cron-name" className={labelClass}>
          {t("settings.cron.fieldName")}
        </label>
        <input
          id="cron-name"
          className={inputClass}
          value={form.name}
          onChange={(e) => set("name", e.target.value)}
          required
          autoComplete="off"
        />
      </div>

      {/* Mode */}
      <div>
        <label htmlFor="cron-mode" className={labelClass}>
          {t("settings.cron.fieldMode")}
        </label>
        <select
          id="cron-mode"
          className={selectClass}
          value={form.mode}
          onChange={(e) => set("mode", e.target.value)}
        >
          <option value="reminder">{t("settings.cron.modeReminder")}</option>
          <option value="task">{t("settings.cron.modeTask")}</option>
        </select>
        <p className="mt-1 text-[11px] text-muted-foreground">{t("settings.cron.modeHelp")}</p>
      </div>

      {/* Prompt */}
      <div>
        <label htmlFor="cron-prompt" className={labelClass}>
          {t("settings.cron.fieldPrompt")}
        </label>
        <textarea
          id="cron-prompt"
          className={cn(inputClass, "resize-y min-h-[64px]")}
          value={form.message}
          onChange={(e) => set("message", e.target.value)}
          required
        />
      </div>

      {/* Schedule */}
      <div>
        <span className={labelClass}>{t("settings.cron.fieldSchedule")}</span>
        <div className="flex items-center gap-2">
          <select
            className={selectClass}
            value={form.schedule_kind}
            onChange={(e) => set("schedule_kind", e.target.value)}
            aria-label={t("settings.cron.fieldSchedule")}
          >
            <option value="cron">{t("settings.cron.scheduleKindCron")}</option>
            <option value="every">{t("settings.cron.scheduleKindInterval")}</option>
          </select>
          {form.schedule_kind === "cron" ? (
            <input
              id="cron-expr"
              className={cn(inputClass, "flex-1")}
              placeholder="0 9 * * *"
              value={form.expr}
              onChange={(e) => set("expr", e.target.value)}
              aria-label={t("settings.cron.scheduleKindCron")}
              required
            />
          ) : (
            <input
              id="cron-interval"
              type="number"
              min="1"
              className={cn(inputClass, "flex-1")}
              placeholder="3600"
              value={form.every_seconds}
              onChange={(e) => set("every_seconds", e.target.value)}
              aria-label={t("settings.cron.scheduleKindInterval")}
              required
            />
          )}
        </div>
      </div>

      {/* Model */}
      <div>
        <label className={labelClass}>{t("settings.cron.fieldModel")}</label>
        <ModelSelectField value={form.model} onChange={(ref) => set("model", ref)} />
      </div>

      {/* Deliver toggle */}
      <div className="flex items-center gap-3">
        <input
          id="cron-deliver"
          type="checkbox"
          checked={form.deliver}
          onChange={(e) => set("deliver", e.target.checked)}
          className="h-4 w-4 rounded border-border accent-primary"
        />
        <label htmlFor="cron-deliver" className="text-[13px] text-foreground/80">
          {t("settings.cron.fieldDeliver")}
        </label>
      </div>

      {/* Channel + To (visible only when deliver is on) */}
      {form.deliver ? (
        <div className="grid grid-cols-2 gap-3 pl-7">
          <div>
            <label htmlFor="cron-channel" className={labelClass}>
              {t("settings.cron.fieldChannel")}
            </label>
            <select
              id="cron-channel"
              className={cn(selectClass, "w-full")}
              value={form.channel}
              onChange={(e) => set("channel", e.target.value)}
            >
              <option value="">{t("settings.cron.fieldChannelPlaceholder")}</option>
              {channels.map((ch) => (
                <option key={ch.name} value={ch.name}>
                  {ch.display_name}
                </option>
              ))}
            </select>
          </div>
          <div>
            <label htmlFor="cron-to" className={labelClass}>
              {t("settings.cron.fieldTo")}
            </label>
            <input
              id="cron-to"
              className={inputClass}
              value={form.to}
              onChange={(e) => set("to", e.target.value)}
              placeholder={t("settings.cron.fieldToPlaceholder")}
            />
            <p className="mt-1 text-[11px] text-muted-foreground">
              {t("settings.cron.fieldToHelp")}
            </p>
          </div>
        </div>
      ) : null}

      {/* Error */}
      {error ? (
        <p className="text-[12px] text-destructive">{error}</p>
      ) : null}

      {/* Actions */}
      <div className="flex items-center justify-end gap-2 pt-1">
        <Button type="button" size="sm" variant="ghost" onClick={onCancel} disabled={saving}>
          {t("settings.cron.cancel")}
        </Button>
        <Button type="submit" size="sm" disabled={saving}>
          {saving ? (
            <Loader2 className="mr-1.5 h-3.5 w-3.5 animate-spin" aria-hidden />
          ) : null}
          {t("settings.cron.save")}
        </Button>
      </div>
    </form>
  );
}

/** Settings → Cron section. Read+manage view over the cron scheduler:
 *  list every job (user-added + system), toggle enabled, remove or edit the
 *  non-system ones, and add new ones via the inline form. */
export function CronSettings({
  token,
  onOpenSession,
}: {
  token: string;
  onOpenSession?: (sessionKey: string) => void;
}) {
  const { t, i18n } = useTranslation();
  const [jobs, setJobs] = useState<CronJobRow[] | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [busyId, setBusyId] = useState<string | null>(null);
  // null = form closed; 'new' = create; CronJobRow = edit
  const [formTarget, setFormTarget] = useState<"new" | CronJobRow | null>(null);

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

  const run = async (id: string) => {
    setBusyId(id);
    setError(null);
    try {
      const r = await runCronJob(token, id);
      if (!r.started) setError(t("settings.cron.alreadyRunning"));
      await refresh();
    } catch (e) {
      setError(e instanceof ApiError ? `HTTP ${e.status}` : (e as Error).message);
    } finally {
      setBusyId(null);
    }
  };

  const handleFormDone = async () => {
    setFormTarget(null);
    await refresh();
  };

  return (
    <div className="space-y-8">
      <section>
        <div className="mb-2 flex items-center justify-between px-1">
          <SettingsSectionTitle>
            {t("settings.cron.title")}
          </SettingsSectionTitle>
          <Button
            size="sm"
            variant="ghost"
            onClick={() => setFormTarget("new")}
            className="h-7 gap-1 text-[12px]"
          >
            <Plus className="h-3.5 w-3.5" aria-hidden />
            {t("settings.cron.addJob")}
          </Button>
        </div>
        <p className="px-1 pb-3 text-[12px] text-muted-foreground">
          {t("settings.cron.description")}
        </p>

        {/* Inline form (create or edit) */}
        {formTarget !== null ? (
          <div className="mb-4 overflow-hidden rounded-[22px] border border-border/45 bg-card/86 shadow-[0_18px_65px_rgba(15,23,42,0.075)] backdrop-blur-xl dark:border-white/10">
            <div className="border-b border-border/45 px-5 py-2.5 text-[12px] font-semibold text-foreground/70">
              {formTarget === "new"
                ? t("settings.cron.addJob")
                : t("settings.cron.editJob")}
            </div>
            <CronForm
              token={token}
              editJob={formTarget === "new" ? null : formTarget}
              onDone={() => void handleFormDone()}
              onCancel={() => setFormTarget(null)}
            />
          </div>
        ) : null}

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
                onRun={() => void run(job.id)}
                onEdit={() => setFormTarget(job)}
                locale={i18n.language}
                onOpenSession={onOpenSession}
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
  onRun,
  onEdit,
  locale,
  onOpenSession,
}: {
  job: CronJobRow;
  busy: boolean;
  onRemove: () => void;
  onToggle: (enabled: boolean) => void;
  onRun: () => void;
  onEdit: () => void;
  locale: string;
  onOpenSession?: (sessionKey: string) => void;
}) {
  const { t } = useTranslation();
  const [historyOpen, setHistoryOpen] = useState(false);
  const next = formatTimestamp(job.state.next_run_at_ms, locale);
  const last = formatTimestamp(job.state.last_run_at_ms, locale);
  const status = job.state.last_status;
  // System job ids are opaque (e.g. "dream", "memory_dream"); the user
  // shouldn't have to know the internal name to understand what the job
  // does. Map known system ids to a translated display label + a short
  // explanation; for user-created jobs (or unknown system ones) we fall
  // back to the raw job.name.
  const displayName = job.is_system
    ? t(`settings.cron.systemJobs.${job.id}.name`, { defaultValue: job.name })
    : job.name;
  const systemNote = job.is_system
    ? t(`settings.cron.systemJobs.${job.id}.note`, { defaultValue: "" })
    : "";
  return (
    <>
    <SettingsRow
      title={
        <span className="flex items-center gap-2">
          <Clock className="h-3.5 w-3.5 text-muted-foreground" aria-hidden />
          <span>{displayName}</span>
          {job.is_system ? (
            <span className="rounded-full bg-muted px-1.5 py-0.5 text-[10px] uppercase tracking-wide text-muted-foreground">
              {t("settings.cron.systemBadge")}
            </span>
          ) : null}
          {job.is_system ? (
            <span className="font-mono text-[10px] text-muted-foreground/60">
              ({job.id})
            </span>
          ) : null}
        </span>
      }
      description={
        <span className="flex flex-col gap-0.5 text-[11px] text-muted-foreground">
          <span className="font-mono">{job.schedule.label}</span>
          {systemNote ? <span>{systemNote}</span> : null}
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
          disabled={busy || !job.enabled || !!job.state.executing}
          onClick={onRun}
          className="rounded-full"
          title={t("settings.cron.runNow")}
        >
          {job.state.executing ? (
            <Loader2 className="h-3.5 w-3.5 animate-spin" aria-hidden />
          ) : (
            <Play className="h-3.5 w-3.5" aria-hidden />
          )}
        </Button>
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
        {/* System jobs cannot be edited or removed (only disabled). */}
        {job.is_system ? null : (
          <>
            <Button
              size="sm"
              variant="ghost"
              disabled={busy}
              onClick={onEdit}
              className="rounded-full text-muted-foreground"
              title={t("settings.cron.editJob")}
              aria-label={t("settings.cron.editJob")}
            >
              <Pencil className="h-3.5 w-3.5" aria-hidden />
            </Button>
            <Button
              size="sm"
              variant="ghost"
              disabled={busy}
              onClick={onRemove}
              className="rounded-full text-muted-foreground hover:text-destructive"
              title={t("settings.cron.remove")}
            >
              <Trash2 className="h-3.5 w-3.5" aria-hidden />
            </Button>
          </>
        )}
      </div>
    </SettingsRow>
    {/* Run history expandable section */}
    <RunHistory
      history={job.run_history}
      open={historyOpen}
      onToggle={() => setHistoryOpen((v) => !v)}
      locale={locale}
      onOpenSession={onOpenSession}
    />
    </>
  );
}

const HISTORY_PAGE = 8;

function RunHistory({
  history,
  open,
  onToggle,
  locale,
  onOpenSession,
}: {
  history: CronJobRow["run_history"];
  open: boolean;
  onToggle: () => void;
  locale: string;
  onOpenSession?: (sessionKey: string) => void;
}) {
  const { t } = useTranslation();
  const [showAll, setShowAll] = useState(false);
  // Sort newest-first without mutating the prop.
  const runs = [...(history ?? [])].sort((a, b) => b.run_at_ms - a.run_at_ms);
  const visible = showAll ? runs : runs.slice(0, HISTORY_PAGE);
  const hasMore = runs.length > HISTORY_PAGE;

  return (
    <div className="border-t border-border/30 bg-muted/20">
      <button
        type="button"
        onClick={onToggle}
        className="flex w-full items-center gap-1.5 px-5 py-1.5 text-[11px] text-muted-foreground hover:text-foreground/80"
        aria-expanded={open}
      >
        {open ? (
          <ChevronDown className="h-3 w-3" aria-hidden />
        ) : (
          <ChevronRight className="h-3 w-3" aria-hidden />
        )}
        {t("settings.cron.history")}
        {runs.length > 0 ? (
          <span className="ml-1 rounded-full bg-muted px-1.5 text-[10px]">
            {runs.length}
          </span>
        ) : null}
      </button>
      {open ? (
        <div className="px-5 pb-3">
          {runs.length === 0 ? (
            <p className="text-[11px] text-muted-foreground">
              {t("settings.cron.noRuns")}
            </p>
          ) : (
            <>
              <table className="w-full text-[11px]">
                <thead>
                  <tr className="text-left text-muted-foreground">
                    <th className="pb-1 pr-3 font-medium">{t("settings.cron.runAt")}</th>
                    <th className="pb-1 pr-3 font-medium">{t("settings.cron.status")}</th>
                    <th className="pb-1 pr-3 font-medium">{t("settings.cron.duration")}</th>
                    <th className="pb-1 pr-3 font-medium">{t("settings.cron.model")}</th>
                    <th className="pb-1 font-medium"></th>
                  </tr>
                </thead>
                <tbody>
                  {visible.map((run) => (
                    <tr key={run.run_at_ms} className="border-t border-border/20">
                      <td className="py-1 pr-3 tabular-nums">
                        {formatTimestamp(run.run_at_ms, locale)}
                      </td>
                      <td className="py-1 pr-3">
                        <span
                          className={cn(
                            "font-medium",
                            run.status === "ok"
                              ? "text-emerald-600"
                              : run.status === "error"
                                ? "text-destructive"
                                : "text-muted-foreground",
                          )}
                          title={run.error ?? undefined}
                        >
                          {run.status}
                        </span>
                      </td>
                      <td className="py-1 pr-3 tabular-nums text-muted-foreground">
                        {formatDuration(run.duration_ms)}
                      </td>
                      <td className="py-1 pr-3 text-muted-foreground">
                        {run.model ?? "—"}
                      </td>
                      <td className="py-1">
                        {run.session_key ? (
                          <Button
                            size="sm"
                            variant="ghost"
                            className="h-5 rounded px-1.5 text-[10px]"
                            title={run.session_key}
                            aria-label={t("settings.cron.openRun")}
                            onClick={() => onOpenSession?.(run.session_key!)}
                          >
                            <ExternalLink className="mr-0.5 h-2.5 w-2.5" aria-hidden />
                            {t("settings.cron.openRun")}
                          </Button>
                        ) : null}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
              {hasMore ? (
                <button
                  type="button"
                  onClick={() => setShowAll((v) => !v)}
                  className="mt-1.5 text-[11px] text-muted-foreground hover:text-foreground/80"
                >
                  {showAll ? t("settings.cron.showLess") : t("settings.cron.showMore")}
                </button>
              ) : null}
            </>
          )}
        </div>
      ) : null}
    </div>
  );
}

function formatDuration(ms: number): string {
  if (ms < 1000) return `${ms}ms`;
  return `${(ms / 1000).toFixed(1)}s`;
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

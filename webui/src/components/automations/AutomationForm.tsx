import { useEffect, useRef, useState } from "react";
import { Check, Copy, Loader2, Plus, Trash2 } from "lucide-react";
import { useTranslation } from "react-i18next";

import { Button } from "@/components/ui/button";
import { Textarea } from "@/components/ui/textarea";
import { DeleteConfirm } from "@/components/DeleteConfirm";
import { cn } from "@/lib/utils";
import {
  ApiError,
  deleteAutomation,
  getAutomationsHooksSecret,
  listAutomations,
  listWorkflows,
  saveAutomation,
  type AutomationDef,
  type AutomationTrigger,
} from "@/lib/api";

const CHANNEL_KINDS = ["email", "telegram", "slack", "discord", "whatsapp"] as const;
type ChannelKind = (typeof CHANNEL_KINDS)[number];

interface FilterPair {
  key: string;
  value: string;
}

const NAMED_FILTER_KEYS = new Set([
  "from_contains",
  "subject_contains",
  "sender_contains",
  "text_contains",
]);

interface TriggerRow {
  // Stable identity for this row, independent of its position in the array —
  // removeTrigger() shifts array indices, so any state keyed by index (e.g.
  // revealedSecretRows) would desync after a removal — key by rowId instead.
  rowId: string;
  source: "schedule" | "channel" | "webhook" | "chain";
  // schedule
  scheduleKind: "cron" | "every" | "at";
  expr: string;
  tz: string;
  everySeconds: string;
  atLocal: string; // datetime-local value, local time
  task: string;
  // channel
  channel: ChannelKind;
  fromContains: string;
  subjectContains: string;
  senderContains: string;
  textContains: string;
  // Every filter key that is not one of the four named inputs above: the
  // universal facts (chat, sender_kind, ...) and each channel's own
  // vocabulary. Kept as pairs so a definition authored elsewhere survives a
  // round-trip through this form untouched.
  otherFilters: FilterPair[];
  semantic: string; // shared by channel + webhook
  match: "wake_or_new" | "always_new";
  correlate: string; // shared by channel + webhook
  // webhook
  hook: string;
  // chain
  chainAutomation: string;
  chainWhen: "achieved" | "completed" | "failed" | "any";
}

interface FormState {
  name: string;
  triggers: TriggerRow[];
  workflow: string;
  deliveryChannel: string;
  deliveryTo: string;
  notify: "always" | "failures_only" | "when_notable" | "never";
  silentLabels: string[];
  helpChannel: string;
  helpTo: string;
  lifeEnabled: boolean;
  intent: string;
  achievedWhenKind: "any_completed" | "label";
  achievedWhenLabel: string;
  maxAttempts: string;
  onStuck: "escalate_pause" | "notify" | "keep";
  // No UI exposes this — preserved from the loaded definition on edit (or
  // defaulted on create) so saving from this form never silently resets an
  // automation an API/tool caller configured as "parallel".
  concurrency: "single" | "parallel";
}

// Monotonic counter for TriggerRow.rowId — unique within a form session,
// which is all that's needed since rows never persist across mounts.
let rowIdCounter = 0;
const nextRowId = () => `automation-trigger-${rowIdCounter++}`;

const EMPTY_TRIGGER: Omit<TriggerRow, "rowId"> = {
  source: "schedule",
  scheduleKind: "cron",
  expr: "",
  tz: "",
  everySeconds: "",
  atLocal: "",
  task: "",
  channel: "email",
  fromContains: "",
  subjectContains: "",
  senderContains: "",
  textContains: "",
  otherFilters: [],
  semantic: "",
  match: "wake_or_new",
  correlate: "",
  hook: "",
  chainAutomation: "",
  chainWhen: "any",
};

const EMPTY_FORM: FormState = {
  name: "",
  triggers: [],
  workflow: "",
  deliveryChannel: "",
  deliveryTo: "",
  notify: "always",
  silentLabels: ["NOTHING_TO_REPORT"],
  helpChannel: "",
  helpTo: "",
  lifeEnabled: false,
  intent: "",
  achievedWhenKind: "any_completed",
  achievedWhenLabel: "",
  maxAttempts: "",
  onStuck: "notify",
  concurrency: "single",
};

/** epoch ms -> "YYYY-MM-DDTHH:mm" in local time, for a datetime-local input. */
function msToLocalInput(ms: number): string {
  const d = new Date(ms);
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

/** A datetime-local input's value has no timezone designator, so the Date
 *  constructor parses it as local time — the exact inverse of msToLocalInput,
 *  keeping the round-trip timezone-independent. */
function localInputToMs(value: string): number {
  return new Date(value).getTime();
}

function parseAchievedWhen(raw: string): { kind: "any_completed" | "label"; label: string } {
  if (raw.startsWith("label:")) return { kind: "label", label: raw.slice("label:".length) };
  return { kind: "any_completed", label: "" };
}

function defToForm(def: AutomationDef): FormState {
  const achievedWhen = parseAchievedWhen(def.life?.achieved_when ?? "any_completed");
  return {
    name: def.name,
    triggers: def.triggers.map((trig): TriggerRow => {
      if (trig.source === "channel") {
        return {
          ...EMPTY_TRIGGER,
          rowId: nextRowId(),
          source: "channel",
          channel: trig.channel ?? "email",
          fromContains: trig.filters?.from_contains ?? "",
          subjectContains: trig.filters?.subject_contains ?? "",
          senderContains: trig.filters?.sender_contains ?? "",
          textContains: trig.filters?.text_contains ?? "",
          otherFilters: Object.entries(trig.filters ?? {})
            .filter(([key]) => !NAMED_FILTER_KEYS.has(key))
            .map(([key, value]) => ({ key, value: String(value ?? "") })),
          semantic: trig.semantic ?? "",
          match: trig.match ?? "wake_or_new",
          correlate: trig.correlate ?? "",
        };
      }
      if (trig.source === "webhook") {
        return {
          ...EMPTY_TRIGGER,
          rowId: nextRowId(),
          source: "webhook",
          hook: trig.hook ?? "",
          semantic: trig.semantic ?? "",
          correlate: trig.correlate ?? "",
        };
      }
      if (trig.source === "chain") {
        return {
          ...EMPTY_TRIGGER,
          rowId: nextRowId(),
          source: "chain",
          chainAutomation: trig.chain_automation ?? "",
          chainWhen: trig.chain_when ?? "any",
        };
      }
      const sched = trig.schedule;
      const scheduleKind = sched?.kind === "every" ? "every" : sched?.kind === "at" ? "at" : "cron";
      return {
        ...EMPTY_TRIGGER,
        rowId: nextRowId(),
        source: "schedule",
        scheduleKind,
        expr: sched?.expr ?? "",
        tz: sched?.tz ?? "",
        everySeconds: sched?.every_ms != null ? String(sched.every_ms / 1000) : "",
        atLocal: sched?.at_ms != null ? msToLocalInput(sched.at_ms) : "",
        task: trig.task ?? "",
      };
    }),
    workflow: def.workflow,
    deliveryChannel: def.delivery.channel ?? "",
    deliveryTo: def.delivery.to ?? "",
    notify: def.delivery.notify,
    silentLabels: def.delivery.silent_labels ?? [],
    helpChannel: def.help.channel ?? "",
    helpTo: def.help.to ?? "",
    lifeEnabled: def.life != null,
    intent: def.life?.intent ?? "",
    achievedWhenKind: achievedWhen.kind,
    achievedWhenLabel: achievedWhen.label,
    maxAttempts: def.life?.max_attempts != null ? String(def.life.max_attempts) : "",
    onStuck: def.life?.on_stuck ?? "notify",
    concurrency: def.concurrency,
  };
}

function formToDef(form: FormState, enabled: boolean): AutomationDef {
  const triggers: AutomationTrigger[] = form.triggers.map((row): AutomationTrigger => {
    if (row.source === "channel") {
      const filters: Record<string, string> = {};
      // from/subject inputs are only shown in the UI for email, but the
      // backend accepts them on any channel — an out-of-band definition
      // (API/tool-authored) may already carry them on a non-email row, and
      // re-saving from the webui must not silently drop them.
      if (row.fromContains.trim()) filters.from_contains = row.fromContains.trim();
      if (row.subjectContains.trim()) filters.subject_contains = row.subjectContains.trim();
      if (row.senderContains.trim()) filters.sender_contains = row.senderContains.trim();
      if (row.textContains.trim()) filters.text_contains = row.textContains.trim();
      // Same reasoning, now for the open vocabulary: whatever this form did
      // not give a named input to still has to come back out intact.
      for (const pair of row.otherFilters) {
        const key = pair.key.trim();
        const value = pair.value.trim();
        if (key && value) filters[key] = value;
      }
      return {
        source: "channel",
        channel: row.channel,
        filters,
        match: row.match,
        ...(row.semantic.trim() ? { semantic: row.semantic.trim() } : {}),
        ...(row.correlate.trim() ? { correlate: row.correlate.trim() } : {}),
      };
    }
    if (row.source === "webhook") {
      return {
        source: "webhook",
        hook: row.hook.trim(),
        ...(row.semantic.trim() ? { semantic: row.semantic.trim() } : {}),
        ...(row.correlate.trim() ? { correlate: row.correlate.trim() } : {}),
      };
    }
    if (row.source === "chain") {
      return {
        source: "chain",
        chain_automation: row.chainAutomation.trim(),
        chain_when: row.chainWhen,
      };
    }
    const schedule =
      row.scheduleKind === "cron"
        ? { kind: "cron" as const, expr: row.expr, ...(row.tz ? { tz: row.tz } : {}) }
        : row.scheduleKind === "every"
          ? { kind: "every" as const, every_ms: Number(row.everySeconds) * 1000 }
          : { kind: "at" as const, at_ms: localInputToMs(row.atLocal) };
    return { source: "schedule", schedule, task: row.task.trim() };
  });
  return {
    name: form.name.trim(),
    workflow: form.workflow,
    enabled,
    triggers,
    delivery: {
      ...(form.deliveryChannel ? { channel: form.deliveryChannel } : {}),
      ...(form.deliveryTo.trim() ? { to: form.deliveryTo.trim() } : {}),
      notify: form.notify,
      silent_labels: form.silentLabels,
    },
    help: {
      ...(form.helpChannel ? { channel: form.helpChannel } : {}),
      ...(form.helpTo.trim() ? { to: form.helpTo.trim() } : {}),
    },
    ...(form.lifeEnabled
      ? {
          life: {
            intent: form.intent.trim(),
            achieved_when:
              form.achievedWhenKind === "label"
                ? `label:${form.achievedWhenLabel.trim()}`
                : ("any_completed" as const),
            ...(form.maxAttempts.trim() ? { max_attempts: Number(form.maxAttempts) } : {}),
            on_stuck: form.onStuck,
          },
        }
      : {}),
    concurrency: form.concurrency,
  };
}

function errMsg(e: unknown): string {
  if (e instanceof ApiError) return e.detail ? `HTTP ${e.status}: ${e.detail}` : `HTTP ${e.status}`;
  return (e as Error).message;
}

/** A row of mutually-exclusive pill buttons — the mockup's "radio" / segmented
 *  control, reused for notify / achieved_when / on_stuck. */
function PillGroup<T extends string>({
  ariaLabel,
  value,
  onChange,
  options,
}: {
  ariaLabel: string;
  value: T;
  onChange: (v: T) => void;
  options: { value: T; label: string }[];
}) {
  return (
    <div className="flex flex-wrap gap-1.5" role="group" aria-label={ariaLabel}>
      {options.map((opt) => (
        <button
          key={opt.value}
          type="button"
          aria-pressed={value === opt.value}
          onClick={() => onChange(opt.value)}
          className={cn(
            "rounded-full border px-3 py-1 text-[11.5px] font-medium transition-colors",
            value === opt.value
              ? "border-accent bg-accent/15 text-accent"
              : "border-border/60 text-muted-foreground hover:text-foreground",
          )}
        >
          {opt.label}
        </button>
      ))}
    </div>
  );
}

/** Create/edit form for an automation definition — disparadores (triggers),
 *  qué corre (workflow), entrega (delivery), ayuda (help) and vida (life),
 *  in the mockup's reading order. Trigger editors (schedule/channel/webhook
 *  rows, filters, semantic, correlate, the hook secret reveal) reuse the
 *  established trigger-editing UX, adapted to AutomationDef's shape (a
 *  "schedule" source instead of "cron", a per-trigger task, and the new
 *  chain trigger). */
export function AutomationForm({
  token,
  editAutomation,
  onDone,
  onCancel,
}: {
  token: string;
  editAutomation: AutomationDef | null;
  onDone: () => void;
  onCancel: () => void;
}) {
  const { t } = useTranslation();
  const [form, setForm] = useState<FormState>(editAutomation ? defToForm(editAutomation) : EMPTY_FORM);
  const [workflows, setWorkflows] = useState<string[]>([]);
  const [automationNames, setAutomationNames] = useState<string[]>([]);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // Which submit button was clicked (Save & enable vs Save as paused) — set
  // by the button's own onClick, which fires before the form's submit event.
  const enabledOnSubmitRef = useRef(true);
  const formRef = useRef<HTMLFormElement>(null);

  const [confirmingDelete, setConfirmingDelete] = useState(false);
  const [deleting, setDeleting] = useState(false);

  const [silentLabelDraft, setSilentLabelDraft] = useState("");

  // Webhook ingress secret: shared across every webhook row, fetched at most
  // once and only on demand (never on mount) — a secret must stay hidden
  // until the user explicitly asks to see it.
  const [hooksSecret, setHooksSecret] = useState<string | null>(null);
  const [secretLoading, setSecretLoading] = useState(false);
  const [revealedSecretRows, setRevealedSecretRows] = useState<Set<string>>(new Set());
  const [copiedSecret, setCopiedSecret] = useState(false);

  useEffect(() => {
    listWorkflows(token).then(setWorkflows).catch(() => {});
  }, [token]);

  useEffect(() => {
    listAutomations(token)
      .then((defs) => setAutomationNames(defs.map((d) => d.name)))
      .catch(() => {});
  }, [token]);

  const set = <K extends keyof FormState>(key: K, value: FormState[K]) =>
    setForm((f) => ({ ...f, [key]: value }));

  const setTrigger = <K extends keyof TriggerRow>(i: number, key: K, value: TriggerRow[K]) =>
    setForm((f) => ({
      ...f,
      triggers: f.triggers.map((row, idx) => (idx === i ? { ...row, [key]: value } : row)),
    }));

  const addTrigger = () =>
    setForm((f) => ({ ...f, triggers: [...f.triggers, { ...EMPTY_TRIGGER, rowId: nextRowId() }] }));
  const removeTrigger = (i: number) =>
    setForm((f) => ({ ...f, triggers: f.triggers.filter((_, idx) => idx !== i) }));

  const setFilterPair = (i: number, j: number, key: keyof FilterPair, value: string) =>
    setForm((f) => ({
      ...f,
      triggers: f.triggers.map((row, idx) =>
        idx === i
          ? {
              ...row,
              otherFilters: row.otherFilters.map((pair, pairIdx) =>
                pairIdx === j ? { ...pair, [key]: value } : pair,
              ),
            }
          : row,
      ),
    }));
  const addFilterPair = (i: number) =>
    setForm((f) => ({
      ...f,
      triggers: f.triggers.map((row, idx) =>
        idx === i ? { ...row, otherFilters: [...row.otherFilters, { key: "", value: "" }] } : row,
      ),
    }));
  const removeFilterPair = (i: number, j: number) =>
    setForm((f) => ({
      ...f,
      triggers: f.triggers.map((row, idx) =>
        idx === i
          ? { ...row, otherFilters: row.otherFilters.filter((_, pairIdx) => pairIdx !== j) }
          : row,
      ),
    }));

  const addSilentLabel = () => {
    const label = silentLabelDraft.trim();
    if (!label) return;
    setForm((f) => ({ ...f, silentLabels: [...f.silentLabels, label] }));
    setSilentLabelDraft("");
  };
  const removeSilentLabel = (i: number) =>
    setForm((f) => ({ ...f, silentLabels: f.silentLabels.filter((_, idx) => idx !== i) }));

  const showSecret = async (rowId: string) => {
    setRevealedSecretRows((prev) => new Set(prev).add(rowId));
    if (hooksSecret !== null) return;
    setSecretLoading(true);
    try {
      const res = await getAutomationsHooksSecret(token);
      setHooksSecret(res.secret);
    } catch (e) {
      // Undo the optimistic reveal: with hooksSecret still null and rowId
      // still in revealedSecretRows, the row would otherwise render its
      // Loader2 spinner forever with no secret ever coming and no button
      // left to retry. Falling back to the "Show secret" button lets the
      // user try again, and the shared inline error banner (same path
      // handleSubmit/handleDelete use) says why it failed.
      setRevealedSecretRows((prev) => {
        const next = new Set(prev);
        next.delete(rowId);
        return next;
      });
      setError(errMsg(e));
    } finally {
      setSecretLoading(false);
    }
  };

  const copySecret = async () => {
    if (!hooksSecret) return;
    try {
      await navigator.clipboard.writeText(hooksSecret);
      setCopiedSecret(true);
      setTimeout(() => setCopiedSecret(false), 2500);
    } catch {
      // clipboard failure — the value is still visible to select/copy manually.
    }
  };

  const handleDelete = async () => {
    if (!editAutomation) return;
    setConfirmingDelete(false);
    setDeleting(true);
    setError(null);
    try {
      await deleteAutomation(token, editAutomation.name);
      onDone();
    } catch (e) {
      setError(errMsg(e));
    } finally {
      setDeleting(false);
    }
  };

  // Native "press Enter to submit" is unreliable here: the form's default
  // button is nested a few DOM levels down inside the actions row, and the
  // browser's implicit-submission default action doesn't fire from every
  // single-line field in that layout. Drive it explicitly instead, so Enter
  // behaves the same regardless of where the input sits in the tree. The
  // intent textarea and the silent-label draft input are excluded (Enter
  // means "newline" / "add this tag" there, not "submit").
  const handleFormKeyDown = (e: React.KeyboardEvent<HTMLFormElement>) => {
    if (e.key !== "Enter" || e.nativeEvent.isComposing) return;
    const target = e.target as HTMLElement;
    if (target.tagName !== "INPUT" || (target as HTMLInputElement).type === "checkbox") return;
    e.preventDefault();
    enabledOnSubmitRef.current = true;
    formRef.current?.requestSubmit();
  };

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setSaving(true);
    setError(null);
    try {
      await saveAutomation(token, formToDef(form, enabledOnSubmitRef.current));
      onDone();
    } catch (e) {
      setError(errMsg(e));
    } finally {
      setSaving(false);
    }
  };

  const labelClass = "block text-[12px] font-medium text-foreground/80 mb-1";
  const rowLabelClass = "block text-[10.5px] text-muted-foreground mb-0.5";
  const inputClass =
    "w-full rounded-md border border-border/60 bg-background px-3 py-1.5 text-[13px] text-foreground " +
    "placeholder:text-muted-foreground focus:outline-none focus:ring-1 focus:ring-ring disabled:opacity-60";
  const selectClass =
    "w-full rounded-md border border-border/60 bg-background px-2 py-1.5 text-[13px] text-foreground " +
    "focus:outline-none focus:ring-1 focus:ring-ring";

  const channelOptions = (
    <>
      {CHANNEL_KINDS.map((c) => (
        <option key={c} value={c}>
          {t(`automations.chips.channel_${c}`)}
        </option>
      ))}
    </>
  );

  return (
    <form
      ref={formRef}
      onSubmit={(e) => void handleSubmit(e)}
      onKeyDown={handleFormKeyDown}
      className="space-y-5 px-5 py-4"
    >
      {error ? (
        <div className="rounded-md border border-destructive/40 bg-destructive/10 px-3 py-2 text-xs text-destructive">
          {error}
        </div>
      ) : null}

      {/* Name */}
      <div>
        <label htmlFor="automation-name" className={labelClass}>
          {t("automations.form.name")}
        </label>
        <input
          id="automation-name"
          className={inputClass}
          value={form.name}
          onChange={(e) => set("name", e.target.value)}
          readOnly={!!editAutomation}
          required
          autoComplete="off"
        />
      </div>

      {/* Disparadores */}
      <div data-testid="automation-group-triggers">
        <div className="mb-1 flex items-center justify-between">
          <span className={labelClass}>{t("automations.form.triggersTitle")}</span>
          <Button
            type="button"
            size="sm"
            variant="ghost"
            onClick={addTrigger}
            className="h-6 gap-1 px-2 text-[11px]"
          >
            <Plus className="h-3 w-3" aria-hidden /> {t("automations.form.addTrigger")}
          </Button>
        </div>
        {form.triggers.length === 0 ? (
          <p className="text-[11px] text-muted-foreground">{t("automations.form.noTriggersHint")}</p>
        ) : (
          <div className="space-y-2">
            {form.triggers.map((row, i) => (
              <div key={row.rowId} className="flex flex-wrap items-end gap-2 rounded-md border border-border/40 p-2">
                <div className="w-32">
                  <label htmlFor={`automation-trigger-source-${i}`} className={rowLabelClass}>
                    {t("automations.form.source")}
                  </label>
                  <select
                    id={`automation-trigger-source-${i}`}
                    className={selectClass}
                    value={row.source}
                    onChange={(e) => setTrigger(i, "source", e.target.value as TriggerRow["source"])}
                  >
                    <option value="schedule">{t("automations.form.sourceSchedule")}</option>
                    <option value="channel">{t("automations.form.sourceChannel")}</option>
                    <option value="webhook">{t("automations.form.sourceWebhook")}</option>
                    <option value="chain">{t("automations.form.sourceChain")}</option>
                  </select>
                </div>

                {row.source === "channel" ? (
                  <div className="w-32">
                    <label htmlFor={`automation-trigger-channel-${i}`} className={rowLabelClass}>
                      {t("automations.form.channel")}
                    </label>
                    <select
                      id={`automation-trigger-channel-${i}`}
                      className={selectClass}
                      value={row.channel}
                      onChange={(e) => setTrigger(i, "channel", e.target.value as ChannelKind)}
                    >
                      {channelOptions}
                    </select>
                  </div>
                ) : null}

                {row.source === "schedule" ? (
                  <>
                    <div className="w-32">
                      <label htmlFor={`automation-trigger-kind-${i}`} className={rowLabelClass}>
                        {t("automations.form.scheduleKind")}
                      </label>
                      <select
                        id={`automation-trigger-kind-${i}`}
                        className={selectClass}
                        value={row.scheduleKind}
                        onChange={(e) => setTrigger(i, "scheduleKind", e.target.value as TriggerRow["scheduleKind"])}
                      >
                        <option value="cron">{t("automations.form.scheduleKindCron")}</option>
                        <option value="every">{t("automations.form.scheduleKindEvery")}</option>
                        <option value="at">{t("automations.form.scheduleKindAt")}</option>
                      </select>
                    </div>
                    {row.scheduleKind === "cron" ? (
                      <>
                        <div className="min-w-[140px] flex-1">
                          <label htmlFor={`automation-trigger-expr-${i}`} className={rowLabelClass}>
                            {t("automations.form.exprLabel")}
                          </label>
                          <input
                            id={`automation-trigger-expr-${i}`}
                            className={inputClass}
                            placeholder="0 9 * * *"
                            value={row.expr}
                            onChange={(e) => setTrigger(i, "expr", e.target.value)}
                            required
                          />
                        </div>
                        <div className="w-28">
                          <label htmlFor={`automation-trigger-tz-${i}`} className={rowLabelClass}>
                            {t("automations.form.tz")}
                          </label>
                          <input
                            id={`automation-trigger-tz-${i}`}
                            className={inputClass}
                            placeholder="UTC"
                            value={row.tz}
                            onChange={(e) => setTrigger(i, "tz", e.target.value)}
                          />
                        </div>
                      </>
                    ) : row.scheduleKind === "every" ? (
                      <div className="min-w-[140px] flex-1">
                        <label htmlFor={`automation-trigger-interval-${i}`} className={rowLabelClass}>
                          {t("automations.form.intervalLabel")}
                        </label>
                        <input
                          id={`automation-trigger-interval-${i}`}
                          type="number"
                          min="1"
                          className={inputClass}
                          placeholder="3600"
                          value={row.everySeconds}
                          onChange={(e) => setTrigger(i, "everySeconds", e.target.value)}
                          required
                        />
                      </div>
                    ) : (
                      <div className="min-w-[180px] flex-1">
                        <label htmlFor={`automation-trigger-at-${i}`} className={rowLabelClass}>
                          {t("automations.form.atLabel")}
                        </label>
                        <input
                          id={`automation-trigger-at-${i}`}
                          type="datetime-local"
                          className={inputClass}
                          value={row.atLocal}
                          onChange={(e) => setTrigger(i, "atLocal", e.target.value)}
                          required
                        />
                      </div>
                    )}
                    <div className="min-w-[220px] flex-[2]">
                      <label htmlFor={`automation-trigger-task-${i}`} className={rowLabelClass}>
                        {t("automations.form.task")}
                      </label>
                      <input
                        id={`automation-trigger-task-${i}`}
                        className={inputClass}
                        value={row.task}
                        onChange={(e) => setTrigger(i, "task", e.target.value)}
                        required
                      />
                      <p className="mt-1 text-[10.5px] text-muted-foreground">{t("automations.form.taskHint")}</p>
                    </div>
                  </>
                ) : row.source === "channel" ? (
                  <>
                    {row.channel === "email" ? (
                      <>
                        <div className="min-w-[140px] flex-1">
                          <label htmlFor={`automation-trigger-from-${i}`} className={rowLabelClass}>
                            {t("automations.form.fromContains")}
                          </label>
                          <input
                            id={`automation-trigger-from-${i}`}
                            className={inputClass}
                            value={row.fromContains}
                            onChange={(e) => setTrigger(i, "fromContains", e.target.value)}
                          />
                        </div>
                        <div className="min-w-[140px] flex-1">
                          <label htmlFor={`automation-trigger-subject-${i}`} className={rowLabelClass}>
                            {t("automations.form.subjectContains")}
                          </label>
                          <input
                            id={`automation-trigger-subject-${i}`}
                            className={inputClass}
                            value={row.subjectContains}
                            onChange={(e) => setTrigger(i, "subjectContains", e.target.value)}
                          />
                        </div>
                      </>
                    ) : null}
                    <div className="min-w-[140px] flex-1">
                      <label htmlFor={`automation-trigger-sender-${i}`} className={rowLabelClass}>
                        {t("automations.form.senderContains")}
                      </label>
                      <input
                        id={`automation-trigger-sender-${i}`}
                        className={inputClass}
                        value={row.senderContains}
                        onChange={(e) => setTrigger(i, "senderContains", e.target.value)}
                      />
                    </div>
                    <div className="min-w-[140px] flex-1">
                      <label htmlFor={`automation-trigger-text-${i}`} className={rowLabelClass}>
                        {t("automations.form.textContains")}
                      </label>
                      <input
                        id={`automation-trigger-text-${i}`}
                        className={inputClass}
                        value={row.textContains}
                        onChange={(e) => setTrigger(i, "textContains", e.target.value)}
                      />
                    </div>
                    <div className="min-w-[260px] flex-[2]">
                      <span className={rowLabelClass}>{t("automations.form.otherFilters")}</span>
                      {row.otherFilters.map((pair, j) => (
                        <div key={`${row.rowId}-filter-${j}`} className="mt-1 flex gap-1">
                          <input
                            aria-label={t("automations.form.filterKey")}
                            placeholder={t("automations.form.filterKeyPlaceholder")}
                            className={`${inputClass} flex-1`}
                            value={pair.key}
                            onChange={(e) => setFilterPair(i, j, "key", e.target.value)}
                          />
                          <input
                            aria-label={t("automations.form.filterValue")}
                            placeholder={t("automations.form.filterValuePlaceholder")}
                            className={`${inputClass} flex-1`}
                            value={pair.value}
                            onChange={(e) => setFilterPair(i, j, "value", e.target.value)}
                          />
                          <button
                            type="button"
                            aria-label={t("automations.form.removeFilter")}
                            className="px-2 text-xs text-muted-foreground hover:text-foreground"
                            onClick={() => removeFilterPair(i, j)}
                          >
                            ×
                          </button>
                        </div>
                      ))}
                      <button
                        type="button"
                        className="mt-1 text-[10.5px] text-muted-foreground hover:text-foreground"
                        onClick={() => addFilterPair(i)}
                      >
                        + {t("automations.form.addFilter")}
                      </button>
                      <p className="mt-1 text-[10.5px] text-muted-foreground">
                        {t("automations.form.otherFiltersHint")}
                      </p>
                    </div>
                    <div className="min-w-[200px] flex-[2]">
                      <label htmlFor={`automation-trigger-semantic-${i}`} className={rowLabelClass}>
                        {t("automations.form.semantic")}
                      </label>
                      <input
                        id={`automation-trigger-semantic-${i}`}
                        className={inputClass}
                        value={row.semantic}
                        onChange={(e) => setTrigger(i, "semantic", e.target.value)}
                      />
                      <p className="mt-1 text-[10.5px] text-muted-foreground">{t("automations.form.semanticHint")}</p>
                    </div>
                    <div className="min-w-[200px] flex-1">
                      <label htmlFor={`automation-trigger-correlate-${i}`} className={rowLabelClass}>
                        {t("automations.form.correlate")}
                      </label>
                      <input
                        id={`automation-trigger-correlate-${i}`}
                        className={inputClass}
                        value={row.correlate}
                        onChange={(e) => setTrigger(i, "correlate", e.target.value)}
                      />
                      <p className="mt-1 text-[10.5px] text-muted-foreground">{t("automations.form.correlateHint")}</p>
                    </div>
                    <div className="w-56">
                      <label htmlFor={`automation-trigger-match-${i}`} className={rowLabelClass}>
                        {t("automations.form.match")}
                      </label>
                      <select
                        id={`automation-trigger-match-${i}`}
                        className={selectClass}
                        value={row.match}
                        onChange={(e) => setTrigger(i, "match", e.target.value as TriggerRow["match"])}
                      >
                        <option value="wake_or_new">{t("automations.form.matchWakeOrNew")}</option>
                        <option value="always_new">{t("automations.form.matchAlwaysNew")}</option>
                      </select>
                    </div>
                  </>
                ) : row.source === "webhook" ? (
                  <>
                    <div className="min-w-[140px] flex-1">
                      <label htmlFor={`automation-trigger-hook-${i}`} className={rowLabelClass}>
                        {t("automations.form.hookName")}
                      </label>
                      <input
                        id={`automation-trigger-hook-${i}`}
                        className={inputClass}
                        value={row.hook}
                        onChange={(e) => setTrigger(i, "hook", e.target.value)}
                        required
                        autoComplete="off"
                      />
                    </div>
                    <div className="min-w-[200px] flex-1">
                      <label htmlFor={`automation-trigger-hook-url-${i}`} className={rowLabelClass}>
                        {t("automations.form.hookUrlLabel")}
                      </label>
                      <input
                        id={`automation-trigger-hook-url-${i}`}
                        className={cn(inputClass, "font-mono")}
                        value={`/api/v1/hooks/${row.hook}`}
                        readOnly
                      />
                    </div>
                    <div className="min-w-[200px] flex-[2]">
                      <label htmlFor={`automation-trigger-semantic-${i}`} className={rowLabelClass}>
                        {t("automations.form.semantic")}
                      </label>
                      <input
                        id={`automation-trigger-semantic-${i}`}
                        className={inputClass}
                        value={row.semantic}
                        onChange={(e) => setTrigger(i, "semantic", e.target.value)}
                      />
                      <p className="mt-1 text-[10.5px] text-muted-foreground">{t("automations.form.semanticHint")}</p>
                    </div>
                    <div className="min-w-[200px] flex-1">
                      <label htmlFor={`automation-trigger-correlate-${i}`} className={rowLabelClass}>
                        {t("automations.form.correlate")}
                      </label>
                      <input
                        id={`automation-trigger-correlate-${i}`}
                        className={inputClass}
                        value={row.correlate}
                        onChange={(e) => setTrigger(i, "correlate", e.target.value)}
                      />
                      <p className="mt-1 text-[10.5px] text-muted-foreground">{t("automations.form.correlateHint")}</p>
                    </div>
                    <div className="min-w-[160px]">
                      <span className={rowLabelClass}>{t("automations.form.hookSecret")}</span>
                      {revealedSecretRows.has(row.rowId) ? (
                        hooksSecret !== null ? (
                          <div className="flex items-center gap-1.5">
                            <code className="flex-1 truncate rounded-md border border-border/60 bg-background px-2 py-1.5 font-mono text-[12px]">
                              {hooksSecret}
                            </code>
                            <Button
                              type="button"
                              size="sm"
                              variant="ghost"
                              onClick={() => void copySecret()}
                              aria-label={t("automations.form.copySecret")}
                              className="h-8 w-8 shrink-0 p-0 text-muted-foreground"
                            >
                              {copiedSecret ? (
                                <Check className="h-3.5 w-3.5" aria-hidden />
                              ) : (
                                <Copy className="h-3.5 w-3.5" aria-hidden />
                              )}
                            </Button>
                          </div>
                        ) : (
                          <Loader2 className="h-4 w-4 animate-spin text-muted-foreground" aria-hidden />
                        )
                      ) : (
                        <Button
                          type="button"
                          size="sm"
                          variant="outline"
                          disabled={secretLoading}
                          onClick={() => void showSecret(row.rowId)}
                          className="h-8 text-[11px]"
                        >
                          {t("automations.form.showSecret")}
                        </Button>
                      )}
                    </div>
                  </>
                ) : (
                  <>
                    <div className="min-w-[200px] flex-1">
                      <label htmlFor={`automation-trigger-chain-automation-${i}`} className={rowLabelClass}>
                        {t("automations.form.chainAutomation")}
                      </label>
                      <select
                        id={`automation-trigger-chain-automation-${i}`}
                        className={selectClass}
                        value={row.chainAutomation}
                        onChange={(e) => setTrigger(i, "chainAutomation", e.target.value)}
                        required
                      >
                        <option value="" disabled>
                          {t("automations.form.chainAutomationPlaceholder")}
                        </option>
                        {automationNames
                          .filter((n) => n !== editAutomation?.name)
                          .map((n) => (
                            <option key={n} value={n}>
                              {n}
                            </option>
                          ))}
                      </select>
                    </div>
                    <div className="w-56">
                      <label htmlFor={`automation-trigger-chain-when-${i}`} className={rowLabelClass}>
                        {t("automations.form.chainWhen")}
                      </label>
                      <select
                        id={`automation-trigger-chain-when-${i}`}
                        className={selectClass}
                        value={row.chainWhen}
                        onChange={(e) => setTrigger(i, "chainWhen", e.target.value as TriggerRow["chainWhen"])}
                      >
                        <option value="achieved">{t("automations.form.chainWhenAchieved")}</option>
                        <option value="completed">{t("automations.form.chainWhenCompleted")}</option>
                        <option value="failed">{t("automations.form.chainWhenFailed")}</option>
                        <option value="any">{t("automations.form.chainWhenAny")}</option>
                      </select>
                    </div>
                  </>
                )}
                <Button
                  type="button"
                  size="sm"
                  variant="ghost"
                  onClick={() => removeTrigger(i)}
                  aria-label={t("automations.form.removeTrigger")}
                  className="h-8 w-8 shrink-0 p-0 text-muted-foreground"
                >
                  <Trash2 className="h-3.5 w-3.5" aria-hidden />
                </Button>
              </div>
            ))}
          </div>
        )}
      </div>

      {/* Qué corre */}
      <div data-testid="automation-group-runs">
        <span className={labelClass}>{t("automations.form.runsTitle")}</span>
        <div className="mt-1">
          <label htmlFor="automation-workflow" className={rowLabelClass}>
            {t("automations.form.workflow")}
          </label>
          <select
            id="automation-workflow"
            className={selectClass}
            value={form.workflow}
            onChange={(e) => set("workflow", e.target.value)}
            required
          >
            <option value="">{t("automations.form.workflowPlaceholder")}</option>
            {workflows.map((w) => (
              <option key={w} value={w}>
                {w}
              </option>
            ))}
          </select>
        </div>
      </div>

      {/* Entrega */}
      <div data-testid="automation-group-delivery" className="space-y-2">
        <span className={labelClass}>{t("automations.form.deliveryTitle")}</span>
        <div className="grid grid-cols-2 gap-3">
          <div>
            <label htmlFor="automation-delivery-channel" className={rowLabelClass}>
              {t("automations.form.deliveryChannel")}
            </label>
            <select
              id="automation-delivery-channel"
              className={selectClass}
              value={form.deliveryChannel}
              onChange={(e) => set("deliveryChannel", e.target.value)}
            >
              <option value="">{t("automations.form.channelNone")}</option>
              {channelOptions}
            </select>
          </div>
          <div>
            <label htmlFor="automation-delivery-to" className={rowLabelClass}>
              {t("automations.form.deliveryTo")}
            </label>
            <input
              id="automation-delivery-to"
              className={inputClass}
              value={form.deliveryTo}
              onChange={(e) => set("deliveryTo", e.target.value)}
            />
          </div>
        </div>
        <div>
          <span className={rowLabelClass}>{t("automations.form.notify")}</span>
          <PillGroup
            ariaLabel={t("automations.form.notify")}
            value={form.notify}
            onChange={(v) => set("notify", v)}
            options={[
              { value: "always", label: t("automations.form.notifyAlways") },
              { value: "failures_only", label: t("automations.form.notifyFailuresOnly") },
              { value: "when_notable", label: t("automations.form.notifyWhenNotable") },
              { value: "never", label: t("automations.form.notifyNever") },
            ]}
          />
        </div>
        {form.notify === "when_notable" ? (
          <div>
            <span className={rowLabelClass}>{t("automations.form.silentLabels")}</span>
            <div className="flex flex-wrap gap-1">
              {form.silentLabels.map((label, j) => (
                <span
                  key={`${label}-${j}`}
                  className="inline-flex items-center gap-1 rounded-full border border-border/60 bg-muted/40 px-2 py-0.5 text-[11px]"
                >
                  {label}
                  <button
                    type="button"
                    aria-label={t("automations.form.removeSilentLabel")}
                    className="text-muted-foreground hover:text-foreground"
                    onClick={() => removeSilentLabel(j)}
                  >
                    ×
                  </button>
                </span>
              ))}
            </div>
            <div className="mt-1 flex gap-1">
              <input
                aria-label={t("automations.form.silentLabelInput")}
                className={`${inputClass} flex-1`}
                value={silentLabelDraft}
                onChange={(e) => setSilentLabelDraft(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key !== "Enter") return;
                  e.preventDefault();
                  e.stopPropagation();
                  addSilentLabel();
                }}
              />
              <Button type="button" size="sm" variant="ghost" onClick={addSilentLabel} className="text-[11px]">
                + {t("automations.form.addSilentLabel")}
              </Button>
            </div>
            <p className="mt-1 text-[10.5px] text-muted-foreground">{t("automations.form.silentLabelsHint")}</p>
          </div>
        ) : null}
      </div>

      {/* Ayuda */}
      <div data-testid="automation-group-help" className="space-y-2">
        <span className={labelClass}>{t("automations.form.helpTitle")}</span>
        <div className="grid grid-cols-2 gap-3">
          <div>
            <label htmlFor="automation-help-channel" className={rowLabelClass}>
              {t("automations.form.helpChannel")}
            </label>
            <select
              id="automation-help-channel"
              className={selectClass}
              value={form.helpChannel}
              onChange={(e) => set("helpChannel", e.target.value)}
            >
              <option value="">{t("automations.form.channelNone")}</option>
              {channelOptions}
            </select>
          </div>
          <div>
            <label htmlFor="automation-help-to" className={rowLabelClass}>
              {t("automations.form.helpTo")}
            </label>
            <input
              id="automation-help-to"
              className={inputClass}
              value={form.helpTo}
              onChange={(e) => set("helpTo", e.target.value)}
            />
          </div>
        </div>
        <p className="text-[11px] text-muted-foreground">{t("automations.form.helpHint")}</p>
      </div>

      {/* Vida */}
      <div data-testid="automation-group-life" className="space-y-3">
        <span className={labelClass}>{t("automations.form.lifeTitle")}</span>
        <div className="flex items-start gap-2">
          <input
            id="automation-life-enabled"
            type="checkbox"
            checked={form.lifeEnabled}
            onChange={(e) => set("lifeEnabled", e.target.checked)}
            className="mt-0.5 h-3.5 w-3.5 rounded border-border accent-primary"
          />
          <label htmlFor="automation-life-enabled" className="text-[12px] font-medium text-foreground/80">
            {t("automations.form.lifeEnabledLabel")}
          </label>
        </div>
        {form.lifeEnabled ? (
          <>
            <div>
              <label htmlFor="automation-intent" className={labelClass}>
                {t("automations.form.intent")}
              </label>
              <Textarea
                id="automation-intent"
                className="min-h-[64px] resize-y"
                value={form.intent}
                onChange={(e) => set("intent", e.target.value)}
                required
              />
            </div>
            <div>
              <span className={rowLabelClass}>{t("automations.form.achievedWhen")}</span>
              <PillGroup
                ariaLabel={t("automations.form.achievedWhen")}
                value={form.achievedWhenKind}
                onChange={(v) => set("achievedWhenKind", v)}
                options={[
                  { value: "any_completed", label: t("automations.form.achievedWhenAnyCompleted") },
                  { value: "label", label: t("automations.form.achievedWhenLabelOption") },
                ]}
              />
              {form.achievedWhenKind === "label" ? (
                <div className="mt-2 max-w-[220px]">
                  <label htmlFor="automation-achieved-label" className={rowLabelClass}>
                    {t("automations.form.achievedLabel")}
                  </label>
                  <input
                    id="automation-achieved-label"
                    className={inputClass}
                    placeholder={t("automations.form.achievedWhenLabelPlaceholder")}
                    value={form.achievedWhenLabel}
                    onChange={(e) => set("achievedWhenLabel", e.target.value)}
                    required
                  />
                </div>
              ) : null}
            </div>
            <div className="grid grid-cols-2 gap-3">
              <div>
                <label htmlFor="automation-max-attempts" className={rowLabelClass}>
                  {t("automations.form.maxAttempts")}
                </label>
                <input
                  id="automation-max-attempts"
                  type="number"
                  min="1"
                  className={inputClass}
                  value={form.maxAttempts}
                  onChange={(e) => set("maxAttempts", e.target.value)}
                />
                <p className="mt-1 text-[10.5px] text-muted-foreground">{t("automations.form.maxAttemptsHint")}</p>
              </div>
            </div>
            <div>
              <span className={rowLabelClass}>{t("automations.form.onStuck")}</span>
              <PillGroup
                ariaLabel={t("automations.form.onStuck")}
                value={form.onStuck}
                onChange={(v) => set("onStuck", v)}
                options={[
                  { value: "escalate_pause", label: t("automations.form.onStuckEscalatePause") },
                  { value: "notify", label: t("automations.form.onStuckNotify") },
                  { value: "keep", label: t("automations.form.onStuckKeep") },
                ]}
              />
            </div>
            <p className="text-[11.5px] text-muted-foreground">{t("automations.form.singleCaseHint")}</p>
          </>
        ) : null}
      </div>

      {/* Actions */}
      <div className="flex items-center justify-between gap-2 pt-1">
        <div>
          {editAutomation ? (
            <Button
              type="button"
              size="sm"
              variant="ghost"
              disabled={saving || deleting}
              onClick={() => setConfirmingDelete(true)}
              className="gap-1.5 text-destructive hover:text-destructive"
            >
              {deleting ? (
                <Loader2 className="h-3.5 w-3.5 animate-spin" aria-hidden />
              ) : (
                <Trash2 className="h-3.5 w-3.5" aria-hidden />
              )}
              {t("automations.form.delete")}
            </Button>
          ) : null}
        </div>
        <div className="flex items-center gap-2">
          <Button type="button" size="sm" variant="ghost" onClick={onCancel} disabled={saving || deleting}>
            {t("automations.form.cancel")}
          </Button>
          <Button
            type="button"
            size="sm"
            variant="outline"
            disabled={saving || deleting}
            onClick={() => {
              enabledOnSubmitRef.current = false;
              formRef.current?.requestSubmit();
            }}
          >
            {saving ? <Loader2 className="mr-1.5 h-3.5 w-3.5 animate-spin" aria-hidden /> : null}
            {t("automations.form.savePaused")}
          </Button>
          <Button
            type="submit"
            size="sm"
            disabled={saving || deleting}
            onClick={() => {
              enabledOnSubmitRef.current = true;
            }}
          >
            {saving ? <Loader2 className="mr-1.5 h-3.5 w-3.5 animate-spin" aria-hidden /> : null}
            {t("automations.form.saveEnabled")}
          </Button>
        </div>
      </div>

      <DeleteConfirm
        open={confirmingDelete}
        title={editAutomation?.name ?? ""}
        titleKey="automations.form.deleteTitle"
        onCancel={() => setConfirmingDelete(false)}
        onConfirm={() => void handleDelete()}
      />
    </form>
  );
}

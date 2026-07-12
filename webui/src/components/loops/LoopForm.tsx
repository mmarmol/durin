import { useEffect, useRef, useState } from "react";
import { Check, Copy, Loader2, Plus, Trash2 } from "lucide-react";
import { useTranslation } from "react-i18next";

import { Button } from "@/components/ui/button";
import { Textarea } from "@/components/ui/textarea";
import { cn } from "@/lib/utils";
import {
  ApiError,
  getHooksSecret,
  listWorkflows,
  saveLoop,
  type LoopCheck,
  type LoopDef,
  type LoopTrigger,
} from "@/lib/api";

const CHANNEL_KINDS = ["email", "telegram", "slack", "discord", "whatsapp"] as const;
type ChannelKind = (typeof CHANNEL_KINDS)[number];

interface TriggerRow {
  // Stable identity for this row, independent of its position in the array.
  // removeTrigger() shifts array indices, so any state keyed by index (e.g.
  // revealedSecretRows) would desync after a removal — key by rowId instead.
  rowId: string;
  source: "cron" | "channel" | "webhook";
  scheduleKind: "cron" | "every";
  expr: string;
  tz: string;
  everySeconds: string;
  channel: ChannelKind;
  fromContains: string;
  subjectContains: string;
  senderContains: string;
  textContains: string;
  semantic: string;
  match: "wake_or_new" | "always_new";
  correlate: string;
  hook: string;
}

interface CheckRow {
  kind: "script" | "assertion";
  required: boolean;
  command: string;
  text: string;
}

interface FormState {
  name: string;
  workflow: string;
  intent: string;
  checks: CheckRow[];
  checksSufficient: boolean;
  triggers: TriggerRow[];
  concurrency: "single" | "parallel";
  stuckAfter: string;
  operatorChannel: string;
  operatorTo: string;
}

// Monotonic counter for TriggerRow.rowId — unique within a form session,
// which is all that's needed since rows never persist across mounts.
let rowIdCounter = 0;
const nextRowId = () => `trigger-${rowIdCounter++}`;

const EMPTY_TRIGGER: Omit<TriggerRow, "rowId"> = {
  source: "cron",
  scheduleKind: "cron",
  expr: "",
  tz: "",
  everySeconds: "",
  channel: "email",
  fromContains: "",
  subjectContains: "",
  senderContains: "",
  textContains: "",
  semantic: "",
  match: "wake_or_new",
  correlate: "",
  hook: "",
};
const EMPTY_CHECK: CheckRow = { kind: "script", required: true, command: "", text: "" };

const EMPTY_FORM: FormState = {
  name: "",
  workflow: "",
  intent: "",
  checks: [],
  checksSufficient: false,
  triggers: [],
  concurrency: "single",
  stuckAfter: "3",
  operatorChannel: "",
  operatorTo: "",
};

function defToForm(def: LoopDef): FormState {
  return {
    name: def.name,
    workflow: def.workflow,
    intent: def.goal.intent,
    checks: def.goal.checks.map((c) => ({
      kind: c.kind,
      required: c.required,
      command: c.command ?? "",
      text: c.text ?? "",
    })),
    checksSufficient: def.goal.checks_sufficient ?? false,
    // The schedule-kind select only offers "cron" and "every". A one-shot
    // "at" trigger (not creatable here) falls back to "cron" so the row
    // still renders instead of crashing.
    triggers: def.triggers.map((trig) => {
      if (trig.source === "channel") {
        return {
          ...EMPTY_TRIGGER,
          rowId: nextRowId(),
          source: "channel",
          channel: trig.channel,
          fromContains: trig.filters.from_contains ?? "",
          subjectContains: trig.filters.subject_contains ?? "",
          senderContains: trig.filters.sender_contains ?? "",
          textContains: trig.filters.text_contains ?? "",
          semantic: trig.semantic ?? "",
          match: trig.match,
          correlate: trig.correlate ?? "",
        };
      }
      if (trig.source === "webhook") {
        return {
          ...EMPTY_TRIGGER,
          rowId: nextRowId(),
          source: "webhook",
          hook: trig.hook,
          semantic: trig.semantic ?? "",
          correlate: trig.correlate ?? "",
        };
      }
      return {
        ...EMPTY_TRIGGER,
        rowId: nextRowId(),
        source: "cron",
        scheduleKind: trig.schedule.kind === "every" ? "every" : "cron",
        expr: trig.schedule.expr ?? "",
        tz: trig.schedule.tz ?? "",
        everySeconds: trig.schedule.every_ms != null ? String(trig.schedule.every_ms / 1000) : "",
      };
    }),
    concurrency: def.concurrency,
    stuckAfter: String(def.stuck_after),
    operatorChannel: def.operator_channel ?? "",
    operatorTo: def.operator_to ?? "",
  };
}

function formToDef(form: FormState, enabled: boolean): LoopDef {
  const checks: LoopCheck[] = form.checks.map((c) =>
    c.kind === "script"
      ? { kind: "script", required: c.required, command: c.command }
      : { kind: "assertion", required: c.required, text: c.text },
  );
  const triggers: LoopTrigger[] = form.triggers.map((row): LoopTrigger => {
    if (row.source === "channel") {
      const filters: {
        from_contains?: string;
        subject_contains?: string;
        sender_contains?: string;
        text_contains?: string;
      } = {};
      // from/subject inputs are only shown in the UI for email, but the
      // backend accepts them on any channel — an out-of-band definition
      // (API/tool-authored) may already carry them on a non-email row, and
      // re-saving from the webui must not silently drop them.
      if (row.fromContains.trim()) filters.from_contains = row.fromContains.trim();
      if (row.subjectContains.trim()) filters.subject_contains = row.subjectContains.trim();
      if (row.senderContains.trim()) filters.sender_contains = row.senderContains.trim();
      if (row.textContains.trim()) filters.text_contains = row.textContains.trim();
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
    return {
      source: "cron",
      schedule:
        row.scheduleKind === "cron"
          ? { kind: "cron", expr: row.expr, ...(row.tz ? { tz: row.tz } : {}) }
          : { kind: "every", every_ms: Number(row.everySeconds) * 1000 },
    };
  });
  return {
    name: form.name.trim(),
    enabled,
    workflow: form.workflow,
    goal: {
      intent: form.intent,
      checks,
      ...(form.checksSufficient ? { checks_sufficient: true } : {}),
    },
    triggers,
    concurrency: form.concurrency,
    stuck_after: Math.max(1, Number(form.stuckAfter) || 1),
    operator_channel: form.operatorChannel.trim() || null,
    operator_to: form.operatorTo.trim() || null,
  };
}

/** Collapsible create/edit form for a loop definition (the "card"). */
export function LoopForm({
  token,
  editLoop,
  onDone,
  onCancel,
}: {
  token: string;
  editLoop: LoopDef | null;
  onDone: () => void;
  onCancel: () => void;
}) {
  const { t } = useTranslation();
  const [form, setForm] = useState<FormState>(editLoop ? defToForm(editLoop) : EMPTY_FORM);
  const [workflows, setWorkflows] = useState<string[]>([]);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // Which submit button was clicked (Save & enable vs Save as paused) — set
  // by the button's own onClick, which fires before the form's submit event.
  const enabledOnSubmitRef = useRef(true);
  const formRef = useRef<HTMLFormElement>(null);

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

  const set = <K extends keyof FormState>(key: K, value: FormState[K]) =>
    setForm((f) => ({ ...f, [key]: value }));

  const setTrigger = <K extends keyof TriggerRow>(i: number, key: K, value: TriggerRow[K]) =>
    setForm((f) => ({
      ...f,
      triggers: f.triggers.map((row, idx) => (idx === i ? { ...row, [key]: value } : row)),
    }));

  const setCheck = <K extends keyof CheckRow>(i: number, key: K, value: CheckRow[K]) =>
    setForm((f) => ({
      ...f,
      checks: f.checks.map((row, idx) => (idx === i ? { ...row, [key]: value } : row)),
    }));

  const addTrigger = () =>
    setForm((f) => ({ ...f, triggers: [...f.triggers, { ...EMPTY_TRIGGER, rowId: nextRowId() }] }));
  const removeTrigger = (i: number) =>
    setForm((f) => ({ ...f, triggers: f.triggers.filter((_, idx) => idx !== i) }));

  const addCheck = () => setForm((f) => ({ ...f, checks: [...f.checks, { ...EMPTY_CHECK }] }));
  const removeCheck = (i: number) =>
    setForm((f) => ({ ...f, checks: f.checks.filter((_, idx) => idx !== i) }));

  const showSecret = async (rowId: string) => {
    setRevealedSecretRows((prev) => new Set(prev).add(rowId));
    if (hooksSecret !== null) return;
    setSecretLoading(true);
    try {
      const res = await getHooksSecret(token);
      setHooksSecret(res.secret);
    } catch {
      // leave hooksSecret null — the row shows nothing further to click; the
      // "Show secret" affordance already reflects nothing was revealed.
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

  // Native "press Enter to submit" is unreliable here: the form's default
  // button is nested a few DOM levels down inside the actions row, and the
  // browser's implicit-submission default action doesn't fire from every
  // single-line field in that layout. Drive it explicitly instead, so Enter
  // behaves the same regardless of where the input sits in the tree.
  // Textareas (the Intention field) are excluded so Enter still inserts a
  // newline there instead of submitting.
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
      await saveLoop(token, formToDef(form, enabledOnSubmitRef.current));
      onDone();
    } catch (e) {
      setError(
        e instanceof ApiError
          ? e.detail
            ? `HTTP ${e.status}: ${e.detail}`
            : `HTTP ${e.status}`
          : (e as Error).message,
      );
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

  return (
    <form
      ref={formRef}
      onSubmit={(e) => void handleSubmit(e)}
      onKeyDown={handleFormKeyDown}
      className="space-y-5 px-5 py-4"
    >
      {/* Name */}
      <div>
        <label htmlFor="loop-name" className={labelClass}>
          {t("loops.form.name")}
        </label>
        <input
          id="loop-name"
          className={inputClass}
          value={form.name}
          onChange={(e) => set("name", e.target.value)}
          readOnly={!!editLoop}
          required
          autoComplete="off"
        />
      </div>

      {/* Triggers */}
      <div>
        <div className="mb-1 flex items-center justify-between">
          <span className={labelClass}>{t("loops.form.triggersTitle")}</span>
          <Button
            type="button"
            size="sm"
            variant="ghost"
            onClick={addTrigger}
            className="h-6 gap-1 px-2 text-[11px]"
          >
            <Plus className="h-3 w-3" aria-hidden /> {t("loops.form.addTrigger")}
          </Button>
        </div>
        {form.triggers.length === 0 ? (
          <p className="text-[11px] text-muted-foreground">{t("loops.form.noTriggersHint")}</p>
        ) : (
          <div className="space-y-2">
            {form.triggers.map((row, i) => (
              <div key={row.rowId} className="flex flex-wrap items-end gap-2 rounded-md border border-border/40 p-2">
                <div className="w-36">
                  <label htmlFor={`loop-trigger-source-${i}`} className={rowLabelClass}>
                    {t("loops.form.source")}
                  </label>
                  <select
                    id={`loop-trigger-source-${i}`}
                    className={selectClass}
                    value={row.source}
                    onChange={(e) => setTrigger(i, "source", e.target.value as TriggerRow["source"])}
                  >
                    <option value="cron">{t("loops.form.sourceCron")}</option>
                    <option value="channel">{t("loops.form.sourceChannel")}</option>
                    <option value="webhook">{t("loops.form.sourceWebhook")}</option>
                  </select>
                </div>
                {row.source === "channel" ? (
                  <div className="w-32">
                    <label htmlFor={`loop-trigger-channel-${i}`} className={rowLabelClass}>
                      {t("loops.form.channel")}
                    </label>
                    <select
                      id={`loop-trigger-channel-${i}`}
                      className={selectClass}
                      value={row.channel}
                      onChange={(e) => setTrigger(i, "channel", e.target.value as ChannelKind)}
                    >
                      {CHANNEL_KINDS.map((c) => (
                        <option key={c} value={c}>
                          {t(`loops.form.channel_${c}`)}
                        </option>
                      ))}
                    </select>
                  </div>
                ) : null}
                {row.source === "cron" ? (
                  <>
                    <div className="w-28">
                      <label htmlFor={`loop-trigger-kind-${i}`} className={rowLabelClass}>
                        {t("loops.form.scheduleKind")}
                      </label>
                      <select
                        id={`loop-trigger-kind-${i}`}
                        className={selectClass}
                        value={row.scheduleKind}
                        onChange={(e) => setTrigger(i, "scheduleKind", e.target.value as TriggerRow["scheduleKind"])}
                      >
                        <option value="cron">{t("loops.form.scheduleKindCron")}</option>
                        <option value="every">{t("loops.form.scheduleKindEvery")}</option>
                      </select>
                    </div>
                    {row.scheduleKind === "cron" ? (
                      <>
                        <div className="min-w-[140px] flex-1">
                          <label htmlFor={`loop-trigger-expr-${i}`} className={rowLabelClass}>
                            {t("loops.form.exprLabel")}
                          </label>
                          <input
                            id={`loop-trigger-expr-${i}`}
                            className={inputClass}
                            placeholder="0 9 * * *"
                            value={row.expr}
                            onChange={(e) => setTrigger(i, "expr", e.target.value)}
                            required
                          />
                        </div>
                        <div className="w-28">
                          <label htmlFor={`loop-trigger-tz-${i}`} className={rowLabelClass}>
                            {t("loops.form.tz")}
                          </label>
                          <input
                            id={`loop-trigger-tz-${i}`}
                            className={inputClass}
                            placeholder="UTC"
                            value={row.tz}
                            onChange={(e) => setTrigger(i, "tz", e.target.value)}
                          />
                        </div>
                      </>
                    ) : (
                      <div className="min-w-[140px] flex-1">
                        <label htmlFor={`loop-trigger-interval-${i}`} className={rowLabelClass}>
                          {t("loops.form.intervalLabel")}
                        </label>
                        <input
                          id={`loop-trigger-interval-${i}`}
                          type="number"
                          min="1"
                          className={inputClass}
                          placeholder="3600"
                          value={row.everySeconds}
                          onChange={(e) => setTrigger(i, "everySeconds", e.target.value)}
                          required
                        />
                      </div>
                    )}
                  </>
                ) : row.source === "channel" ? (
                  <>
                    {row.channel === "email" ? (
                      <>
                        <div className="min-w-[140px] flex-1">
                          <label htmlFor={`loop-trigger-from-${i}`} className={rowLabelClass}>
                            {t("loops.form.fromContains")}
                          </label>
                          <input
                            id={`loop-trigger-from-${i}`}
                            className={inputClass}
                            value={row.fromContains}
                            onChange={(e) => setTrigger(i, "fromContains", e.target.value)}
                          />
                        </div>
                        <div className="min-w-[140px] flex-1">
                          <label htmlFor={`loop-trigger-subject-${i}`} className={rowLabelClass}>
                            {t("loops.form.subjectContains")}
                          </label>
                          <input
                            id={`loop-trigger-subject-${i}`}
                            className={inputClass}
                            value={row.subjectContains}
                            onChange={(e) => setTrigger(i, "subjectContains", e.target.value)}
                          />
                        </div>
                      </>
                    ) : null}
                    <div className="min-w-[140px] flex-1">
                      <label htmlFor={`loop-trigger-sender-${i}`} className={rowLabelClass}>
                        {t("loops.form.senderContains")}
                      </label>
                      <input
                        id={`loop-trigger-sender-${i}`}
                        className={inputClass}
                        value={row.senderContains}
                        onChange={(e) => setTrigger(i, "senderContains", e.target.value)}
                      />
                    </div>
                    <div className="min-w-[140px] flex-1">
                      <label htmlFor={`loop-trigger-text-${i}`} className={rowLabelClass}>
                        {t("loops.form.textContains")}
                      </label>
                      <input
                        id={`loop-trigger-text-${i}`}
                        className={inputClass}
                        value={row.textContains}
                        onChange={(e) => setTrigger(i, "textContains", e.target.value)}
                      />
                    </div>
                    <div className="min-w-[200px] flex-[2]">
                      <label htmlFor={`loop-trigger-semantic-${i}`} className={rowLabelClass}>
                        {t("loops.form.semantic")}
                      </label>
                      <input
                        id={`loop-trigger-semantic-${i}`}
                        className={inputClass}
                        value={row.semantic}
                        onChange={(e) => setTrigger(i, "semantic", e.target.value)}
                      />
                      <p className="mt-1 text-[10.5px] text-muted-foreground">{t("loops.form.semanticHint")}</p>
                    </div>
                    <div className="min-w-[200px] flex-1">
                      <label htmlFor={`loop-trigger-correlate-${i}`} className={rowLabelClass}>
                        {t("loops.form.correlate")}
                      </label>
                      <input
                        id={`loop-trigger-correlate-${i}`}
                        className={inputClass}
                        value={row.correlate}
                        onChange={(e) => setTrigger(i, "correlate", e.target.value)}
                      />
                      <p className="mt-1 text-[10.5px] text-muted-foreground">{t("loops.form.correlateHint")}</p>
                    </div>
                    <div className="w-56">
                      <label htmlFor={`loop-trigger-match-${i}`} className={rowLabelClass}>
                        {t("loops.form.match")}
                      </label>
                      <select
                        id={`loop-trigger-match-${i}`}
                        className={selectClass}
                        value={row.match}
                        onChange={(e) => setTrigger(i, "match", e.target.value as TriggerRow["match"])}
                      >
                        <option value="wake_or_new">{t("loops.form.matchWakeOrNew")}</option>
                        <option value="always_new">{t("loops.form.matchAlwaysNew")}</option>
                      </select>
                    </div>
                  </>
                ) : (
                  <>
                    <div className="min-w-[140px] flex-1">
                      <label htmlFor={`loop-trigger-hook-${i}`} className={rowLabelClass}>
                        {t("loops.form.hookName")}
                      </label>
                      <input
                        id={`loop-trigger-hook-${i}`}
                        className={inputClass}
                        value={row.hook}
                        onChange={(e) => setTrigger(i, "hook", e.target.value)}
                        required
                        autoComplete="off"
                      />
                    </div>
                    <div className="min-w-[200px] flex-1">
                      <label htmlFor={`loop-trigger-hook-url-${i}`} className={rowLabelClass}>
                        {t("loops.form.hookUrlLabel")}
                      </label>
                      <input
                        id={`loop-trigger-hook-url-${i}`}
                        className={cn(inputClass, "font-mono")}
                        value={`/api/v1/hooks/${row.hook}`}
                        readOnly
                      />
                    </div>
                    <div className="min-w-[200px] flex-[2]">
                      <label htmlFor={`loop-trigger-webhook-semantic-${i}`} className={rowLabelClass}>
                        {t("loops.form.semantic")}
                      </label>
                      <input
                        id={`loop-trigger-webhook-semantic-${i}`}
                        className={inputClass}
                        value={row.semantic}
                        onChange={(e) => setTrigger(i, "semantic", e.target.value)}
                      />
                      <p className="mt-1 text-[10.5px] text-muted-foreground">{t("loops.form.semanticHint")}</p>
                    </div>
                    <div className="min-w-[200px] flex-1">
                      <label htmlFor={`loop-trigger-webhook-correlate-${i}`} className={rowLabelClass}>
                        {t("loops.form.correlate")}
                      </label>
                      <input
                        id={`loop-trigger-webhook-correlate-${i}`}
                        className={inputClass}
                        value={row.correlate}
                        onChange={(e) => setTrigger(i, "correlate", e.target.value)}
                      />
                      <p className="mt-1 text-[10.5px] text-muted-foreground">{t("loops.form.correlateHint")}</p>
                    </div>
                    <div className="min-w-[160px]">
                      <span className={rowLabelClass}>{t("loops.form.hookSecret")}</span>
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
                              aria-label={t("loops.form.copySecret")}
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
                          {t("loops.form.showSecret")}
                        </Button>
                      )}
                    </div>
                  </>
                )}
                <Button
                  type="button"
                  size="sm"
                  variant="ghost"
                  onClick={() => removeTrigger(i)}
                  aria-label={t("loops.form.removeTrigger")}
                  className="h-8 w-8 shrink-0 p-0 text-muted-foreground"
                >
                  <Trash2 className="h-3.5 w-3.5" aria-hidden />
                </Button>
              </div>
            ))}
          </div>
        )}
      </div>

      {/* Goal */}
      <div className="space-y-3">
        <span className={labelClass}>{t("loops.form.goalTitle")}</span>
        <div>
          <label htmlFor="loop-intent" className={labelClass}>
            {t("loops.form.intent")}
          </label>
          <Textarea
            id="loop-intent"
            className="min-h-[64px] resize-y"
            value={form.intent}
            onChange={(e) => set("intent", e.target.value)}
            required
          />
        </div>
        <div>
          <div className="mb-1 flex items-center justify-between">
            <span className={labelClass}>{t("loops.form.checksTitle")}</span>
            <Button
              type="button"
              size="sm"
              variant="ghost"
              onClick={addCheck}
              className="h-6 gap-1 px-2 text-[11px]"
            >
              <Plus className="h-3 w-3" aria-hidden /> {t("loops.form.addCheck")}
            </Button>
          </div>
          <div className="space-y-2">
            {form.checks.map((row, i) => (
              <div key={i} className="flex flex-wrap items-end gap-2 rounded-md border border-border/40 p-2">
                <div className="w-28">
                  <label htmlFor={`loop-check-kind-${i}`} className={rowLabelClass}>
                    {t("loops.form.checkKind")}
                  </label>
                  <select
                    id={`loop-check-kind-${i}`}
                    className={selectClass}
                    value={row.kind}
                    onChange={(e) => setCheck(i, "kind", e.target.value as CheckRow["kind"])}
                  >
                    <option value="script">{t("loops.form.checkKindScript")}</option>
                    <option value="assertion">{t("loops.form.checkKindAssertion")}</option>
                  </select>
                </div>
                <div className="min-w-[160px] flex-1">
                  <label htmlFor={`loop-check-value-${i}`} className={rowLabelClass}>
                    {row.kind === "script" ? t("loops.form.checkCommand") : t("loops.form.checkText")}
                  </label>
                  {row.kind === "script" ? (
                    <input
                      id={`loop-check-value-${i}`}
                      className={inputClass}
                      placeholder="curl -f https://…"
                      value={row.command}
                      onChange={(e) => setCheck(i, "command", e.target.value)}
                      required
                    />
                  ) : (
                    <input
                      id={`loop-check-value-${i}`}
                      className={inputClass}
                      placeholder={t("loops.form.checkText")}
                      value={row.text}
                      onChange={(e) => setCheck(i, "text", e.target.value)}
                      required
                    />
                  )}
                </div>
                <div className="flex items-center gap-1.5 pb-1.5">
                  <input
                    id={`loop-check-required-${i}`}
                    type="checkbox"
                    checked={row.required}
                    onChange={(e) => setCheck(i, "required", e.target.checked)}
                    className="h-3.5 w-3.5 rounded border-border accent-primary"
                  />
                  <label htmlFor={`loop-check-required-${i}`} className="text-[11px] text-foreground/70">
                    {t("loops.form.checkRequired")}
                  </label>
                </div>
                <Button
                  type="button"
                  size="sm"
                  variant="ghost"
                  onClick={() => removeCheck(i)}
                  aria-label={t("loops.form.removeCheck")}
                  className="h-8 w-8 shrink-0 p-0 text-muted-foreground"
                >
                  <Trash2 className="h-3.5 w-3.5" aria-hidden />
                </Button>
              </div>
            ))}
          </div>
        </div>
        <div className="flex items-start gap-2">
          <input
            id="loop-checks-sufficient"
            type="checkbox"
            checked={form.checksSufficient}
            onChange={(e) => set("checksSufficient", e.target.checked)}
            className="mt-0.5 h-3.5 w-3.5 rounded border-border accent-primary"
          />
          <div>
            <label htmlFor="loop-checks-sufficient" className="text-[12px] font-medium text-foreground/80">
              {t("loops.form.checksSufficient")}
            </label>
            <p className="text-[11px] text-muted-foreground">{t("loops.form.checksSufficientHint")}</p>
          </div>
        </div>
      </div>

      {/* Body */}
      <div className="grid grid-cols-2 gap-3">
        <div>
          <label htmlFor="loop-workflow" className={labelClass}>
            {t("loops.form.workflow")}
          </label>
          <select
            id="loop-workflow"
            className={selectClass}
            value={form.workflow}
            onChange={(e) => set("workflow", e.target.value)}
            required
          >
            <option value="">{t("loops.form.workflowPlaceholder")}</option>
            {workflows.map((w) => (
              <option key={w} value={w}>
                {w}
              </option>
            ))}
          </select>
        </div>
        <div>
          <label htmlFor="loop-stuck-after" className={labelClass}>
            {t("loops.form.stuckAfter")}
          </label>
          <input
            id="loop-stuck-after"
            type="number"
            min="1"
            className={inputClass}
            value={form.stuckAfter}
            onChange={(e) => set("stuckAfter", e.target.value)}
            required
          />
          <p className="mt-1 text-[11px] text-muted-foreground">{t("loops.form.stuckAfterHint")}</p>
        </div>
      </div>

      {/* Concurrency */}
      <div>
        <label htmlFor="loop-concurrency" className={labelClass}>
          {t("loops.form.concurrency")}
        </label>
        <select
          id="loop-concurrency"
          className={cn(selectClass, "w-auto")}
          value={form.concurrency}
          onChange={(e) => set("concurrency", e.target.value as FormState["concurrency"])}
        >
          <option value="single">{t("loops.form.concurrencySingle")}</option>
          <option value="parallel">{t("loops.form.concurrencyParallel")}</option>
        </select>
      </div>

      {/* Operator */}
      <div>
        <span className={labelClass}>{t("loops.form.operatorTitle")}</span>
        <div className="grid grid-cols-2 gap-3">
          <div>
            <label htmlFor="loop-operator-channel" className={rowLabelClass}>
              {t("loops.form.operatorChannel")}
            </label>
            <input
              id="loop-operator-channel"
              className={inputClass}
              value={form.operatorChannel}
              onChange={(e) => set("operatorChannel", e.target.value)}
              placeholder={t("loops.form.operatorChannelPlaceholder")}
            />
          </div>
          <div>
            <label htmlFor="loop-operator-to" className={rowLabelClass}>
              {t("loops.form.operatorTo")}
            </label>
            <input
              id="loop-operator-to"
              className={inputClass}
              value={form.operatorTo}
              onChange={(e) => set("operatorTo", e.target.value)}
              placeholder={t("loops.form.operatorToPlaceholder")}
            />
          </div>
        </div>
        <p className="mt-1 text-[11px] text-muted-foreground">{t("loops.form.operatorToHint")}</p>
      </div>

      {/* Error */}
      {error ? <p className="text-[12px] text-destructive">{error}</p> : null}

      {/* Actions */}
      <div className="flex items-center justify-end gap-2 pt-1">
        <Button type="button" size="sm" variant="ghost" onClick={onCancel} disabled={saving}>
          {t("loops.form.cancel")}
        </Button>
        <Button
          type="button"
          size="sm"
          variant="outline"
          disabled={saving}
          onClick={() => {
            enabledOnSubmitRef.current = false;
            formRef.current?.requestSubmit();
          }}
        >
          {saving ? <Loader2 className="mr-1.5 h-3.5 w-3.5 animate-spin" aria-hidden /> : null}
          {t("loops.form.savePaused")}
        </Button>
        <Button
          type="submit"
          size="sm"
          disabled={saving}
          onClick={() => {
            enabledOnSubmitRef.current = true;
          }}
        >
          {saving ? <Loader2 className="mr-1.5 h-3.5 w-3.5 animate-spin" aria-hidden /> : null}
          {t("loops.form.saveEnabled")}
        </Button>
      </div>
    </form>
  );
}

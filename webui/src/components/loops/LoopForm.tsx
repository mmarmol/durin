import { useEffect, useRef, useState } from "react";
import { Loader2, Plus, Trash2 } from "lucide-react";
import { useTranslation } from "react-i18next";

import { Button } from "@/components/ui/button";
import { Textarea } from "@/components/ui/textarea";
import { cn } from "@/lib/utils";
import {
  ApiError,
  listWorkflows,
  saveLoop,
  type LoopCheck,
  type LoopDef,
  type LoopTrigger,
} from "@/lib/api";

interface TriggerRow {
  scheduleKind: "cron" | "every";
  expr: string;
  tz: string;
  everySeconds: string;
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

const EMPTY_TRIGGER: TriggerRow = { scheduleKind: "cron", expr: "", tz: "", everySeconds: "" };
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
    // The select only offers "cron" and "every". A one-shot "at" trigger
    // (not creatable here) falls back to "cron" so the row still renders
    // instead of crashing.
    triggers: def.triggers.map((trig) => ({
      scheduleKind: trig.schedule.kind === "every" ? "every" : "cron",
      expr: trig.schedule.expr ?? "",
      tz: trig.schedule.tz ?? "",
      everySeconds: trig.schedule.every_ms != null ? String(trig.schedule.every_ms / 1000) : "",
    })),
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
  const triggers: LoopTrigger[] = form.triggers.map((row) => ({
    source: "cron",
    schedule:
      row.scheduleKind === "cron"
        ? { kind: "cron", expr: row.expr, ...(row.tz ? { tz: row.tz } : {}) }
        : { kind: "every", every_ms: Number(row.everySeconds) * 1000 },
  }));
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

  const addTrigger = () => setForm((f) => ({ ...f, triggers: [...f.triggers, { ...EMPTY_TRIGGER }] }));
  const removeTrigger = (i: number) =>
    setForm((f) => ({ ...f, triggers: f.triggers.filter((_, idx) => idx !== i) }));

  const addCheck = () => setForm((f) => ({ ...f, checks: [...f.checks, { ...EMPTY_CHECK }] }));
  const removeCheck = (i: number) =>
    setForm((f) => ({ ...f, checks: f.checks.filter((_, idx) => idx !== i) }));

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
    <form ref={formRef} onSubmit={(e) => void handleSubmit(e)} className="space-y-5 px-5 py-4">
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
              <div key={i} className="flex flex-wrap items-end gap-2 rounded-md border border-border/40 p-2">
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

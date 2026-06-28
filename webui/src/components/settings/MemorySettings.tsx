import { useCallback, useEffect, useMemo, useState } from "react";
import { Loader2 } from "lucide-react";
import { useTranslation } from "react-i18next";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import {
  getConfig,
  getExtraStatus,
  setConfigValue,
  testCrossEncoderModel,
  type CrossEncoderTestResult,
  type ExtraStatus,
} from "@/lib/api";
import { ExtraInstallPrompt } from "./ExtraInstallPrompt";
import {
  SettingsGroup,
  SettingsRow,
  SettingsSectionTitle,
} from "./primitives";

// Suggested cross-encoder models surfaced as datalist options.
// Audit B12 (2026-05-28): the input is NOT restricted to this list —
// any sentence-transformers compatible id (HuggingFace handle, local
// path, etc.) is accepted, with validation happening live via the
// Test button.
// H30 (2026-05-30): bge-reranker-base promoted to default (MIT, no
// trust_remote_code required, no transformers 5.x compat issues).
// jina-reranker-v2 dropped from suggestions (CC-BY-NC + custom-code
// requirement that breaks on fresh installs).
const CROSS_ENCODER_SUGGESTED_MODELS: ReadonlyArray<string> = [
  "BAAI/bge-reranker-base",
  "BAAI/bge-reranker-v2-m3",
  "mixedbread-ai/mxbai-rerank-base-v2",
] as const;

const DEFAULT_CROSS_ENCODER_MODEL = CROSS_ENCODER_SUGGESTED_MODELS[0];

interface CrossEncoderState {
  enabled: boolean;
  model: string;
}

interface MemoryConfigShape {
  // /api/config returns the canonical snake_case shape. The
  // setConfigValue *paths* also use snake_case, normalized by the
  // backend's `_normalize_dotted_path` before writing.
  search?: {
    cross_encoder?: Partial<CrossEncoderState>;
  };
  // memory.dream — the extract/refine/skill passes' triggers (§8e).
  dream?: {
    enabled?: boolean;
    cron?: string;
    post_compaction?: boolean;
    on_session_close?: boolean;
    discover_enabled?: boolean;
    min_seconds_between_runs?: number;
    max_seconds_per_run?: number;
    always_on_token_budget?: number;
    auto_absorb?: { enabled?: boolean; confidence_threshold?: number; semantic_distance_threshold?: number };
  };
}

interface DreamState {
  enabled: boolean;
  cron: string;
  postCompaction: boolean;
  onSessionClose: boolean;
  discover: boolean;
  minSecondsBetweenRuns: number;
  maxSecondsPerRun: number;
  alwaysOnTokenBudget: number;
  autoAbsorb: boolean;
  autoAbsorbConfidence: number;
  autoAbsorbSemanticThreshold: number;
}

function readCrossEncoder(config: Record<string, unknown> | null): CrossEncoderState {
  const memory = config?.memory as MemoryConfigShape | undefined;
  const ce = memory?.search?.cross_encoder ?? {};
  return {
    enabled: typeof ce.enabled === "boolean" ? ce.enabled : false,
    model: typeof ce.model === "string" && ce.model ? ce.model : DEFAULT_CROSS_ENCODER_MODEL,
  };
}

function readDream(config: Record<string, unknown> | null): DreamState {
  const memory = config?.memory as MemoryConfigShape | undefined;
  const d = memory?.dream ?? {};
  return {
    enabled: typeof d.enabled === "boolean" ? d.enabled : true,
    cron: typeof d.cron === "string" && d.cron ? d.cron : "0 3 * * *",
    postCompaction: typeof d.post_compaction === "boolean" ? d.post_compaction : true,
    onSessionClose: typeof d.on_session_close === "boolean" ? d.on_session_close : true,
    discover: typeof d.discover_enabled === "boolean" ? d.discover_enabled : true,
    minSecondsBetweenRuns:
      typeof d.min_seconds_between_runs === "number" ? d.min_seconds_between_runs : 300,
    maxSecondsPerRun:
      typeof d.max_seconds_per_run === "number" ? d.max_seconds_per_run : 600,
    alwaysOnTokenBudget:
      typeof d.always_on_token_budget === "number" ? d.always_on_token_budget : 1500,
    autoAbsorb:
      typeof d.auto_absorb?.enabled === "boolean" ? d.auto_absorb.enabled : true,
    autoAbsorbConfidence:
      typeof d.auto_absorb?.confidence_threshold === "number" ? d.auto_absorb.confidence_threshold : 95,
    autoAbsorbSemanticThreshold:
      typeof d.auto_absorb?.semantic_distance_threshold === "number" ? d.auto_absorb.semantic_distance_threshold : 0.20,
  };
}

/** Memory settings — cross-encoder rerank, dream auto-trigger threshold,
 *  and read-only summary of the temporal decay defaults (defined in
 *  `durin/memory/decay.py::CLASS_HALF_LIFE_DEFAULTS`). */
export function MemorySettings({ token }: { token: string }) {
  const { t } = useTranslation();
  const [config, setConfig] = useState<Record<string, unknown> | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [savingPath, setSavingPath] = useState<string | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const snap = await getConfig(token);
      setConfig(snap.config as Record<string, unknown>);
    } catch {
      setError(t("settings.memory.loadError"));
    } finally {
      setLoading(false);
    }
  }, [token, t]);

  useEffect(() => {
    void load();
  }, [load]);

  const onSave = useCallback(
    async (path: string, value: unknown) => {
      setSavingPath(path);
      setError(null);
      try {
        const next = await setConfigValue(token, path, value);
        setConfig(next as Record<string, unknown>);
      } catch {
        setError(t("settings.memory.saveError", { path }));
      } finally {
        setSavingPath(null);
      }
    },
    [token, t],
  );

  const crossEncoder = useMemo(() => readCrossEncoder(config), [config]);
  const dream = useMemo(() => readDream(config), [config]);

  const [pendingExtra, setPendingExtra] = useState<
    { feature: string; status: ExtraStatus; after: () => void } | null
  >(null);
  const ensureThen = useCallback(
    async (feature: string, after: () => void) => {
      try {
        const st = await getExtraStatus(token, feature);
        if (st.present) {
          after();
          return;
        }
        setPendingExtra({ feature, status: st, after });
      } catch {
        after(); // status check failed — let the action surface its own error
      }
    },
    [token],
  );

  if (loading) {
    return (
      <div className="flex h-40 items-center justify-center text-sm text-muted-foreground">
        <Loader2 className="mr-2 h-4 w-4 animate-spin" />
        {t("settings.status.loading")}
      </div>
    );
  }

  return (
    <div className="space-y-6">
      <p className="px-1 text-[13px] leading-5 text-muted-foreground">
        {t("settings.memory.description")}
      </p>

      {error ? (
        <div className="rounded-[18px] border border-destructive/20 bg-destructive/5 px-4 py-3 text-[13px] text-destructive">
          {error}
        </div>
      ) : null}

      <section>
        <SettingsSectionTitle>{t("settings.memory.sections.rerank")}</SettingsSectionTitle>
        <SettingsGroup>
          <SettingsRow
            title={t("settings.memory.rows.crossEncoderEnabled")}
            description={t("settings.memory.help.crossEncoderEnabled")}
          >
            <Button
              size="sm"
              variant="outline"
              disabled={savingPath === "memory.search.cross_encoder.enabled"}
              onClick={() =>
                crossEncoder.enabled
                  ? void onSave("memory.search.cross_encoder.enabled", false)
                  : void ensureThen("cross_encoder", () =>
                      void onSave("memory.search.cross_encoder.enabled", true),
                    )
              }
              className="min-w-[68px] rounded-full"
            >
              {crossEncoder.enabled ? t("settings.config.on") : t("settings.config.off")}
            </Button>
          </SettingsRow>

          <SettingsRow
            title={t("settings.memory.rows.crossEncoderModel")}
            description={t("settings.memory.help.crossEncoderModel")}
          >
            <CrossEncoderModelEditor
              token={token}
              value={crossEncoder.model}
              disabled={!crossEncoder.enabled || savingPath === "memory.search.cross_encoder.model"}
              onSave={(model) => void onSave("memory.search.cross_encoder.model", model)}
              ensureThen={ensureThen}
            />
          </SettingsRow>
          {pendingExtra ? (
            <ExtraInstallPrompt
              token={token}
              feature={pendingExtra.feature}
              status={pendingExtra.status}
              onCancel={() => setPendingExtra(null)}
              onDone={(restarting) => {
                const after = pendingExtra.after;
                setPendingExtra(null);
                if (!restarting) after();
              }}
            />
          ) : null}
        </SettingsGroup>
      </section>

      <section>
        <SettingsSectionTitle>{t("settings.memory.sections.dream")}</SettingsSectionTitle>
        <p className="px-1 pb-2 text-[12px] text-muted-foreground">
          {t("settings.memory.dreamDescription")}
        </p>
        <SettingsGroup>
          <ToggleRow
            title={t("settings.memory.rows.dreamEnabled")}
            description={t("settings.memory.help.dreamEnabled")}
            value={dream.enabled}
            saving={savingPath === "memory.dream.enabled"}
            onToggle={() => void onSave("memory.dream.enabled", !dream.enabled)}
          />
          <DreamCronRow
            value={dream.cron}
            disabled={!dream.enabled}
            saving={savingPath === "memory.dream.cron"}
            onSave={(c) => void onSave("memory.dream.cron", c)}
          />
          <ToggleRow
            title={t("settings.memory.rows.dreamPostCompaction")}
            description={t("settings.memory.help.dreamPostCompaction")}
            value={dream.postCompaction}
            saving={savingPath === "memory.dream.post_compaction"}
            onToggle={() =>
              void onSave("memory.dream.post_compaction", !dream.postCompaction)
            }
          />
          <ToggleRow
            title={t("settings.memory.rows.dreamOnSessionClose")}
            description={t("settings.memory.help.dreamOnSessionClose")}
            value={dream.onSessionClose}
            saving={savingPath === "memory.dream.on_session_close"}
            onToggle={() =>
              void onSave("memory.dream.on_session_close", !dream.onSessionClose)
            }
          />
          <ToggleRow
            title={t("settings.memory.rows.dreamDiscover")}
            description={t("settings.memory.help.dreamDiscover")}
            value={dream.discover}
            saving={savingPath === "memory.dream.discover_enabled"}
            onToggle={() =>
              void onSave("memory.dream.discover_enabled", !dream.discover)
            }
          />
          <ToggleRow
            title={t("settings.memory.rows.dreamAutoAbsorb")}
            description={t("settings.memory.help.dreamAutoAbsorb")}
            value={dream.autoAbsorb}
            saving={savingPath === "memory.dream.auto_absorb.enabled"}
            onToggle={() =>
              void onSave("memory.dream.auto_absorb.enabled", !dream.autoAbsorb)
            }
          />
          <DreamNumberRow
            title={t("settings.memory.rows.dreamAbsorbConfidence")}
            description={t("settings.memory.help.dreamAbsorbConfidence")}
            value={dream.autoAbsorbConfidence}
            disabled={!dream.enabled || !dream.autoAbsorb}
            saving={savingPath === "memory.dream.auto_absorb.confidence_threshold"}
            onSave={(n) => void onSave("memory.dream.auto_absorb.confidence_threshold", n)}
          />
          <DreamDecimalRow
            title={t("settings.memory.rows.dreamAbsorbSemanticThreshold")}
            description={t("settings.memory.help.dreamAbsorbSemanticThreshold")}
            value={dream.autoAbsorbSemanticThreshold}
            disabled={!dream.enabled || !dream.autoAbsorb}
            saving={savingPath === "memory.dream.auto_absorb.semantic_distance_threshold"}
            onSave={(n) => void onSave("memory.dream.auto_absorb.semantic_distance_threshold", n)}
          />
          <DreamNumberRow
            title={t("settings.memory.rows.dreamThrottle")}
            description={t("settings.memory.help.dreamThrottle")}
            value={dream.minSecondsBetweenRuns}
            disabled={!dream.enabled}
            saving={savingPath === "memory.dream.min_seconds_between_runs"}
            onSave={(n) => void onSave("memory.dream.min_seconds_between_runs", n)}
          />
          <DreamNumberRow
            title={t("settings.memory.rows.dreamMaxSeconds")}
            description={t("settings.memory.help.dreamMaxSeconds")}
            value={dream.maxSecondsPerRun}
            disabled={!dream.enabled}
            saving={savingPath === "memory.dream.max_seconds_per_run"}
            onSave={(n) => void onSave("memory.dream.max_seconds_per_run", n)}
          />
          <DreamNumberRow
            title={t("settings.memory.rows.dreamAlwaysOnBudget")}
            description={t("settings.memory.help.dreamAlwaysOnBudget")}
            value={dream.alwaysOnTokenBudget}
            disabled={!dream.enabled}
            saving={savingPath === "memory.dream.always_on_token_budget"}
            onSave={(n) => void onSave("memory.dream.always_on_token_budget", n)}
          />
        </SettingsGroup>
      </section>
    </div>
  );
}

/** On/off toggle row — the dream trigger booleans (enabled, post_compaction,
 *  on_session_close, auto_absorb). */
function ToggleRow({
  title,
  description,
  value,
  saving,
  onToggle,
}: {
  title: string;
  description: string;
  value: boolean;
  saving: boolean;
  onToggle: () => void;
}) {
  const { t } = useTranslation();
  return (
    <SettingsRow title={title} description={description}>
      <Button
        size="sm"
        variant="outline"
        disabled={saving}
        onClick={onToggle}
        className="min-w-[68px] rounded-full"
      >
        {value ? t("settings.config.on") : t("settings.config.off")}
      </Button>
    </SettingsRow>
  );
}

/** Cron expression input for `memory.dream.cron` (the daily pass schedule). */
function DreamCronRow({
  value,
  disabled,
  saving,
  onSave,
}: {
  value: string;
  disabled: boolean;
  saving: boolean;
  onSave: (cron: string) => void;
}) {
  const { t } = useTranslation();
  const [draft, setDraft] = useState(value);
  useEffect(() => setDraft(value), [value]);

  const dirty = draft.trim() !== value && draft.trim().length > 0;
  const commit = () => {
    if (!dirty) return;
    onSave(draft.trim());
  };

  return (
    <SettingsRow
      title={t("settings.memory.rows.dreamCron")}
      description={t("settings.memory.help.dreamCron")}
    >
      <div className="flex items-center gap-2">
        <Input
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter") commit();
          }}
          placeholder="0 3 * * *"
          disabled={disabled}
          className="h-8 w-[140px] rounded-full text-[13px]"
        />
        <Button
          size="sm"
          variant="outline"
          disabled={!dirty || disabled || saving}
          onClick={commit}
          className="rounded-full"
        >
          {t("settings.config.save")}
        </Button>
      </div>
    </SettingsRow>
  );
}

/** Numeric input row for the dream tuning knobs (reactive throttle seconds,
 *  wall-clock cap seconds). Commit on Enter or Save. */
function DreamNumberRow({
  title,
  description,
  value,
  disabled,
  saving,
  onSave,
}: {
  title: string;
  description: string;
  value: number;
  disabled: boolean;
  saving: boolean;
  onSave: (n: number) => void;
}) {
  const { t } = useTranslation();
  const [draft, setDraft] = useState(String(value));
  useEffect(() => setDraft(String(value)), [value]);

  const parsed = Number(draft);
  const valid = Number.isFinite(parsed) && parsed >= 0 && Number.isInteger(parsed);
  const dirty = valid && parsed !== value;
  const commit = () => {
    if (!dirty) return;
    onSave(parsed);
  };

  return (
    <SettingsRow title={title} description={description}>
      <div className="flex items-center gap-2">
        <Input
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter") commit();
          }}
          inputMode="numeric"
          disabled={disabled}
          className="h-8 w-[110px] rounded-full text-[13px]"
        />
        <Button
          size="sm"
          variant="outline"
          disabled={!dirty || disabled || saving}
          onClick={commit}
          className="rounded-full"
        >
          {t("settings.config.save")}
        </Button>
      </div>
    </SettingsRow>
  );
}

/** Decimal input row for fractional dream tuning knobs (0–1 range). Commit on Enter or Save. */
function DreamDecimalRow({
  title,
  description,
  value,
  disabled,
  saving,
  onSave,
}: {
  title: string;
  description: string;
  value: number;
  disabled: boolean;
  saving: boolean;
  onSave: (n: number) => void;
}) {
  const { t } = useTranslation();
  const [draft, setDraft] = useState(String(value));
  useEffect(() => setDraft(String(value)), [value]);

  const parsed = Number(draft);
  const valid = Number.isFinite(parsed) && parsed >= 0 && parsed <= 1;
  const dirty = valid && parsed !== value;
  const commit = () => {
    if (!dirty) return;
    onSave(parsed);
  };

  return (
    <SettingsRow title={title} description={description}>
      <div className="flex items-center gap-2">
        <Input
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter") commit();
          }}
          inputMode="decimal"
          disabled={disabled}
          className="h-8 w-[110px] rounded-full text-[13px]"
        />
        <Button
          size="sm"
          variant="outline"
          disabled={!dirty || disabled || saving}
          onClick={commit}
          className="rounded-full"
        >
          {t("settings.config.save")}
        </Button>
      </div>
    </SettingsRow>
  );
}

/**
 * Free-form model id editor with live test (audit B12, 2026-05-28).
 *
 * Replaces the prior fixed dropdown with a text input + <datalist> so any
 * sentence-transformers compatible id can be entered (the four suggestions
 * are bundled in the install but the user is free to type a HuggingFace
 * handle, local path, etc.). The Test button validates the value live —
 * the backend loads the model and runs a trivial score, surfacing OK /
 * fail with the underlying error.
 */
function CrossEncoderModelEditor({
  token,
  value,
  disabled,
  onSave,
  ensureThen,
}: {
  token: string;
  value: string;
  disabled: boolean;
  onSave: (model: string) => void;
  ensureThen: (feature: string, after: () => void) => void;
}) {
  const { t } = useTranslation();
  const [draft, setDraft] = useState(value);
  const [testing, setTesting] = useState(false);
  const [result, setResult] = useState<CrossEncoderTestResult | null>(null);

  useEffect(() => {
    setDraft(value);
    setResult(null);
  }, [value]);

  const dirty = draft.trim() !== value && draft.trim().length > 0;

  const commit = () => {
    if (!dirty) return;
    onSave(draft.trim());
  };

  const runTest = async () => {
    setTesting(true);
    setResult(null);
    try {
      const r = await testCrossEncoderModel(token, draft.trim());
      setResult(r);
    } catch (err) {
      setResult({
        status: "fail",
        message: (err as Error).message,
        model_id: draft.trim(),
        duration_ms: 0,
      });
    } finally {
      setTesting(false);
    }
  };

  const datalistId = "ce-model-suggestions";

  return (
    <div className="flex flex-col gap-1.5">
      <div className="flex items-center gap-2">
        <Input
          list={datalistId}
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter") commit();
          }}
          placeholder={t("settings.memory.crossEncoderModelPlaceholder")}
          disabled={disabled}
          className="h-8 w-[280px] rounded-full text-[13px]"
        />
        <datalist id={datalistId}>
          {CROSS_ENCODER_SUGGESTED_MODELS.map((id) => (
            <option key={id} value={id} />
          ))}
        </datalist>
        <Button
          size="sm"
          variant="outline"
          disabled={!dirty || disabled}
          onClick={commit}
          className="rounded-full"
        >
          {t("settings.config.save")}
        </Button>
        <Button
          size="sm"
          variant="outline"
          disabled={disabled || testing || draft.trim().length === 0}
          onClick={() => ensureThen("cross_encoder", () => void runTest())}
          className="rounded-full"
        >
          {testing ? (
            <Loader2 className="mr-1.5 h-3.5 w-3.5 animate-spin" aria-hidden />
          ) : null}
          {t("settings.memory.crossEncoderTest")}
        </Button>
      </div>
      {result ? (
        <div
          className={
            result.status === "ok"
              ? "text-[12px] text-emerald-600 dark:text-emerald-400"
              : "text-[12px] text-destructive"
          }
        >
          {result.status === "ok" ? "✓ " : "✗ "}
          {result.message}
        </div>
      ) : null}
    </div>
  );
}



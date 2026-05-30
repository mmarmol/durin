import { useCallback, useEffect, useMemo, useState } from "react";
import { Loader2 } from "lucide-react";
import { useTranslation } from "react-i18next";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import {
  getConfig,
  setConfigValue,
  testCrossEncoderModel,
  type CrossEncoderTestResult,
} from "@/lib/api";
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
  search?: {
    cross_encoder?: Partial<CrossEncoderState>;
  };
  dream?: {
    threshold_entries?: number;
  };
}

function readCrossEncoder(config: Record<string, unknown> | null): CrossEncoderState {
  const memory = config?.memory as MemoryConfigShape | undefined;
  const ce = memory?.search?.cross_encoder ?? {};
  return {
    enabled: typeof ce.enabled === "boolean" ? ce.enabled : false,
    model: typeof ce.model === "string" && ce.model ? ce.model : DEFAULT_CROSS_ENCODER_MODEL,
  };
}

function readThresholdEntries(config: Record<string, unknown> | null): number {
  const memory = config?.memory as MemoryConfigShape | undefined;
  const value = memory?.dream?.threshold_entries;
  return typeof value === "number" && Number.isFinite(value) ? value : 5;
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
  const thresholdEntries = useMemo(() => readThresholdEntries(config), [config]);

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
                void onSave("memory.search.cross_encoder.enabled", !crossEncoder.enabled)
              }
              className="w-[68px] rounded-full"
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
            />
          </SettingsRow>
        </SettingsGroup>
      </section>

      <section>
        <SettingsSectionTitle>{t("settings.memory.sections.dream")}</SettingsSectionTitle>
        <SettingsGroup>
          <ThresholdEntriesRow
            value={thresholdEntries}
            saving={savingPath === "memory.dream.threshold_entries"}
            onSave={(n) => void onSave("memory.dream.threshold_entries", n)}
          />
        </SettingsGroup>
      </section>

      <section>
        <SettingsSectionTitle>{t("settings.memory.sections.decay")}</SettingsSectionTitle>
        <SettingsGroup>
          <DecayDefaultsRows />
        </SettingsGroup>
      </section>
    </div>
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
}: {
  token: string;
  value: string;
  disabled: boolean;
  onSave: (model: string) => void;
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
          onClick={() => void runTest()}
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

function ThresholdEntriesRow({
  value,
  saving,
  onSave,
}: {
  value: number;
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
    <SettingsRow
      title={t("settings.memory.rows.thresholdEntries")}
      description={t("settings.memory.help.thresholdEntries")}
    >
      <div className="flex items-center gap-2">
        <Input
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter") commit();
          }}
          inputMode="numeric"
          className="h-8 w-[100px] rounded-full text-[13px]"
        />
        <Button
          size="sm"
          variant="outline"
          disabled={!dirty || saving}
          onClick={commit}
          className="rounded-full"
        >
          {t("settings.config.save")}
        </Button>
      </div>
    </SettingsRow>
  );
}

/** Read-only mirror of `CLASS_HALF_LIFE_DEFAULTS`. Decay defaults are
 *  defined in code (durin/memory/decay.py); per-entry overrides live in
 *  individual entry frontmatter, not the global config. */
function DecayDefaultsRows() {
  const { t } = useTranslation();
  const rows: Array<{ className: string; days: number | null }> = [
    { className: "episodic", days: 90 },
    { className: "session_summary", days: 120 },
    { className: "entity", days: null },
    { className: "stable", days: null },
    { className: "corpus", days: null },
  ];
  return (
    <>
      {rows.map(({ className, days }) => (
        <SettingsRow
          key={className}
          title={t(`settings.memory.decayClasses.${className}`)}
          description={t(`settings.memory.decayHelp.${className}`)}
        >
          <span className="text-[12px] tabular-nums text-muted-foreground">
            {days === null
              ? t("settings.memory.decayNever")
              : t("settings.memory.decayDays", { days })}
          </span>
        </SettingsRow>
      ))}
      <SettingsRow
        title={t("settings.memory.rows.decayOverrides")}
        description={t("settings.memory.help.decayOverrides")}
      />
    </>
  );
}

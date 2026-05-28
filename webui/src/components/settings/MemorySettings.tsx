import { useCallback, useEffect, useMemo, useState } from "react";
import { Check, ChevronDown, Loader2 } from "lucide-react";
import { useTranslation } from "react-i18next";

import { Button } from "@/components/ui/button";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { Input } from "@/components/ui/input";
import { getConfig, setConfigValue } from "@/lib/api";
import { cn } from "@/lib/utils";
import {
  SettingsGroup,
  SettingsRow,
  SettingsSectionTitle,
} from "./primitives";

const CROSS_ENCODER_MODELS: ReadonlyArray<{ id: string; labelKey: string }> = [
  { id: "jinaai/jina-reranker-v2-base-multilingual", labelKey: "jinaV2" },
  { id: "BAAI/bge-reranker-base", labelKey: "bgeBase" },
  { id: "BAAI/bge-reranker-v2-m3", labelKey: "bgeV2M3" },
  { id: "Qwen/Qwen3-Reranker-0.6B", labelKey: "qwen3" },
] as const;

const DEFAULT_CROSS_ENCODER_MODEL = CROSS_ENCODER_MODELS[0].id;

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
            <CrossEncoderModelPicker
              value={crossEncoder.model}
              disabled={!crossEncoder.enabled || savingPath === "memory.search.cross_encoder.model"}
              onChange={(model) => void onSave("memory.search.cross_encoder.model", model)}
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

function CrossEncoderModelPicker({
  value,
  disabled,
  onChange,
}: {
  value: string;
  disabled: boolean;
  onChange: (model: string) => void;
}) {
  const { t } = useTranslation();
  const selected = CROSS_ENCODER_MODELS.find((m) => m.id === value);
  const label = selected
    ? t(`settings.memory.crossEncoderModels.${selected.labelKey}`)
    : value;
  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild disabled={disabled}>
        <Button
          type="button"
          variant="outline"
          disabled={disabled}
          className={cn(
            "h-8 w-[260px] justify-between rounded-full border-input bg-background px-3 text-[13px] font-normal shadow-none",
            "hover:bg-accent/55 focus-visible:ring-2 focus-visible:ring-ring",
            disabled && "text-muted-foreground",
          )}
        >
          <span className="truncate">{label}</span>
          <ChevronDown className="ml-2 h-3.5 w-3.5 shrink-0 text-muted-foreground" aria-hidden />
        </Button>
      </DropdownMenuTrigger>
      <DropdownMenuContent
        align="end"
        className="max-h-[18rem] w-[300px] overflow-y-auto rounded-[18px] border-border/65 bg-popover p-1.5 text-popover-foreground shadow-[0_18px_55px_rgba(15,23,42,0.18)] dark:border-white/10 dark:shadow-[0_22px_55px_rgba(0,0,0,0.45)]"
      >
        {CROSS_ENCODER_MODELS.map((model) => {
          const isSelected = model.id === value;
          return (
            <DropdownMenuItem
              key={model.id}
              onSelect={() => onChange(model.id)}
              className={cn(
                "flex cursor-default items-center justify-between gap-2 rounded-[12px] px-3 py-2 text-[13px]",
                "focus:bg-muted focus:text-foreground",
                isSelected && "bg-primary/10 text-primary focus:bg-primary/12 focus:text-primary",
              )}
            >
              <span className="truncate">
                {t(`settings.memory.crossEncoderModels.${model.labelKey}`)}
              </span>
              {isSelected ? <Check className="h-3.5 w-3.5 shrink-0" aria-hidden /> : null}
            </DropdownMenuItem>
          );
        })}
      </DropdownMenuContent>
    </DropdownMenu>
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

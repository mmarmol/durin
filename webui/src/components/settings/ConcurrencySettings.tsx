import { useCallback, useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { getConfig, setConfigValue } from "@/lib/api";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { useConcurrencySnapshot } from "@/hooks/useConcurrencySnapshot";
import {
  SettingsGroup,
  SettingsRow,
  SettingsSectionTitle,
} from "@/components/settings/primitives";

interface Caps {
  interactive: number;
  ceiling: number;
  subagents: number;
  workflowLlm: number;
  workflowScript: number;
}

function readCaps(config: Record<string, unknown> | null): Caps {
  const agents = config?.agents as { defaults?: Record<string, unknown> } | undefined;
  const d = agents?.defaults ?? {};
  const wf = (config?.workflow ?? {}) as Record<string, unknown>;
  const num = (v: unknown, fallback: number) =>
    typeof v === "number" && Number.isFinite(v) ? v : fallback;
  return {
    interactive: num(d.max_concurrent_interactive, 4),
    ceiling: num(d.concurrency_ceiling, 12),
    subagents: num(d.max_concurrent_subagents, 3),
    workflowLlm: num(wf.parallel_llm_concurrency, 2),
    workflowScript: num(wf.parallel_script_concurrency, 4),
  };
}

export function ConcurrencySettings({ token }: { token: string }) {
  const { t } = useTranslation();
  const [config, setConfig] = useState<Record<string, unknown> | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [savingPath, setSavingPath] = useState<string | null>(null);
  const snap = useConcurrencySnapshot();

  const load = useCallback(async () => {
    setError(null);
    try {
      const s = await getConfig(token);
      setConfig(s.config as Record<string, unknown>);
    } catch {
      setError(t("settings.concurrency.description"));
    }
  }, [token, t]);

  useEffect(() => {
    void load();
  }, [load]);

  const onSave = useCallback(
    async (path: string, value: number) => {
      setSavingPath(path);
      try {
        const next = await setConfigValue(token, path, value);
        setConfig(next as Record<string, unknown>);
      } finally {
        setSavingPath(null);
      }
    },
    [token],
  );

  const caps = readCaps(config);
  const fmt = (lane: { active: number; limit: number } | undefined) =>
    lane ? `${lane.active} / ${lane.limit === 0 ? t("settings.concurrency.live.unlimited") : lane.limit}` : "—";

  return (
    <div className="space-y-6">
      <p className="px-1 text-[13px] leading-5 text-muted-foreground">
        {t("settings.concurrency.description")}
      </p>

      {error ? (
        <div className="rounded-[18px] border border-destructive/20 bg-destructive/5 px-4 py-3 text-[13px] text-destructive">
          {error}
        </div>
      ) : null}

      <section>
        <SettingsSectionTitle>{t("settings.concurrency.sections.live")}</SettingsSectionTitle>
        <SettingsGroup>
          <SettingsRow title={t("settings.concurrency.live.interactive")}>
            <span className="tabular-nums text-[13px] text-foreground">{fmt(snap?.lanes.interactive)}</span>
          </SettingsRow>
          <SettingsRow title={t("settings.concurrency.live.ceiling")}>
            <span className="tabular-nums text-[13px] text-foreground">{fmt(snap?.lanes.ceiling)}</span>
          </SettingsRow>
          <SettingsRow title={t("settings.concurrency.live.subagents")}>
            <span className="tabular-nums text-[13px] text-foreground">{fmt(snap?.lanes.subagents)}</span>
          </SettingsRow>
          <SettingsRow title={t("settings.concurrency.live.queued")}>
            <span className="tabular-nums text-[13px] text-foreground">{snap ? snap.queued : "—"}</span>
          </SettingsRow>
        </SettingsGroup>
      </section>

      <section>
        <SettingsSectionTitle>{t("settings.concurrency.sections.caps")}</SettingsSectionTitle>
        <SettingsGroup>
          <CapRow
            title={t("settings.concurrency.rows.interactive")}
            description={t("settings.concurrency.help.interactive")}
            value={caps.interactive}
            saving={savingPath === "agents.defaults.max_concurrent_interactive"}
            onSave={(n) => void onSave("agents.defaults.max_concurrent_interactive", n)}
          />
          <CapRow
            title={t("settings.concurrency.rows.ceiling")}
            description={t("settings.concurrency.help.ceiling")}
            value={caps.ceiling}
            saving={savingPath === "agents.defaults.concurrency_ceiling"}
            onSave={(n) => void onSave("agents.defaults.concurrency_ceiling", n)}
          />
          <CapRow
            title={t("settings.concurrency.rows.subagents")}
            description={t("settings.concurrency.help.subagents")}
            value={caps.subagents}
            saving={savingPath === "agents.defaults.max_concurrent_subagents"}
            onSave={(n) => void onSave("agents.defaults.max_concurrent_subagents", n)}
          />
        </SettingsGroup>
      </section>

      <section>
        <SettingsSectionTitle>{t("settings.concurrency.sections.workflows")}</SettingsSectionTitle>
        <SettingsGroup>
          <CapRow
            title={t("settings.concurrency.rows.workflowLlm")}
            description={t("settings.concurrency.help.workflowLlm")}
            value={caps.workflowLlm}
            saving={savingPath === "workflow.parallel_llm_concurrency"}
            onSave={(n) => void onSave("workflow.parallel_llm_concurrency", n)}
          />
          <CapRow
            title={t("settings.concurrency.rows.workflowScript")}
            description={t("settings.concurrency.help.workflowScript")}
            value={caps.workflowScript}
            saving={savingPath === "workflow.parallel_script_concurrency"}
            onSave={(n) => void onSave("workflow.parallel_script_concurrency", n)}
          />
        </SettingsGroup>
      </section>
    </div>
  );
}

function CapRow({
  title,
  description,
  value,
  saving,
  onSave,
}: {
  title: string;
  description: string;
  value: number;
  saving: boolean;
  onSave: (n: number) => void;
}) {
  const { t } = useTranslation();
  const [draft, setDraft] = useState(String(value));
  useEffect(() => setDraft(String(value)), [value]);
  const parsed = Number(draft);
  const valid = Number.isInteger(parsed) && parsed >= 1;
  const dirty = valid && parsed !== value;
  const commit = () => {
    if (dirty) onSave(parsed);
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
          className="h-8 w-[110px] rounded-full text-[13px]"
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

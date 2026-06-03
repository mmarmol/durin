import { useCallback, useEffect, useMemo, useState } from "react";
import { Loader2, X } from "lucide-react";
import { useTranslation } from "react-i18next";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import {
  getConfig,
  listSecrets,
  setConfigValue,
  testGithubToken,
  type GithubTokenTestResult,
} from "@/lib/api";
import { SettingsGroup, SettingsRow, SettingsSectionTitle } from "./primitives";

const MB = 1024 * 1024;

interface JudgeShape {
  enabled?: boolean;
  maxSeverity?: "caution" | "dangerous";
  model?: string;
}
interface SkillImportShape {
  allowlist?: string[];
  githubTokenSecret?: string;
  maxFiles?: number;
  maxTotalBytes?: number;
  maxFileBytes?: number;
  llmJudge?: JudgeShape;
}

function readSI(config: Record<string, unknown> | null) {
  const memory = config?.memory as { skillImport?: SkillImportShape } | undefined;
  const si = memory?.skillImport ?? {};
  const j = si.llmJudge ?? {};
  return {
    allowlist: Array.isArray(si.allowlist) ? si.allowlist.filter((x) => typeof x === "string") : [],
    githubTokenSecret: typeof si.githubTokenSecret === "string" ? si.githubTokenSecret : "",
    maxFiles: typeof si.maxFiles === "number" ? si.maxFiles : 100,
    maxTotalBytes: typeof si.maxTotalBytes === "number" ? si.maxTotalBytes : 3 * MB,
    maxFileBytes: typeof si.maxFileBytes === "number" ? si.maxFileBytes : MB,
    judgeEnabled: typeof j.enabled === "boolean" ? j.enabled : true,
    judgeMaxSeverity: j.maxSeverity === "dangerous" ? "dangerous" : "caution",
    judgeModel: typeof j.model === "string" ? j.model : "",
  };
}

/** Skills security — the import policy surface (spec 2026-06-03): LLM judge,
 *  trust patterns (allowlist), size caps, and the GitHub token secret. */
export function SkillsSecuritySettings({ token }: { token: string }) {
  const { t } = useTranslation();
  const [config, setConfig] = useState<Record<string, unknown> | null>(null);
  const [secrets, setSecrets] = useState<string[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [savingPath, setSavingPath] = useState<string | null>(null);
  const [newPattern, setNewPattern] = useState("");
  const [tokenTest, setTokenTest] = useState<GithubTokenTestResult | "testing" | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const [snap, secs] = await Promise.all([getConfig(token), listSecrets(token)]);
      setConfig(snap.config as Record<string, unknown>);
      setSecrets(secs.map((s) => (s as { name?: string }).name ?? "").filter(Boolean));
    } catch {
      setError(t("settings.skillsSecurity.loadError"));
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
        setError(t("settings.skillsSecurity.saveError"));
      } finally {
        setSavingPath(null);
      }
    },
    [token, t],
  );

  const v = useMemo(() => readSI(config), [config]);

  const addPattern = useCallback(() => {
    const p = newPattern.trim();
    if (!p || v.allowlist.includes(p)) return;
    void onSave("memory.skill_import.allowlist", [...v.allowlist, p]);
    setNewPattern("");
  }, [newPattern, v.allowlist, onSave]);

  const removePattern = useCallback(
    (p: string) => onSave("memory.skill_import.allowlist", v.allowlist.filter((x) => x !== p)),
    [v.allowlist, onSave],
  );

  const runTokenTest = useCallback(async () => {
    if (!v.githubTokenSecret) return;
    setTokenTest("testing");
    try {
      setTokenTest(await testGithubToken(token, v.githubTokenSecret));
    } catch {
      setTokenTest({ ok: false, error: t("settings.skillsSecurity.tokenTestError") });
    }
  }, [token, v.githubTokenSecret, t]);

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
        {t("settings.skillsSecurity.description")}
      </p>

      {error ? (
        <div className="rounded-[18px] border border-destructive/20 bg-destructive/5 px-4 py-3 text-[13px] text-destructive">
          {error}
        </div>
      ) : null}

      {/* LLM judge */}
      <section>
        <SettingsSectionTitle>{t("settings.skillsSecurity.sections.judge")}</SettingsSectionTitle>
        <SettingsGroup>
          <SettingsRow
            title={t("settings.skillsSecurity.rows.judgeEnabled")}
            description={t("settings.skillsSecurity.help.judgeEnabled")}
          >
            <Button
              size="sm"
              variant="outline"
              disabled={savingPath === "memory.skill_import.llm_judge.enabled"}
              onClick={() => void onSave("memory.skill_import.llm_judge.enabled", !v.judgeEnabled)}
              className="w-[68px] rounded-full"
            >
              {v.judgeEnabled ? t("settings.config.on") : t("settings.config.off")}
            </Button>
          </SettingsRow>
          <SettingsRow
            title={t("settings.skillsSecurity.rows.judgeMaxSeverity")}
            description={t("settings.skillsSecurity.help.judgeMaxSeverity")}
          >
            <select
              value={v.judgeMaxSeverity}
              disabled={!v.judgeEnabled || savingPath === "memory.skill_import.llm_judge.max_severity"}
              onChange={(e) => void onSave("memory.skill_import.llm_judge.max_severity", e.target.value)}
              className="rounded-[8px] border border-border/60 bg-background px-2 py-1 text-[13px] disabled:opacity-50"
            >
              <option value="caution">{t("settings.skillsSecurity.severity.caution")}</option>
              <option value="dangerous">{t("settings.skillsSecurity.severity.dangerous")}</option>
            </select>
          </SettingsRow>
          <SettingsRow
            title={t("settings.skillsSecurity.rows.judgeModel")}
            description={t("settings.skillsSecurity.help.judgeModel")}
          >
            <ModelField
              value={v.judgeModel}
              disabled={!v.judgeEnabled}
              saving={savingPath === "memory.skill_import.llm_judge.model"}
              onSave={(m) => void onSave("memory.skill_import.llm_judge.model", m)}
            />
          </SettingsRow>
        </SettingsGroup>
      </section>

      {/* Trust patterns (allowlist) */}
      <section>
        <SettingsSectionTitle>{t("settings.skillsSecurity.sections.trust")}</SettingsSectionTitle>
        <p className="px-1 pb-2 text-[12px] text-muted-foreground">
          {t("settings.skillsSecurity.trustDescription")}
        </p>
        <SettingsGroup>
          <div className="flex flex-col gap-2 px-1 py-2">
            {v.allowlist.length === 0 ? (
              <p className="text-[12px] text-muted-foreground">{t("settings.skillsSecurity.noPatterns")}</p>
            ) : (
              v.allowlist.map((p) => (
                <div key={p} className="flex items-center gap-2">
                  <code className="flex-1 truncate rounded-[6px] bg-muted/40 px-2 py-1 text-[12px]">{p}</code>
                  <Button
                    size="sm"
                    variant="ghost"
                    disabled={savingPath === "memory.skill_import.allowlist"}
                    onClick={() => void removePattern(p)}
                    aria-label={t("settings.skillsSecurity.removePattern")}
                  >
                    <X className="h-4 w-4" />
                  </Button>
                </div>
              ))
            )}
            <div className="flex gap-2 pt-1">
              <Input
                value={newPattern}
                onChange={(e) => setNewPattern(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === "Enter") {
                    e.preventDefault();
                    addPattern();
                  }
                }}
                placeholder={t("settings.skillsSecurity.patternPlaceholder")}
                className="flex-1 text-[12px]"
              />
              <Button
                size="sm"
                disabled={!newPattern.trim() || savingPath === "memory.skill_import.allowlist"}
                onClick={addPattern}
              >
                {t("settings.skillsSecurity.addPattern")}
              </Button>
            </div>
          </div>
        </SettingsGroup>
      </section>

      {/* GitHub token */}
      <section>
        <SettingsSectionTitle>{t("settings.skillsSecurity.sections.token")}</SettingsSectionTitle>
        <p className="px-1 pb-2 text-[12px] text-muted-foreground">
          {t("settings.skillsSecurity.tokenDescription")}
        </p>
        <SettingsGroup>
          <SettingsRow
            title={t("settings.skillsSecurity.rows.tokenSecret")}
            description={t("settings.skillsSecurity.help.tokenSecret")}
          >
            <div className="flex items-center gap-2">
              <select
                value={v.githubTokenSecret}
                disabled={savingPath === "memory.skill_import.github_token_secret"}
                onChange={(e) => {
                  setTokenTest(null);
                  void onSave("memory.skill_import.github_token_secret", e.target.value);
                }}
                className="rounded-[8px] border border-border/60 bg-background px-2 py-1 text-[13px] disabled:opacity-50"
              >
                <option value="">{t("settings.skillsSecurity.tokenNone")}</option>
                {secrets.map((s) => (
                  <option key={s} value={s}>{s}</option>
                ))}
              </select>
              <Button
                size="sm"
                variant="outline"
                disabled={!v.githubTokenSecret || tokenTest === "testing"}
                onClick={() => void runTokenTest()}
              >
                {tokenTest === "testing" ? (
                  <Loader2 className="h-4 w-4 animate-spin" />
                ) : (
                  t("settings.skillsSecurity.testToken")
                )}
              </Button>
            </div>
          </SettingsRow>
          {tokenTest && tokenTest !== "testing" ? (
            <p className={`px-1 pb-2 text-[12px] ${tokenTest.ok ? "text-foreground" : "text-destructive"}`}>
              {tokenTest.ok
                ? t("settings.skillsSecurity.tokenOk", { remaining: tokenTest.remaining ?? "?" })
                : tokenTest.error || t("settings.skillsSecurity.tokenTestError")}
            </p>
          ) : null}
        </SettingsGroup>
      </section>

      {/* Size caps */}
      <section>
        <SettingsSectionTitle>{t("settings.skillsSecurity.sections.caps")}</SettingsSectionTitle>
        <SettingsGroup>
          <NumberRow
            title={t("settings.skillsSecurity.rows.maxFiles")}
            value={v.maxFiles}
            saving={savingPath === "memory.skill_import.max_files"}
            onSave={(n) => void onSave("memory.skill_import.max_files", n)}
          />
          <NumberRow
            title={t("settings.skillsSecurity.rows.maxTotalMb")}
            value={Math.round(v.maxTotalBytes / MB)}
            saving={savingPath === "memory.skill_import.max_total_bytes"}
            onSave={(n) => void onSave("memory.skill_import.max_total_bytes", n * MB)}
          />
          <NumberRow
            title={t("settings.skillsSecurity.rows.maxFileMb")}
            value={Math.round(v.maxFileBytes / MB)}
            saving={savingPath === "memory.skill_import.max_file_bytes"}
            onSave={(n) => void onSave("memory.skill_import.max_file_bytes", n * MB)}
          />
        </SettingsGroup>
      </section>
    </div>
  );
}

function ModelField({ value, disabled, saving, onSave }: {
  value: string; disabled: boolean; saving: boolean; onSave: (m: string) => void;
}) {
  const { t } = useTranslation();
  const [draft, setDraft] = useState(value);
  useEffect(() => setDraft(value), [value]);
  return (
    <div className="flex items-center gap-2">
      <Input
        value={draft}
        disabled={disabled}
        onChange={(e) => setDraft(e.target.value)}
        placeholder={t("settings.skillsSecurity.modelPlaceholder")}
        className="w-[180px] text-[12px]"
      />
      <Button size="sm" variant="outline" disabled={disabled || saving || draft === value}
        onClick={() => onSave(draft.trim())}>
        {t("settings.actions.save")}
      </Button>
    </div>
  );
}

function NumberRow({ title, value, saving, onSave }: {
  title: string; value: number; saving: boolean; onSave: (n: number) => void;
}) {
  const { t } = useTranslation();
  const [draft, setDraft] = useState(String(value));
  useEffect(() => setDraft(String(value)), [value]);
  const n = Number(draft);
  const valid = Number.isFinite(n) && n > 0;
  return (
    <SettingsRow title={title}>
      <div className="flex items-center gap-2">
        <Input
          type="number"
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          className="w-[88px] text-[12px]"
        />
        <Button size="sm" variant="outline" disabled={!valid || saving || n === value}
          onClick={() => onSave(Math.floor(n))}>
          {t("settings.actions.save")}
        </Button>
      </div>
    </SettingsRow>
  );
}

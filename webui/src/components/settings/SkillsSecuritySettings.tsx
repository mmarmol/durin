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
  trigger?: "off" | "uncertain" | "always";
  max_severity?: "caution" | "dangerous";
  model?: string;
}
interface SkillImportShape {
  allowlist?: string[];
  github_token_secret?: string;
  max_files?: number;
  max_total_bytes?: number;
  max_file_bytes?: number;
  llm_judge?: JudgeShape;
}

function readSI(config: Record<string, unknown> | null) {
  const skills = config?.skills as { security?: SkillImportShape } | undefined;
  const si = skills?.security ?? {};
  const j = si.llm_judge ?? {};
  return {
    allowlist: Array.isArray(si.allowlist) ? si.allowlist.filter((x) => typeof x === "string") : [],
    githubTokenSecret: typeof si.github_token_secret === "string" ? si.github_token_secret : "",
    maxFiles: typeof si.max_files === "number" ? si.max_files : 100,
    maxTotalBytes: typeof si.max_total_bytes === "number" ? si.max_total_bytes : 3 * MB,
    maxFileBytes: typeof si.max_file_bytes === "number" ? si.max_file_bytes : MB,
    judgeTrigger: ["off", "uncertain", "always"].includes(String(j.trigger))
      ? String(j.trigger)
      : "off",
    judgeMaxSeverity: j.max_severity === "dangerous" ? "dangerous" : "caution",
    judgeModel: typeof j.model === "string" ? j.model : "",
  };
}

interface RegistryShape {
  name?: string;
  kind?: string;
  enabled?: boolean;
}

/** The discovery registries (`skills.discovery.registries`) that `skill_search`
 *  queries — surfaced here so the operator can enable/disable each source. */
function readRegistries(config: Record<string, unknown> | null): RegistryShape[] {
  const skills = config?.skills as { discovery?: { registries?: unknown } } | undefined;
  const regs = skills?.discovery?.registries;
  return Array.isArray(regs)
    ? (regs.filter((r) => !!r && typeof r === "object") as RegistryShape[])
    : [];
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
  const registries = useMemo(() => readRegistries(config), [config]);

  const toggleRegistry = useCallback(
    (idx: number) => {
      const next = registries.map((r, i) =>
        i === idx ? { ...r, enabled: !(r.enabled ?? true) } : r,
      );
      void onSave("skills.discovery.registries", next);
    },
    [registries, onSave],
  );

  const addPattern = useCallback(() => {
    const p = newPattern.trim();
    if (!p || v.allowlist.includes(p)) return;
    void onSave("skills.security.allowlist", [...v.allowlist, p]);
    setNewPattern("");
  }, [newPattern, v.allowlist, onSave]);

  const removePattern = useCallback(
    (p: string) => onSave("skills.security.allowlist", v.allowlist.filter((x) => x !== p)),
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

  // Unmount into the spinner only before the FIRST load: later reloads
  // (the periodic auth-token re-mint changes the `token` prop) refresh in
  // place, so open editors and scroll position survive.
  if (loading && config === null) {
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

      {/* Discovery sources — which registries skill_search queries */}
      <section>
        <SettingsSectionTitle>{t("settings.skillsSecurity.sections.discovery")}</SettingsSectionTitle>
        <p className="px-1 pb-2 text-[12px] text-muted-foreground">
          {t("settings.skillsSecurity.discoveryDescription")}
        </p>
        <SettingsGroup>
          {registries.length === 0 ? (
            <p className="px-4 py-3 text-[12px] text-muted-foreground">
              {t("settings.skillsSecurity.noRegistries")}
            </p>
          ) : (
            registries.map((r, i) => {
              const label = r.name || r.kind || "";
              const on = r.enabled !== false;
              return (
                <SettingsRow key={r.kind || String(i)} title={label}>
                  <Button
                    size="sm"
                    variant="outline"
                    disabled={savingPath === "skills.discovery.registries"}
                    aria-label={t("settings.skillsSecurity.toggleRegistry", { name: label })}
                    onClick={() => toggleRegistry(i)}
                    className="min-w-[68px] rounded-full"
                  >
                    {on ? t("settings.config.on") : t("settings.config.off")}
                  </Button>
                </SettingsRow>
              );
            })
          )}
        </SettingsGroup>
      </section>

      {/* LLM judge */}
      <section>
        <SettingsSectionTitle>{t("settings.skillsSecurity.sections.judge")}</SettingsSectionTitle>
        <SettingsGroup>
          <SettingsRow
            title={t("settings.skillsSecurity.rows.judgeTrigger")}
            description={t("settings.skillsSecurity.help.judgeTrigger")}
          >
            <select
              value={v.judgeTrigger}
              disabled={savingPath === "skills.security.llm_judge.trigger"}
              onChange={(e) => void onSave("skills.security.llm_judge.trigger", e.target.value)}
              className="rounded-[8px] border border-border/60 bg-background px-2 py-1 text-[13px] disabled:opacity-50"
            >
              <option value="off">{t("settings.skillsSecurity.trigger.off")}</option>
              <option value="uncertain">{t("settings.skillsSecurity.trigger.uncertain")}</option>
              <option value="always">{t("settings.skillsSecurity.trigger.always")}</option>
            </select>
          </SettingsRow>
          <SettingsRow
            title={t("settings.skillsSecurity.rows.judgeMaxSeverity")}
            description={t("settings.skillsSecurity.help.judgeMaxSeverity")}
          >
            <select
              value={v.judgeMaxSeverity}
              disabled={savingPath === "skills.security.llm_judge.max_severity"}
              onChange={(e) => void onSave("skills.security.llm_judge.max_severity", e.target.value)}
              className="rounded-[8px] border border-border/60 bg-background px-2 py-1 text-[13px] disabled:opacity-50"
            >
              <option value="caution">{t("settings.skillsSecurity.severity.caution")}</option>
              <option value="dangerous">{t("settings.skillsSecurity.severity.dangerous")}</option>
            </select>
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
                    disabled={savingPath === "skills.security.allowlist"}
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
                disabled={!newPattern.trim() || savingPath === "skills.security.allowlist"}
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
                disabled={savingPath === "skills.security.github_token_secret"}
                onChange={(e) => {
                  setTokenTest(null);
                  void onSave("skills.security.github_token_secret", e.target.value);
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
            saving={savingPath === "skills.security.max_files"}
            onSave={(n) => void onSave("skills.security.max_files", n)}
          />
          <NumberRow
            title={t("settings.skillsSecurity.rows.maxTotalMb")}
            value={Math.round(v.maxTotalBytes / MB)}
            saving={savingPath === "skills.security.max_total_bytes"}
            onSave={(n) => void onSave("skills.security.max_total_bytes", n * MB)}
          />
          <NumberRow
            title={t("settings.skillsSecurity.rows.maxFileMb")}
            value={Math.round(v.maxFileBytes / MB)}
            saving={savingPath === "skills.security.max_file_bytes"}
            onSave={(n) => void onSave("skills.security.max_file_bytes", n * MB)}
          />
        </SettingsGroup>
      </section>
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

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Loader2 } from "lucide-react";
import { useTranslation } from "react-i18next";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { getConfig, getExtraStatus, setConfigValue, type ExtraStatus } from "@/lib/api";
import { useClient } from "@/providers/ClientProvider";
import { ExtraInstallPrompt } from "./ExtraInstallPrompt";
import {
  SettingsGroup,
  SettingsRow,
  SettingsSectionTitle,
} from "./primitives";

type Provider = "local" | "openai" | "groq" | "http";
type Mode = "auto" | "preview" | "off";
type LocalEngine = "parakeet" | "sensevoice";

interface TranscriptionConfigShape {
  enabled?: boolean;
  mode?: Mode;
  provider?: Provider;
  language?: string | null;
  local?: { engine?: LocalEngine };
  http?: { base_url?: string | null; api_key?: string | null; model?: string | null };
  openai?: { api_key?: string | null; api_base?: string | null };
  groq?: { api_key?: string | null; api_base?: string | null };
}

interface TranscriptionState {
  enabled: boolean;
  mode: Mode;
  provider: Provider;
  language: string;
  localEngine: LocalEngine;
  httpBaseUrl: string;
  httpApiKey: string;
  httpModel: string;
  openaiApiKey: string;
  groqApiKey: string;
  ttsEnabled: boolean;
  ttsProvider: string;
  ttsVoice: string;
  ttsLanguage: string;
  ttsOpenaiApiKey: string;
  voiceEnabled: boolean;
  bargeIn: boolean;
  spokenMode: string;
  spokenThreshold: number;
}

function readState(config: Record<string, unknown> | null): TranscriptionState {
  const t = (config?.transcription as TranscriptionConfigShape | undefined) ?? {};
  const tts = (config?.tts as Record<string, unknown> | undefined) ?? {};
  const ttsLocal = (tts.local as Record<string, unknown> | undefined) ?? {};
  const ttsOpenai = (tts.openai as Record<string, unknown> | undefined) ?? {};
  const voice = (config?.voice as Record<string, unknown> | undefined) ?? {};
  const sr = (voice.spoken_render as Record<string, unknown> | undefined) ?? {};
  return {
    enabled: typeof t.enabled === "boolean" ? t.enabled : true,
    mode: t.mode ?? "auto",
    provider: t.provider ?? "local",
    language: typeof t.language === "string" ? t.language : "",
    localEngine: t.local?.engine ?? "parakeet",
    httpBaseUrl: t.http?.base_url ?? "",
    httpApiKey: t.http?.api_key ?? "",
    httpModel: t.http?.model ?? "",
    openaiApiKey: t.openai?.api_key ?? "",
    groqApiKey: t.groq?.api_key ?? "",
    ttsEnabled: typeof tts.enabled === "boolean" ? tts.enabled : true,
    ttsProvider: (tts.provider as string) ?? "local",
    ttsVoice: (ttsLocal.voice as string) ?? "F4",
    ttsLanguage: (tts.language as string) ?? "",
    ttsOpenaiApiKey: (ttsOpenai.api_key as string) ?? "",
    voiceEnabled: typeof voice.enabled === "boolean" ? voice.enabled : true,
    bargeIn: typeof voice.barge_in === "boolean" ? voice.barge_in : true,
    spokenMode: (sr.mode as string) ?? "model_led",
    spokenThreshold: typeof sr.long_threshold_words === "number" ? sr.long_threshold_words : 60,
  };
}

// Option values are config-level identifiers (stay literal); labels resolve via i18n.
const PROVIDERS: ReadonlyArray<{ value: Provider; labelKey: string }> = [
  { value: "local", labelKey: "settings.voice.provider.local" },
  { value: "groq", labelKey: "settings.voice.provider.groq" },
  { value: "openai", labelKey: "settings.voice.provider.openai" },
  { value: "http", labelKey: "settings.voice.provider.http" },
];

const MODES: ReadonlyArray<{ value: Mode; labelKey: string }> = [
  { value: "auto", labelKey: "settings.voice.mode.auto" },
  { value: "preview", labelKey: "settings.voice.mode.preview" },
  { value: "off", labelKey: "settings.voice.mode.off" },
];

const LOCAL_ENGINES: ReadonlyArray<{ value: LocalEngine; labelKey: string }> = [
  { value: "parakeet", labelKey: "settings.voice.engine.parakeet" },
  { value: "sensevoice", labelKey: "settings.voice.engine.sensevoice" },
];

const TTS_PROVIDERS: ReadonlyArray<{ value: string; labelKey: string }> = [
  { value: "local", labelKey: "settings.voice.tts.providerLocal" },
  { value: "openai", labelKey: "settings.voice.tts.providerOpenai" },
];
const TTS_VOICES = ["F1", "F2", "F3", "F4", "F5", "M1", "M2", "M3", "M4", "M5"] as const;
const SPOKEN_MODES: ReadonlyArray<{ value: string; labelKey: string }> = [
  { value: "model_led", labelKey: "settings.voice.spoken.modelLed" },
  { value: "aux_summary", labelKey: "settings.voice.spoken.auxSummary" },
  { value: "verbatim", labelKey: "settings.voice.spoken.verbatim" },
];

export function TranscriptionSettings({ token }: { token: string }) {
  const { t } = useTranslation();
  const { client } = useClient();
  const [config, setConfig] = useState<Record<string, unknown> | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [savingPath, setSavingPath] = useState<string | null>(null);
  const [sttStatus, setSttStatus] = useState<ExtraStatus | null>(null);
  const [ttsStatus, setTtsStatus] = useState<ExtraStatus | null>(null);
  const [previewing, setPreviewing] = useState(false);
  const [previewError, setPreviewError] = useState<string | null>(null);
  const previewTimer = useRef<ReturnType<typeof setTimeout> | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const [snap, st, ttsSt] = await Promise.all([
        getConfig(token),
        getExtraStatus(token, "stt"),
        getExtraStatus(token, "tts"),
      ]);
      setConfig(snap.config as Record<string, unknown>);
      setSttStatus(st);
      setTtsStatus(ttsSt);
    } catch {
      setError(t("settings.voice.loadError"));
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
        setError(t("settings.voice.saveError"));
      } finally {
        setSavingPath(null);
      }
    },
    [token, t],
  );

  const state = useMemo(() => readState(config), [config]);

  const [pendingExtra, setPendingExtra] = useState<{
    feature: string;
    status: ExtraStatus;
    after: () => void;
  } | null>(null);
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
        after();
      }
    },
    [token],
  );

  useEffect(
    () => () => {
      if (previewTimer.current) clearTimeout(previewTimer.current);
    },
    [],
  );

  const handlePreview = useCallback(
    (voice: string, language: string) => {
      setPreviewError(null);
      setPreviewing(true);
      let unsub: (() => void) | null = null;
      const finish = () => {
        if (previewTimer.current) {
          clearTimeout(previewTimer.current);
          previewTimer.current = null;
        }
        unsub?.();
        unsub = null;
        setPreviewing(false);
      };
      unsub = client.onVoicePreviewAudio((url, error) => {
        if (url) {
          void (async () => {
            try {
              await new Audio(url).play();
            } catch {
              // autoplay can reject; the user still got the synthesized sample
            }
          })();
        } else {
          setPreviewError(
            error === "tts_unavailable"
              ? t("settings.voice.preview.installToPreview")
              : t("settings.voice.preview.failed"),
          );
        }
        finish();
      });
      // The first synth after installing [tts] downloads the ~260 MB Supertonic
      // model, which can exceed a short timeout — and the gateway normally warms
      // it at startup anyway. Wait generously and, if it still hasn't arrived,
      // show a "preparing" hint instead of silently resetting (which looked broken).
      previewTimer.current = setTimeout(() => {
        setPreviewError(t("settings.voice.preview.preparing"));
        finish();
      }, 30000);
      client.sendVoicePreview(voice, language || null);
    },
    [client, t],
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
        {t("settings.voice.intro")}
      </p>

      {error ? (
        <div className="rounded-[18px] border border-destructive/20 bg-destructive/5 px-4 py-3 text-[13px] text-destructive">
          {error}
        </div>
      ) : null}

      <section>
        <SettingsSectionTitle>{t("settings.voice.section.provider")}</SettingsSectionTitle>
        <SettingsGroup>
          <SettingsRow
            title={t("settings.voice.providerTitle")}
            description={t("settings.voice.providerDesc")}
          >
            <select
              value={state.provider}
              onChange={(e) => void onSave("transcription.provider", e.target.value)}
              disabled={savingPath === "transcription.provider"}
              className="h-8 rounded-full border bg-background px-3 text-[13px]"
            >
              {PROVIDERS.map((p) => (
                <option key={p.value} value={p.value}>
                  {t(p.labelKey)}
                </option>
              ))}
            </select>
          </SettingsRow>

          {state.provider === "local" ? (
            <>
              <SettingsRow
                title={t("settings.voice.engineTitle")}
                description={
                  state.localEngine === "parakeet"
                    ? t("settings.voice.engineDesc.parakeet")
                    : t("settings.voice.engineDesc.sensevoice")
                }
              >
                <select
                  value={state.localEngine}
                  onChange={(e) =>
                    void onSave("transcription.local.engine", e.target.value)
                  }
                  disabled={savingPath === "transcription.local.engine"}
                  className="h-8 rounded-full border bg-background px-3 text-[13px]"
                >
                  {LOCAL_ENGINES.map((eng) => (
                    <option key={eng.value} value={eng.value}>
                      {t(eng.labelKey)}
                    </option>
                  ))}
                </select>
              </SettingsRow>
              <SettingsRow
                title={t("settings.voice.localStt.title")}
                description={
                  sttStatus?.present
                    ? t("settings.voice.localStt.installedDesc")
                    : t("settings.voice.localStt.missingDesc")
                }
              >
                {sttStatus?.present ? (
                  <span className="text-[12px] text-emerald-600 dark:text-emerald-400">
                    {t("settings.voice.localStt.installedBadge")}
                  </span>
                ) : (
                  <Button
                    size="sm"
                    variant="outline"
                    onClick={() =>
                      void ensureThen("stt", () => void load())
                    }
                    className="rounded-full"
                  >
                    {t("settings.voice.install.stt")}
                  </Button>
                )}
              </SettingsRow>
              {pendingExtra && pendingExtra.feature === "stt" ? (
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
            </>
          ) : null}

          {state.provider === "groq" ? (
            <SettingsRow
              title={t("settings.voice.groqKey.title")}
              description={t("settings.voice.groqKey.desc")}
            >
              <ApiKeyInput
                value={state.groqApiKey}
                disabled={savingPath === "transcription.groq.api_key"}
                onSave={(v) => void onSave("transcription.groq.api_key", v)}
              />
            </SettingsRow>
          ) : null}

          {state.provider === "openai" ? (
            <SettingsRow
              title={t("settings.voice.openaiKey.title")}
              description={t("settings.voice.openaiKey.desc")}
            >
              <ApiKeyInput
                value={state.openaiApiKey}
                disabled={savingPath === "transcription.openai.api_key"}
                onSave={(v) => void onSave("transcription.openai.api_key", v)}
              />
            </SettingsRow>
          ) : null}

          {state.provider === "http" ? (
            <>
              <SettingsRow
                title={t("settings.voice.http.baseUrlTitle")}
                description={t("settings.voice.http.baseUrlDesc")}
              >
                <TextRow
                  value={state.httpBaseUrl}
                  placeholder="http://localhost:8080/v1"
                  disabled={savingPath === "transcription.http.base_url"}
                  onSave={(v) => void onSave("transcription.http.base_url", v)}
                />
              </SettingsRow>
              <SettingsRow
                title={t("settings.voice.http.modelTitle")}
                description={t("settings.voice.http.modelDesc")}
              >
                <TextRow
                  value={state.httpModel}
                  placeholder="whisper-large-v3"
                  disabled={savingPath === "transcription.http.model"}
                  onSave={(v) => void onSave("transcription.http.model", v)}
                />
              </SettingsRow>
              <SettingsRow
                title={t("settings.voice.http.keyTitle")}
                description={t("settings.voice.http.keyDesc")}
              >
                <ApiKeyInput
                  value={state.httpApiKey}
                  disabled={savingPath === "transcription.http.api_key"}
                  onSave={(v) => void onSave("transcription.http.api_key", v)}
                />
              </SettingsRow>
            </>
          ) : null}
        </SettingsGroup>
      </section>

      <section>
        <SettingsSectionTitle>{t("settings.voice.section.behavior")}</SettingsSectionTitle>
        <SettingsGroup>
          <SettingsRow
            title={t("settings.voice.enabledTitle")}
            description={t("settings.voice.enabledDesc")}
          >
            <Button
              size="sm"
              variant="outline"
              disabled={savingPath === "transcription.enabled"}
              onClick={() =>
                void onSave("transcription.enabled", !state.enabled)
              }
              className="w-[68px] rounded-full"
            >
              {state.enabled ? t("settings.config.on") : t("settings.config.off")}
            </Button>
          </SettingsRow>
          <SettingsRow
            title={t("settings.voice.modeTitle")}
            description={t("settings.voice.modeDesc")}
          >
            <select
              value={state.mode}
              onChange={(e) => void onSave("transcription.mode", e.target.value)}
              disabled={savingPath === "transcription.mode"}
              className="h-8 rounded-full border bg-background px-3 text-[13px]"
            >
              {MODES.map((m) => (
                <option key={m.value} value={m.value}>
                  {t(m.labelKey)}
                </option>
              ))}
            </select>
          </SettingsRow>
          <SettingsRow
            title={t("settings.voice.languageHintTitle")}
            description={t("settings.voice.languageHintDesc")}
          >
            <TextRow
              value={state.language}
              placeholder="auto"
              disabled={savingPath === "transcription.language"}
              onSave={(v) =>
                void onSave("transcription.language", v || null)
              }
            />
          </SettingsRow>
        </SettingsGroup>
      </section>

        <section>
          <SettingsSectionTitle>{t("settings.voice.section.tts")}</SettingsSectionTitle>
          <SettingsGroup>
            <SettingsRow
              title={t("settings.voice.tts.providerTitle")}
              description={t("settings.voice.tts.providerDesc")}
            >
              <select
                value={state.ttsProvider}
                onChange={(e) => void onSave("tts.provider", e.target.value)}
                disabled={savingPath === "tts.provider"}
                className="h-8 rounded-full border bg-background px-3 text-[13px]"
              >
                {TTS_PROVIDERS.map((p) => (
                  <option key={p.value} value={p.value}>{t(p.labelKey)}</option>
                ))}
              </select>
            </SettingsRow>
            {state.ttsProvider === "local" ? (
              <>
                <SettingsRow
                  title={t("settings.voice.tts.voiceTitle")}
                  description={t("settings.voice.tts.voiceDesc")}
                >
                  <div className="flex items-center gap-2">
                    <select
                      value={state.ttsVoice}
                      onChange={(e) => void onSave("tts.local.voice", e.target.value)}
                      disabled={savingPath === "tts.local.voice"}
                      className="h-8 rounded-full border bg-background px-3 text-[13px]"
                    >
                      {TTS_VOICES.map((v) => (<option key={v} value={v}>{v}</option>))}
                    </select>
                    <Button
                      size="sm"
                      variant="outline"
                      className="rounded-full"
                      disabled={previewing}
                      onClick={() => handlePreview(state.ttsVoice, state.ttsLanguage)}
                    >
                      {previewing ? t("settings.voice.tts.playing") : t("settings.voice.tts.test")}
                    </Button>
                    {previewError ? (
                      <span className="text-[12px] text-muted-foreground">{previewError}</span>
                    ) : null}
                  </div>
                </SettingsRow>
                <SettingsRow
                  title={t("settings.voice.tts.localInstallTitle")}
                  description={
                    ttsStatus?.present
                      ? t("settings.voice.tts.localInstalledDesc")
                      : t("settings.voice.tts.localInstallDesc")
                  }
                >
                  {ttsStatus?.present ? (
                    <span className="text-[12px] text-emerald-600 dark:text-emerald-400">
                      {t("settings.voice.localStt.installedBadge")}
                    </span>
                  ) : (
                    <Button size="sm" variant="outline" className="rounded-full"
                            onClick={() => void ensureThen("tts", () => void load())}>
                      {t("settings.voice.install.tts")}
                    </Button>
                  )}
                </SettingsRow>
                {pendingExtra && pendingExtra.feature === "tts" ? (
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
              </>
            ) : null}
            {state.ttsProvider === "openai" ? (
              <>
                <SettingsRow title={t("settings.voice.tts.modelTitle")} description="gpt-4o-mini-tts" />
                <SettingsRow
                  title={t("settings.voice.tts.openaiKeyTitle")}
                  description={t("settings.voice.tts.openaiKeyDesc")}
                >
                  <ApiKeyInput
                    value={state.ttsOpenaiApiKey}
                    disabled={savingPath === "tts.openai.api_key"}
                    onSave={(v) => void onSave("tts.openai.api_key", v)}
                  />
                </SettingsRow>
              </>
            ) : null}
            <SettingsRow
              title={t("settings.voice.languageHintTitle")}
              description={t("settings.voice.languageHintDesc")}
            >
              <TextRow
                value={state.ttsLanguage}
                placeholder="auto"
                disabled={savingPath === "tts.language"}
                onSave={(v) => void onSave("tts.language", v || null)}
              />
            </SettingsRow>
          </SettingsGroup>
        </section>

        <section>
          <SettingsSectionTitle>{t("settings.voice.section.conversational")}</SettingsSectionTitle>
          <SettingsGroup>
            <SettingsRow
              title={t("settings.voice.conv.handsFreeTitle")}
              description={t("settings.voice.conv.handsFreeDesc")}
            >
              <Button size="sm" variant="outline" className="rounded-full"
                      onClick={() => void onSave("voice.enabled", !state.voiceEnabled)}>
                {state.voiceEnabled ? t("settings.config.on") : t("settings.config.off")}
              </Button>
            </SettingsRow>
            <SettingsRow
              title={t("settings.voice.conv.bargeInTitle")}
              description={t("settings.voice.conv.bargeInDesc")}
            >
              <Button size="sm" variant="outline" className="rounded-full"
                      onClick={() => void onSave("voice.barge_in", !state.bargeIn)}>
                {state.bargeIn ? t("settings.config.on") : t("settings.config.off")}
              </Button>
            </SettingsRow>
          </SettingsGroup>
        </section>

        <section>
          <SettingsSectionTitle>{t("settings.voice.section.spokenRendition")}</SettingsSectionTitle>
          <SettingsGroup>
            <SettingsRow
              title={t("settings.voice.spoken.longRepliesTitle")}
              description={t("settings.voice.spoken.longRepliesDesc")}
            >
              <select
                value={state.spokenMode}
                onChange={(e) => void onSave("voice.spoken_render.mode", e.target.value)}
                disabled={savingPath === "voice.spoken_render.mode"}
                className="h-8 rounded-full border bg-background px-3 text-[13px]"
              >
                {SPOKEN_MODES.map((m) => (
                  <option key={m.value} value={m.value}>{t(m.labelKey)}</option>
                ))}
              </select>
            </SettingsRow>
          </SettingsGroup>
        </section>
    </div>
  );
}

function ApiKeyInput({
  value,
  disabled,
  onSave,
}: {
  value: string;
  disabled: boolean;
  onSave: (v: string) => void;
}) {
  const { t } = useTranslation();
  const [draft, setDraft] = useState(value);
  const [visible, setVisible] = useState(false);
  useEffect(() => setDraft(value), [value]);
  const dirty = draft !== value && draft.length > 0;
  return (
    <div className="flex items-center gap-2">
      <Input
        type={visible ? "text" : "password"}
        value={draft}
        onChange={(e) => setDraft(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === "Enter" && dirty) onSave(draft);
        }}
        placeholder="sk-…"
        disabled={disabled}
        className="h-8 w-[240px] rounded-full text-[13px]"
      />
      <Button
        size="sm"
        variant="ghost"
        onClick={() => setVisible((v) => !v)}
        className="rounded-full"
      >
        {visible ? t("settings.voice.apiKey.hide") : t("settings.voice.apiKey.show")}
      </Button>
      <Button
        size="sm"
        variant="outline"
        disabled={!dirty || disabled}
        onClick={() => onSave(draft)}
        className="rounded-full"
      >
        {t("settings.config.save")}
      </Button>
    </div>
  );
}

function TextRow({
  value,
  placeholder,
  disabled,
  onSave,
}: {
  value: string;
  placeholder?: string;
  disabled: boolean;
  onSave: (v: string) => void;
}) {
  const { t } = useTranslation();
  const [draft, setDraft] = useState(value);
  useEffect(() => setDraft(value), [value]);
  const commit = () => {
    if (draft.trim() !== value) onSave(draft.trim());
  };
  return (
    <div className="flex items-center gap-2">
      <Input
        value={draft}
        onChange={(e) => setDraft(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === "Enter") commit();
        }}
        placeholder={placeholder}
        disabled={disabled}
        className="h-8 w-[240px] rounded-full text-[13px]"
      />
      <Button
        size="sm"
        variant="outline"
        disabled={disabled || draft.trim() === value}
        onClick={commit}
        className="rounded-full"
      >
        {t("settings.config.save")}
      </Button>
    </div>
  );
}

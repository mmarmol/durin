import { useCallback, useEffect, useMemo, useState } from "react";
import { Loader2 } from "lucide-react";
import { useTranslation } from "react-i18next";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { getConfig, getExtraStatus, setConfigValue, type ExtraStatus } from "@/lib/api";
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
  http?: { baseUrl?: string | null; apiKey?: string | null; model?: string | null };
  openai?: { apiKey?: string | null; apiBase?: string | null };
  groq?: { apiKey?: string | null; apiBase?: string | null };
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
}

function readState(config: Record<string, unknown> | null): TranscriptionState {
  const t = (config?.transcription as TranscriptionConfigShape | undefined) ?? {};
  return {
    enabled: typeof t.enabled === "boolean" ? t.enabled : true,
    mode: t.mode ?? "auto",
    provider: t.provider ?? "local",
    language: typeof t.language === "string" ? t.language : "",
    localEngine: t.local?.engine ?? "parakeet",
    httpBaseUrl: t.http?.baseUrl ?? "",
    httpApiKey: t.http?.apiKey ?? "",
    httpModel: t.http?.model ?? "",
    openaiApiKey: t.openai?.apiKey ?? "",
    groqApiKey: t.groq?.apiKey ?? "",
  };
}

const PROVIDERS: ReadonlyArray<{ value: Provider; label: string; hint: string }> = [
  { value: "local", label: "Local STT (sherpa-onnx)", hint: "fast local engines, offline, needs [stt] extra" },
  { value: "groq", label: "Groq", hint: "whisper-large-v3 via Groq API (fast, free tier)" },
  { value: "openai", label: "OpenAI", hint: "whisper-1 via OpenAI API" },
  { value: "http", label: "HTTP server", hint: "any OpenAI-compatible /v1/audio/transcriptions" },
];

const MODES: ReadonlyArray<{ value: Mode; label: string; hint: string }> = [
  { value: "auto", label: "Auto", hint: "transcribe and insert text; edit before send" },
  { value: "preview", label: "Preview", hint: "show transcript; accept before send" },
  { value: "off", label: "Off", hint: "attach raw audio; no transcription" },
];

const LOCAL_ENGINES: ReadonlyArray<{ value: LocalEngine; label: string; hint: string }> = [
  {
    value: "parakeet",
    label: "Parakeet TDT v3",
    hint: "25 European languages incl. Spanish / English — fastest; no Japanese / Chinese",
  },
  {
    value: "sensevoice",
    label: "SenseVoice",
    hint: "Chinese / Japanese / Korean / Cantonese / English",
  },
];

export function TranscriptionSettings({ token }: { token: string }) {
  const { t } = useTranslation();
  const [config, setConfig] = useState<Record<string, unknown> | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [savingPath, setSavingPath] = useState<string | null>(null);
  const [sttStatus, setSttStatus] = useState<ExtraStatus | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const [snap, st] = await Promise.all([
        getConfig(token),
        getExtraStatus(token, "stt"),
      ]);
      setConfig(snap.config as Record<string, unknown>);
      setSttStatus(st);
    } catch {
      setError("Could not load transcription settings.");
    } finally {
      setLoading(false);
    }
  }, [token]);

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
        setError(`Could not save ${path}.`);
      } finally {
        setSavingPath(null);
      }
    },
    [token],
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
        When you attach or record audio, durin transcribes it to text before it
        reaches the agent. The default is local STT via sherpa-onnx (offline).
        Switch to a cloud provider or any OpenAI-compatible HTTP server as needed.
      </p>

      {error ? (
        <div className="rounded-[18px] border border-destructive/20 bg-destructive/5 px-4 py-3 text-[13px] text-destructive">
          {error}
        </div>
      ) : null}

      <section>
        <SettingsSectionTitle>Provider</SettingsSectionTitle>
        <SettingsGroup>
          <SettingsRow
            title="Transcription provider"
            description="Local STT needs the [stt] extra; cloud providers need an API key."
          >
            <select
              value={state.provider}
              onChange={(e) => void onSave("transcription.provider", e.target.value)}
              disabled={savingPath === "transcription.provider"}
              className="h-8 rounded-full border bg-background px-3 text-[13px]"
            >
              {PROVIDERS.map((p) => (
                <option key={p.value} value={p.value}>
                  {p.label}
                </option>
              ))}
            </select>
          </SettingsRow>

          {state.provider === "local" ? (
            <>
              <SettingsRow
                title="Engine"
                description={
                  state.localEngine === "parakeet"
                    ? "Parakeet TDT v3 — 25 European languages incl. Spanish / English, fastest. For Japanese / Chinese, use SenseVoice or a cloud provider."
                    : "SenseVoice — optimized for Chinese, Japanese, Korean, Cantonese, and English."
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
                      {eng.label}
                    </option>
                  ))}
                </select>
              </SettingsRow>
              <SettingsRow
                title="Local STT (sherpa-onnx)"
                description={
                  sttStatus?.present
                    ? "✓ sherpa-onnx is installed. Fast local transcription ready."
                    : "Not installed — local transcription won't work until you add [stt]."
                }
              >
                {sttStatus?.present ? (
                  <span className="text-[12px] text-emerald-600 dark:text-emerald-400">
                    installed
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
                    Install [stt]
                  </Button>
                )}
              </SettingsRow>
            </>
          ) : null}

          {state.provider === "groq" ? (
            <SettingsRow title="Groq API key" description="From console.groq.com (free tier available).">
              <ApiKeyInput
                value={state.groqApiKey}
                disabled={savingPath === "transcription.groq.api_key"}
                onSave={(v) => void onSave("transcription.groq.api_key", v)}
              />
            </SettingsRow>
          ) : null}

          {state.provider === "openai" ? (
            <SettingsRow title="OpenAI API key" description="From platform.openai.com.">
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
                title="Server base URL"
                description="e.g. http://localhost:8080/v1 (whisper.cpp, mlx-asr, vLLM)"
              >
                <TextRow
                  value={state.httpBaseUrl}
                  placeholder="http://localhost:8080/v1"
                  disabled={savingPath === "transcription.http.base_url"}
                  onSave={(v) => void onSave("transcription.http.base_url", v)}
                />
              </SettingsRow>
              <SettingsRow title="Server model name" description="Model id the server exposes (optional).">
                <TextRow
                  value={state.httpModel}
                  placeholder="whisper-large-v3"
                  disabled={savingPath === "transcription.http.model"}
                  onSave={(v) => void onSave("transcription.http.model", v)}
                />
              </SettingsRow>
              <SettingsRow title="Server API key" description="Only if the server requires auth.">
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
        <SettingsSectionTitle>Behavior</SettingsSectionTitle>
        <SettingsGroup>
          <SettingsRow
            title="Enabled"
            description="Master switch — when off, audio is never transcribed."
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
            title="Mode"
            description="Auto inserts text you can edit; Preview requires explicit accept; Off attaches raw audio."
          >
            <select
              value={state.mode}
              onChange={(e) => void onSave("transcription.mode", e.target.value)}
              disabled={savingPath === "transcription.mode"}
              className="h-8 rounded-full border bg-background px-3 text-[13px]"
            >
              {MODES.map((m) => (
                <option key={m.value} value={m.value}>
                  {m.label}
                </option>
              ))}
            </select>
          </SettingsRow>
          <SettingsRow
            title="Language hint"
            description="ISO-639-1 code (es, en, ja, zh…). Empty = auto-detect."
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
        {visible ? "hide" : "show"}
      </Button>
      <Button
        size="sm"
        variant="outline"
        disabled={!dirty || disabled}
        onClick={() => onSave(draft)}
        className="rounded-full"
      >
        save
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
        save
      </Button>
    </div>
  );
}

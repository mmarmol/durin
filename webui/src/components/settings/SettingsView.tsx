import { useCallback, useEffect, useMemo, useRef, useState, type Dispatch, type ReactNode, type SetStateAction } from "react";
import {
  Bot,
  Brain,
  ChevronLeft,
  ChevronDown,
  Check,
  Clock,
  Cloud,
  Cpu,
  Database,
  Eye,
  EyeOff,
  Pencil,
  Gem,
  Globe,
  Grid3X3,
  Hexagon,
  Loader2,
  Lock,
  LogOut,
  KeyRound,
  Layers,
  MessagesSquare,
  Plus,
  Trash2,
  Moon,
  Orbit,
  RotateCcw,
  ScrollText,
  Settings,
  ShieldCheck,
  Sliders,
  Sparkles,
  Triangle,
  Waves,
  Zap,
  type LucideIcon,
} from "lucide-react";
import { useTranslation } from "react-i18next";

import { LanguageSwitcher } from "@/components/LanguageSwitcher";
import { Button } from "@/components/ui/button";
import { CodexOAuthCard } from "@/components/settings/CodexOAuthCard";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { Input } from "@/components/ui/input";
import {
  deleteSecret,
  fetchSettings,
  getConfig,
  getModelCapabilities,
  listSecrets,
  setConfigValue,
  testModel,
  updateProviderSettings,
  updateSettings,
  updateWebSearchSettings,
  type ModelCapabilities,
  type ModelTestResult,
} from "@/lib/api";
import { ChannelsSettings } from "@/components/settings/ChannelsSettings";
import { ConfigSettings } from "@/components/settings/ConfigSettings";
import { CronSettings } from "@/components/settings/CronSettings";
import { LogsSettings } from "@/components/settings/LogsSettings";
import { MemorySettings } from "@/components/settings/MemorySettings";
import { SkillsSecuritySettings } from "@/components/settings/SkillsSecuritySettings";
import { ModelPicker } from "@/components/settings/ModelPicker";
import {
  SettingsGroup,
  SettingsRow,
  SettingsSectionTitle,
} from "@/components/settings/primitives";
import { PALETTES, type Palette } from "@/hooks/useTheme";
import { cn } from "@/lib/utils";
import { ConnectionBadge } from "@/components/ConnectionBadge";
import { useClient } from "@/providers/ClientProvider";
import type { SecretEntry, SettingsPayload, WebSearchSettingsUpdate } from "@/lib/types";

type SettingsSectionKey =
  | "general"
  | "providers"
  | "web-search"
  | "channels"
  | "memory"
  | "skills-security"
  | "cron"
  | "secrets"
  | "advanced"
  | "logs";
type ByokPaneKey = "llm" | "web-search";

interface SettingsViewProps {
  theme: "light" | "dark";
  onToggleTheme: () => void;
  palette: Palette;
  onSelectPalette: (palette: Palette) => void;
  onBackToChat: () => void;
  onModelNameChange: (modelName: string | null) => void;
  onLogout?: () => void;
  onRestart?: () => void;
  isRestarting?: boolean;
}

export function SettingsView({
  theme,
  onToggleTheme,
  palette,
  onSelectPalette,
  onBackToChat,
  onModelNameChange,
  onLogout,
  onRestart,
  isRestarting = false,
}: SettingsViewProps) {
  const { t } = useTranslation();
  const { token } = useClient();
  const [settings, setSettings] = useState<SettingsPayload | null>(null);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [providerSaving, setProviderSaving] = useState<string | null>(null);
  const [webSearchSaving, setWebSearchSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [activeSection, setActiveSection] = useState<SettingsSectionKey>("general");
  const [expandedProvider, setExpandedProvider] = useState<string | null>(null);
  const [providerForms, setProviderForms] = useState<Record<string, { apiKey: string; apiBase: string }>>({});
  const [visibleProviderKeys, setVisibleProviderKeys] = useState<Record<string, boolean>>({});
  const [editingProviderKeys, setEditingProviderKeys] = useState<Record<string, boolean>>({});
  const [webSearchForm, setWebSearchForm] = useState<WebSearchSettingsUpdate>({
    provider: "duckduckgo",
    apiKey: "",
    baseUrl: "",
  });
  const [webSearchKeyVisible, setWebSearchKeyVisible] = useState(false);
  const [webSearchKeyEditing, setWebSearchKeyEditing] = useState(false);
  const [form, setForm] = useState({
    model: "",
    provider: "",
  });

  const applyPayload = useCallback((payload: SettingsPayload) => {
    setSettings(payload);
    setForm({
      model: payload.agent.model,
      provider: payload.agent.provider,
    });
    setWebSearchForm((prev) => ({
      provider: payload.web_search.provider,
      apiKey: prev.provider === payload.web_search.provider ? prev.apiKey ?? "" : "",
      baseUrl: payload.web_search.base_url ?? "",
    }));
  }, []);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    fetchSettings(token)
      .then((payload) => {
        if (!cancelled) {
          applyPayload(payload);
          setError(null);
        }
      })
      .catch((err) => {
        if (!cancelled) setError((err as Error).message);
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [applyPayload, token]);

  useEffect(() => {
    if (!settings) return;
    setProviderForms((prev) => {
      const next = { ...prev };
      for (const provider of settings.providers) {
        next[provider.name] = {
          apiKey: next[provider.name]?.apiKey ?? "",
          apiBase: next[provider.name]?.apiBase ?? provider.api_base ?? provider.default_api_base ?? "",
        };
      }
      return next;
    });
  }, [settings]);

  const dirty = useMemo(() => {
    if (!settings) return false;
    return (
      form.model !== settings.agent.model ||
      form.provider !== settings.agent.provider
    );
  }, [form, settings]);

  const save = async () => {
    if (!dirty || saving) return;
    setSaving(true);
    try {
      const payload = await updateSettings(token, {
        model: form.model,
        ...(form.provider ? { provider: form.provider } : {}),
      });
      applyPayload(payload);
      onModelNameChange(payload.agent.model || null);
      setError(null);
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setSaving(false);
    }
  };

  const saveProvider = async (providerName: string) => {
    if (providerSaving) return;
    const provider = settings?.providers.find((item) => item.name === providerName);
    if (!provider) return;
    const providerForm = providerForms[providerName] ?? { apiKey: "", apiBase: "" };
    const apiKey = providerForm.apiKey.trim();
    if (!provider.configured && !apiKey) {
      setError(t("settings.byok.apiKeyRequired"));
      return;
    }
    setProviderSaving(providerName);
    try {
      const payload = await updateProviderSettings(token, {
        provider: providerName,
        apiKey: apiKey || undefined,
        apiBase: providerForm.apiBase.trim(),
      });
      applyPayload(payload);
      setProviderForms((prev) => ({
        ...prev,
        [providerName]: {
          apiKey: "",
          apiBase: providerForm.apiBase.trim(),
        },
      }));
      setVisibleProviderKeys((prev) => ({ ...prev, [providerName]: false }));
      setEditingProviderKeys((prev) => ({ ...prev, [providerName]: false }));
      setError(null);
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setProviderSaving(null);
    }
  };

  const saveWebSearch = async () => {
    if (!settings || webSearchSaving) return;
    const provider = settings.web_search.providers.find((item) => item.name === webSearchForm.provider);
    if (!provider) return;
    const apiKey = webSearchForm.apiKey?.trim() ?? "";
    const baseUrl = webSearchForm.baseUrl?.trim() ?? "";
    const hasExistingSecret =
      provider.credential === "api_key" &&
      webSearchForm.provider === settings.web_search.provider &&
      !!settings.web_search.api_key_hint;

    if (provider.credential === "api_key" && !apiKey && !hasExistingSecret) {
      setError(t("settings.byok.webSearch.apiKeyRequired"));
      return;
    }
    if (provider.credential === "base_url" && !baseUrl) {
      setError(t("settings.byok.webSearch.baseUrlRequired"));
      return;
    }

    setWebSearchSaving(true);
    try {
      const update: WebSearchSettingsUpdate = { provider: webSearchForm.provider };
      if (provider.credential === "api_key" && apiKey) update.apiKey = apiKey;
      if (provider.credential === "base_url") update.baseUrl = baseUrl;
      const payload = await updateWebSearchSettings(token, update);
      applyPayload(payload);
      setWebSearchForm((prev) => ({
        provider: payload.web_search.provider,
        apiKey: "",
        baseUrl: payload.web_search.base_url ?? prev.baseUrl ?? "",
      }));
      setWebSearchKeyVisible(false);
      setWebSearchKeyEditing(false);
      setError(null);
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setWebSearchSaving(false);
    }
  };

  const resetProviderDraft = useCallback((providerName: string) => {
    const provider = settings?.providers.find((item) => item.name === providerName);
    if (!provider) return;
    setProviderForms((prev) => ({
      ...prev,
      [providerName]: {
        apiKey: "",
        apiBase: provider.api_base ?? provider.default_api_base ?? "",
      },
    }));
    setVisibleProviderKeys((prev) => ({ ...prev, [providerName]: false }));
    setEditingProviderKeys((prev) => ({ ...prev, [providerName]: false }));
  }, [settings]);

  const handleToggleProvider = useCallback((providerName: string) => {
    if (expandedProvider) resetProviderDraft(expandedProvider);
    setExpandedProvider(expandedProvider === providerName ? null : providerName);
  }, [expandedProvider, resetProviderDraft]);

  const resetWebSearchDraft = useCallback(() => {
    if (!settings) return;
    setWebSearchForm({
      provider: settings.web_search.provider,
      apiKey: "",
      baseUrl: settings.web_search.base_url ?? "",
    });
    setWebSearchKeyVisible(false);
    setWebSearchKeyEditing(false);
  }, [settings]);

  const handleWebSearchProviderChange = useCallback((provider: string) => {
    if (!settings) return;
    setWebSearchForm({
      provider,
      apiKey: "",
      baseUrl: provider === settings.web_search.provider ? settings.web_search.base_url ?? "" : "",
    });
    setWebSearchKeyVisible(false);
    setWebSearchKeyEditing(false);
  }, [settings]);

  const toggleProviderKeyVisibility = (providerName: string) => {
    const isVisible = visibleProviderKeys[providerName];
    setVisibleProviderKeys((prev) => ({ ...prev, [providerName]: !isVisible }));
  };

  const toggleProviderKeyEditing = (providerName: string) => {
    setEditingProviderKeys((prev) => {
      const nextEditing = !prev[providerName];
      if (!nextEditing) {
        setProviderForms((forms) => ({
          ...forms,
          [providerName]: {
            apiKey: "",
            apiBase: forms[providerName]?.apiBase ?? "",
          },
        }));
        setVisibleProviderKeys((visible) => ({ ...visible, [providerName]: false }));
      }
      return { ...prev, [providerName]: nextEditing };
    });
  };

  return (
    <div className="flex min-h-0 flex-1 overflow-hidden bg-[radial-gradient(circle_at_50%_0%,hsl(var(--muted))_0%,hsl(var(--background))_42%)]">
      <SettingsSidebar
        activeSection={activeSection}
        onSelectSection={setActiveSection}
        onBackToChat={onBackToChat}
        onLogout={onLogout}
      />

      <main className="min-w-0 flex-1 overflow-y-auto [scrollbar-gutter:stable]">
        <div className="mx-auto w-full max-w-[840px] px-6 py-10 sm:px-10 lg:py-14">
          <div className="mb-8">
            <p className="mb-2 text-[13px] font-medium text-muted-foreground">
              {t("settings.sidebar.title")}
            </p>
            <h1 className="text-[28px] font-semibold leading-tight tracking-[-0.035em] text-foreground sm:text-[34px]">
              {t(`settings.nav.${activeSection}`)}
            </h1>
          </div>

          {loading ? (
            <div className="flex h-48 items-center justify-center rounded-[24px] border border-border/50 bg-card/75 text-sm text-muted-foreground shadow-[0_20px_70px_rgba(15,23,42,0.07)]">
              <Loader2 className="mr-2 h-4 w-4 animate-spin" />
              {t("settings.status.loading")}
            </div>
          ) : error && !settings ? (
            <SettingsGroup>
              <SettingsRow title={t("settings.status.loadError")}>
                <span className="max-w-[520px] text-sm text-muted-foreground">{error}</span>
              </SettingsRow>
            </SettingsGroup>
          ) : settings ? (
            <div className="space-y-5">
              {error ? (
                <div className="rounded-[18px] border border-destructive/20 bg-destructive/5 px-4 py-3 text-[13px] text-destructive">
                  {error}
                </div>
              ) : null}
              {activeSection === "general" ? (
                <GeneralSettings
                  theme={theme}
                  onToggleTheme={onToggleTheme}
                  palette={palette}
                  onSelectPalette={onSelectPalette}
                  form={form}
                  setForm={setForm}
                  settings={settings}
                  dirty={dirty}
                  saving={saving}
                  onSave={save}
                  onRestart={onRestart}
                  isRestarting={isRestarting}
                  onOpenByok={() => setActiveSection("providers")}
                  token={token}
                />
              ) : activeSection === "channels" ? (
                <ChannelsSettings token={token} />
              ) : activeSection === "memory" ? (
                <MemorySettings token={token} />
              ) : activeSection === "skills-security" ? (
                <SkillsSecuritySettings token={token} />
              ) : activeSection === "cron" ? (
                <CronSettings token={token} />
              ) : activeSection === "secrets" ? (
                <SecretsSettings token={token} />
              ) : activeSection === "advanced" ? (
                <ConfigSettings token={token} />
              ) : activeSection === "logs" ? (
                <LogsSettings token={token} />
              ) : (
                <ByokSettings
                  forcePane={activeSection === "web-search" ? "web-search" : "llm"}
                  settings={settings}
                  expandedProvider={expandedProvider}
                  providerForms={providerForms}
                  visibleProviderKeys={visibleProviderKeys}
                  editingProviderKeys={editingProviderKeys}
                  providerSaving={providerSaving}
                  webSearchForm={webSearchForm}
                  webSearchKeyVisible={webSearchKeyVisible}
                  webSearchKeyEditing={webSearchKeyEditing}
                  webSearchSaving={webSearchSaving}
                  onToggleProvider={handleToggleProvider}
                  onToggleProviderKey={toggleProviderKeyVisibility}
                  onToggleProviderKeyEditing={toggleProviderKeyEditing}
                  onChangeProviderForm={(provider, value) =>
                    setProviderForms((prev) => ({
                      ...prev,
                      [provider]: {
                        apiKey: prev[provider]?.apiKey ?? "",
                        apiBase: prev[provider]?.apiBase ?? "",
                        ...value,
                      },
                    }))
                  }
                  onSaveProvider={saveProvider}
                  onRefreshSettings={() => {
                    fetchSettings(token).then(applyPayload).catch(() => {});
                  }}
                  onChangeWebSearchForm={setWebSearchForm}
                  onChangeWebSearchProvider={handleWebSearchProviderChange}
                  onToggleWebSearchKey={() => setWebSearchKeyVisible((visible) => !visible)}
                  onToggleWebSearchKeyEditing={() => {
                    setWebSearchKeyEditing((editing) => !editing);
                    setWebSearchKeyVisible(false);
                    setWebSearchForm((prev) => ({ ...prev, apiKey: "" }));
                  }}
                  onResetProviderDraft={resetProviderDraft}
                  onResetWebSearchDraft={resetWebSearchDraft}
                  onSaveWebSearch={saveWebSearch}
                />
              )}
            </div>
          ) : null}
        </div>
      </main>
    </div>
  );
}

const SETTINGS_NAV_ITEMS = [
  { key: "general", icon: Settings },
  { key: "providers", icon: KeyRound },
  { key: "web-search", icon: Globe },
  { key: "channels", icon: MessagesSquare },
  { key: "memory", icon: Brain },
  { key: "skills-security", icon: ShieldCheck },
  { key: "cron", icon: Clock },
  { key: "secrets", icon: Lock },
  { key: "advanced", icon: Sliders },
  { key: "logs", icon: ScrollText },
] as const;

function SettingsSidebar({
  activeSection,
  onSelectSection,
  onBackToChat,
  onLogout,
}: {
  activeSection: SettingsSectionKey;
  onSelectSection: (section: SettingsSectionKey) => void;
  onBackToChat: () => void;
  onLogout?: () => void;
}) {
  const { t } = useTranslation();
  return (
    <aside className="flex w-[17rem] shrink-0 flex-col border-r border-border/55 bg-card/62 px-3 py-4 shadow-[inset_-1px_0_0_rgba(255,255,255,0.55)] backdrop-blur-xl dark:bg-card/45 dark:shadow-none">
      <button
        type="button"
        onClick={onBackToChat}
        className="mb-3 inline-flex w-fit items-center gap-1.5 rounded-full px-2.5 py-1.5 text-[12px] font-medium text-muted-foreground transition-colors hover:bg-muted/70 hover:text-foreground"
      >
        <ChevronLeft className="h-3.5 w-3.5" aria-hidden />
        {t("settings.backToChat")}
      </button>
      <div className="mb-5 px-2">
        <h2 className="text-[21px] font-semibold tracking-[-0.035em] text-foreground">
          {t("settings.sidebar.title")}
        </h2>
      </div>

      <nav aria-label={t("settings.sidebar.ariaLabel")} className="space-y-1">
        {SETTINGS_NAV_ITEMS.map(({ key, icon: Icon }) => {
          const active = key === activeSection;
          return (
            <button
              key={key}
              type="button"
              aria-current={active ? "page" : undefined}
              onClick={() => onSelectSection(key)}
              className={cn(
                "flex h-9 w-full items-center gap-2 rounded-[10px] px-2.5 text-left text-[13px] font-medium transition-colors",
                active
                  ? "bg-muted/90 text-foreground shadow-[inset_0_0_0_1px_rgba(0,0,0,0.025)]"
                  : "text-muted-foreground/78 hover:bg-muted/45 hover:text-foreground",
              )}
            >
              <Icon className="h-4 w-4 shrink-0" strokeWidth={2} aria-hidden />
              <span className="truncate">{t(`settings.nav.${key}`)}</span>
            </button>
          );
        })}
      </nav>

      <div className="mt-auto pt-4">
        <div className="px-2 pb-2">
          <ConnectionBadge />
        </div>
        {onLogout ? (
          <Button
            type="button"
            variant="ghost"
            onClick={onLogout}
            className="h-9 w-full justify-start gap-2 rounded-[10px] px-2.5 text-[13px] font-medium text-muted-foreground hover:bg-destructive/8 hover:text-destructive"
          >
            <LogOut className="h-4 w-4" aria-hidden />
            {t("app.account.logout")}
          </Button>
        ) : null}
      </div>
    </aside>
  );
}

function GeneralSettings({
  theme,
  onToggleTheme,
  palette,
  onSelectPalette,
  form,
  setForm,
  settings,
  dirty,
  saving,
  onSave,
  onRestart,
  isRestarting,
  onOpenByok,
  token,
}: {
  theme: "light" | "dark";
  onToggleTheme: () => void;
  palette: Palette;
  onSelectPalette: (palette: Palette) => void;
  form: {
    model: string;
    provider: string;
  };
  setForm: Dispatch<SetStateAction<{
    model: string;
    provider: string;
  }>>;
  settings: SettingsPayload;
  dirty: boolean;
  saving: boolean;
  onSave: () => void;
  onRestart?: () => void;
  isRestarting?: boolean;
  onOpenByok: () => void;
  token: string;
}) {
  const { t } = useTranslation();
  const configuredProviders = settings.providers.filter((provider) => provider.configured);
  const providerValue = configuredProviders.some((provider) => provider.name === form.provider)
    ? form.provider
    : "";
  return (
    <div className="space-y-8">
      <section>
        <SettingsSectionTitle>{t("settings.sections.interface")}</SettingsSectionTitle>
        <SettingsGroup>
          <SettingsRow
            title={t("settings.rows.theme")}
            description={t("settings.help.theme")}
          >
            <button
              type="button"
              onClick={onToggleTheme}
              className="inline-flex h-8 items-center rounded-full bg-muted p-0.5 text-[12px] font-medium text-muted-foreground"
            >
              <span
                className={cn(
                  "rounded-full px-3 py-1 transition-colors",
                  theme === "light" && "bg-background text-foreground shadow-sm",
                )}
              >
                {t("settings.values.light")}
              </span>
              <span
                className={cn(
                  "rounded-full px-3 py-1 transition-colors",
                  theme === "dark" && "bg-background text-foreground shadow-sm",
                )}
              >
                {t("settings.values.dark")}
              </span>
            </button>
          </SettingsRow>

          <SettingsRow
            title={t("settings.rows.palette")}
            description={t("settings.help.palette")}
          >
            <div className="inline-flex h-8 items-center rounded-full bg-muted p-0.5 text-[12px] font-medium text-muted-foreground">
              {PALETTES.map((option) => (
                <button
                  key={option}
                  type="button"
                  onClick={() => onSelectPalette(option)}
                  className={cn(
                    "inline-flex items-center gap-1.5 rounded-full px-3 py-1 capitalize transition-colors",
                    palette === option &&
                      "bg-background text-foreground shadow-sm",
                  )}
                >
                  {/* Swatch previews each palette's own accent: a nested
                      data-palette element resolves that palette's --primary
                      token, so the dot stays in sync with the design system
                      instead of hardcoding hex values. */}
                  <span
                    data-palette={option}
                    aria-hidden
                    className="h-2.5 w-2.5 rounded-full ring-1 ring-black/10"
                    style={{ backgroundColor: "hsl(var(--primary))" }}
                  />
                  {option}
                </button>
              ))}
            </div>
          </SettingsRow>

          <SettingsRow
            title={t("settings.rows.language")}
            description={t("settings.help.language")}
          >
            <LanguageSwitcher />
          </SettingsRow>
        </SettingsGroup>
      </section>

      <section>
        <SettingsSectionTitle>{t("settings.sections.ai")}</SettingsSectionTitle>
        <SettingsGroup>
          <SettingsRow
            title={t("settings.rows.provider")}
            description={t("settings.help.provider")}
          >
            <ProviderPicker
              providers={configuredProviders}
              value={providerValue}
              emptyLabel={t("settings.byok.noConfiguredProviders")}
              onChange={(provider) => setForm((prev) => ({ ...prev, provider }))}
            />
          </SettingsRow>

          <SettingsRow
            title={t("settings.rows.model")}
            description={t("settings.help.model")}
          >
            <div className="flex flex-col items-end gap-2">
              <ModelPicker
                token={token}
                provider={form.provider}
                value={form.model}
                onChange={(model) => setForm((prev) => ({ ...prev, model }))}
              />
              <ModelTestInline
                token={token}
                model={form.model}
                provider={form.provider}
              />
            </div>
          </SettingsRow>

          <ModelBlockRows
            token={token}
            model={form.model}
            provider={form.provider}
            configuredProviders={configuredProviders}
          />

          {(dirty || saving || settings.requires_restart) ? (
            <SettingsFooter
              dirty={dirty}
              saving={saving}
              saved={settings.requires_restart && !dirty}
              onSave={onSave}
            />
          ) : null}
          {configuredProviders.length === 0 ? (
            <SettingsRow title={t("settings.byok.configureFirst")}>
              <Button size="sm" variant="outline" onClick={onOpenByok} className="rounded-full">
                {t("settings.byok.openByok")}
              </Button>
            </SettingsRow>
          ) : null}
        </SettingsGroup>
      </section>

      {onRestart && (
        <section>
          <SettingsSectionTitle>{t("settings.sections.system")}</SettingsSectionTitle>
          <SettingsGroup>
            <SettingsRow
              title={t("settings.rows.restart")}
              description={t("app.system.restartHint")}
            >
              <Button
                size="sm"
                variant="outline"
                onClick={onRestart}
                disabled={isRestarting}
                className="rounded-full"
              >
                {isRestarting ? (
                  <Loader2 className="mr-1.5 h-3.5 w-3.5 animate-spin" aria-hidden />
                ) : (
                  <RotateCcw className="mr-1.5 h-3.5 w-3.5" aria-hidden />
                )}
                {isRestarting ? t("app.system.restarting") : t("app.system.restart")}
              </Button>
            </SettingsRow>
            <SettingsRow
              title={t("settings.rows.configPath")}
              description={t("settings.help.configPath")}
            >
              <span className="max-w-[260px] truncate text-right text-[13px] text-muted-foreground">
                {settings.runtime.config_path || t("settings.values.notAvailable")}
              </span>
            </SettingsRow>
          </SettingsGroup>
        </section>
      )}
    </div>
  );
}

interface AuxModel {
  model: string;
  provider: string;
}

function readAux(config: Record<string, unknown> | null, kind: string): AuxModel | null {
  const agents = config?.agents as Record<string, unknown> | undefined;
  const aux = agents?.auxModels as Record<string, unknown> | undefined;
  const entry = aux?.[kind] as Record<string, unknown> | undefined;
  if (!entry || typeof entry.model !== "string") return null;
  return {
    model: entry.model,
    provider: typeof entry.provider === "string" ? entry.provider : "auto",
  };
}

function modelCapsSummary(
  caps: ModelCapabilities | null,
  t: (key: string, opts?: Record<string, unknown>) => string,
): string {
  if (!caps) return t("settings.models.capsUnknown");
  const parts = [t("settings.models.capsText")];
  parts.push(`${t("settings.models.capsVision")} ${caps.supports_vision ? "✓" : "✗"}`);
  parts.push(`${t("settings.models.capsAudio")} ${caps.supports_audio_input ? "✓" : "✗"}`);
  if (caps.max_input_tokens && caps.max_input_tokens > 0) {
    const k = Math.round(caps.max_input_tokens / 1000);
    parts.push(t("settings.models.capsContext", { tokens: `${k}K` }));
  }
  return parts.join(" · ");
}

/** Compact vision/audio aux-model editor — provider dropdown +
 *  model autocomplete + save.
 *
 *  Provider: REQUIRED dropdown of providers the user has already
 *  configured (API keys present). No "Auto" option — the operator
 *  picks the provider explicitly. Legacy configs that carry
 *  ``provider: "auto"`` render as "no selection" and stay unsaveable
 *  until the user picks one, prompting migration.
 *
 *  Model: ModelPicker (autocomplete + free input) filtered server-side
 *  by the selected provider and by ``capability`` so the vision aux
 *  picker only surfaces vision-capable models, audio aux only audio,
 *  etc. Save is disabled until both provider and model are set.
 */
function AuxControl({
  current,
  busy,
  onSave,
  onClear,
  configuredProviders,
  token,
  capability,
}: {
  current: AuxModel | null;
  busy: boolean;
  onSave: (value: AuxModel) => void;
  onClear: () => void;
  configuredProviders: Array<{ name: string; label: string }>;
  token: string;
  capability: string;
}) {
  const { t } = useTranslation();
  // Treat legacy "auto" as no selection so the user is prompted to
  // pick a real provider on next save.
  const initialProv = current?.provider && current.provider !== "auto"
    ? current.provider : "";
  const [model, setModel] = useState(current?.model ?? "");
  const [prov, setProv] = useState(initialProv);
  const [testing, setTesting] = useState(false);
  const [test, setTest] = useState<ModelTestResult | null>(null);
  const [caps, setCaps] = useState<ModelCapabilities | null>(null);
  useEffect(() => {
    setModel(current?.model ?? "");
    setProv(
      current?.provider && current.provider !== "auto" ? current.provider : "",
    );
    // Clear any prior result when the row's underlying config changes
    // (e.g. another save lands or the row is cleared) so a stale ✓ /
    // ✗ badge doesn't claim the new combo was tested.
    setTest(null);
  }, [current]);
  // Fetch capabilities for the picked combo so the operator sees what
  // the model supports (vision/audio/context size) without leaving the
  // row — same info the main model's "Capacidades" row surfaces.
  useEffect(() => {
    if (!model.trim() || !prov.trim()) {
      setCaps(null);
      return;
    }
    let cancelled = false;
    getModelCapabilities(token, model.trim(), prov.trim())
      .then((c) => {
        if (!cancelled) setCaps(c);
      })
      .catch(() => {
        if (!cancelled) setCaps(null);
      });
    return () => {
      cancelled = true;
    };
  }, [token, model, prov]);
  const dirty =
    model.trim() !== (current?.model ?? "") ||
    prov.trim() !== (current?.provider && current.provider !== "auto"
      ? current.provider : "");
  const runTest = async () => {
    if (!model.trim() || !prov.trim()) return;
    setTesting(true);
    setTest(null);
    try {
      // Pass model+provider explicitly so the endpoint tests the
      // in-flight combo even when the user hasn't saved yet — gives
      // immediate feedback on whether a pick will work before
      // committing it to config.
      setTest(
        await testModel(token, {
          model: model.trim(),
          provider: prov.trim(),
        }),
      );
    } catch {
      setTest({
        status: "fail",
        message: t("settings.models.testError"),
        fix: "",
      });
    } finally {
      setTesting(false);
    }
  };
  // Two-row layout: pickers on top (full-width on narrow, inline on
  // wide), action buttons + result badge on the bottom row. Keeps the
  // SettingsRow's title/description column from being squeezed when
  // five controls share a single line.
  return (
    <div className="flex flex-col items-end gap-2">
      <div className="flex flex-wrap items-center justify-end gap-2">
        <ProviderPicker
          providers={configuredProviders}
          value={prov}
          emptyLabel={t("settings.models.pickProvider")}
          onChange={setProv}
        />
        <ModelPicker
          token={token}
          provider={prov}
          value={model}
          onChange={setModel}
          capability={capability}
        />
      </div>
      <div className="flex flex-wrap items-center justify-end gap-2">
        {test ? (
          <span
            className={cn(
              "text-[12px]",
              test.status === "ok" ? "text-emerald-600" : "text-destructive",
            )}
            title={test.message}
          >
            {test.status === "ok" ? "✓ " : "✗ "}
            <span className="truncate max-w-[220px] inline-block align-bottom">
              {test.message}
            </span>
          </span>
        ) : null}
        <Button
          size="sm"
          variant="outline"
          disabled={!dirty || busy || !model.trim() || !prov.trim()}
          onClick={() => onSave({ model: model.trim(), provider: prov.trim() })}
          className="rounded-full"
        >
          {t("settings.models.save")}
        </Button>
        <Button
          size="sm"
          variant="ghost"
          disabled={testing || busy || !model.trim() || !prov.trim()}
          onClick={() => void runTest()}
          className="rounded-full"
          title={t("settings.models.testRowHint")}
        >
          {testing ? t("settings.models.testing") : t("settings.models.testRow")}
        </Button>
        {current ? (
          <Button
            size="sm"
            variant="ghost"
            disabled={busy}
            onClick={onClear}
            className="rounded-full text-muted-foreground"
          >
            {t("settings.models.clear")}
          </Button>
        ) : null}
      </div>
      {caps && model.trim() && prov.trim() ? (
        <span className="text-[11px] text-muted-foreground">
          {modelCapsSummary(caps, t)}
        </span>
      ) : null}
    </div>
  );
}

/** Inline test button + result for the main model — rendered next to
 *  the ModelPicker in the "Modelo" row so the action sits with what it
 *  acts on (same shape as each AuxControl row). Replaces the standalone
 *  "Prueba" SettingsRow that used to live at the end of the AI block.
 */
function ModelTestInline({
  token,
  model,
  provider,
}: {
  token: string;
  model: string;
  provider: string;
}) {
  const { t } = useTranslation();
  const [testing, setTesting] = useState(false);
  const [test, setTest] = useState<ModelTestResult | null>(null);
  // Clear any prior result when the user changes the model/provider so
  // a stale ✓ doesn't claim the new combo was tested.
  useEffect(() => {
    setTest(null);
  }, [model, provider]);
  const runTest = async () => {
    if (!model.trim()) return;
    setTesting(true);
    setTest(null);
    try {
      setTest(await testModel(token, { model: model.trim(), provider: provider.trim() }));
    } catch {
      setTest({ status: "fail", message: t("settings.models.testError"), fix: "" });
    } finally {
      setTesting(false);
    }
  };
  return (
    <div className="flex flex-wrap items-center justify-end gap-2">
      {test ? (
        <span
          className={cn(
            "text-[12px]",
            test.status === "ok" ? "text-emerald-600" : "text-destructive",
          )}
          title={test.message}
        >
          {test.status === "ok" ? "✓ " : "✗ "}
          <span className="truncate max-w-[220px] inline-block align-bottom">
            {test.message}
          </span>
        </span>
      ) : null}
      <Button
        size="sm"
        variant="ghost"
        disabled={testing || !model.trim()}
        onClick={() => void runTest()}
        className="rounded-full"
        title={t("settings.models.testRowHint")}
      >
        {testing ? t("settings.models.testing") : t("settings.models.testRow")}
      </Button>
    </div>
  );
}

/** The model rows of the AI block: capabilities, vision/audio aux
 *  models. The main-model test sits in the parent SettingsRow next to
 *  the ModelPicker (see ModelTestInline) — keeping it close to what it
 *  acts on. */
/** Collapsible "Advanced" row exposing main-model knobs that don't fit
 *  the primary picker rows: context window cap, temperature, max output
 *  tokens, reasoning effort. Reads + writes individual paths in
 *  `agents.defaults.*` via the generic `setConfigValue` API — same
 *  contract as a `durin config set` invocation.
 *
 *  All four are optional / overridable: empty input clears the override
 *  (config returns to schema default). Numeric inputs bound by the
 *  schema (ge=, le=) — invalid values surface as a save error inline.
 */
function AdvancedModelRow({ token }: { token: string }) {
  const { t } = useTranslation();
  const [open, setOpen] = useState(false);
  const [values, setValues] = useState<{
    contextWindowTokens: string;
    temperature: string;
    maxTokens: string;
    reasoningEffort: string;
  }>({ contextWindowTokens: "", temperature: "", maxTokens: "", reasoningEffort: "" });
  const [saving, setSaving] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const loadValues = useCallback(async () => {
    try {
      const cfg = (await getConfig(token)).config;
      const agents = cfg.agents as Record<string, unknown> | undefined;
      const def = (agents?.defaults ?? {}) as Record<string, unknown>;
      setValues({
        contextWindowTokens: def.contextWindowTokens != null ? String(def.contextWindowTokens) : "",
        temperature: def.temperature != null ? String(def.temperature) : "",
        maxTokens: def.maxTokens != null ? String(def.maxTokens) : "",
        reasoningEffort: typeof def.reasoningEffort === "string" ? def.reasoningEffort : "",
      });
    } catch {
      // leave inputs blank on failure
    }
  }, [token]);
  useEffect(() => {
    if (open) void loadValues();
  }, [open, loadValues]);

  const saveOne = async (path: string, raw: string, parse: (s: string) => unknown) => {
    setSaving(path);
    setError(null);
    try {
      const trimmed = raw.trim();
      const value = trimmed === "" ? null : parse(trimmed);
      await setConfigValue(token, `agents.defaults.${path}`, value);
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setSaving(null);
    }
  };

  return (
    <div className="border-t border-border/30 px-4 py-3.5 sm:px-5">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        className="flex w-full items-center justify-between text-[13px] font-medium text-muted-foreground hover:text-foreground"
      >
        <span>{t("settings.models.advanced")}</span>
        <span className="text-[11px]">{open ? "▾" : "▸"}</span>
      </button>
      {open ? (
        <div className="mt-3 grid grid-cols-1 gap-3 sm:grid-cols-2">
          <AdvancedField
            label={t("settings.models.contextWindow")}
            description={t("settings.models.contextWindowHint")}
            value={values.contextWindowTokens}
            onChange={(v) => setValues((s) => ({ ...s, contextWindowTokens: v }))}
            onSave={() =>
              void saveOne("contextWindowTokens", values.contextWindowTokens, (s) => {
                const n = Number(s);
                if (!Number.isFinite(n) || n <= 0) throw new Error("must be a positive integer");
                return Math.floor(n);
              })
            }
            saving={saving === "contextWindowTokens"}
            placeholder="202800"
            inputMode="numeric"
          />
          <AdvancedField
            label={t("settings.models.temperature")}
            description={t("settings.models.temperatureHint")}
            value={values.temperature}
            onChange={(v) => setValues((s) => ({ ...s, temperature: v }))}
            onSave={() =>
              void saveOne("temperature", values.temperature, (s) => {
                const n = Number(s);
                if (!Number.isFinite(n) || n < 0 || n > 2)
                  throw new Error("must be in [0, 2]");
                return n;
              })
            }
            saving={saving === "temperature"}
            placeholder="0.4"
            inputMode="decimal"
          />
          <AdvancedField
            label={t("settings.models.maxTokens")}
            description={t("settings.models.maxTokensHint")}
            value={values.maxTokens}
            onChange={(v) => setValues((s) => ({ ...s, maxTokens: v }))}
            onSave={() =>
              void saveOne("maxTokens", values.maxTokens, (s) => {
                const n = Number(s);
                if (!Number.isFinite(n) || n <= 0) throw new Error("must be a positive integer");
                return Math.floor(n);
              })
            }
            saving={saving === "maxTokens"}
            placeholder="8192"
            inputMode="numeric"
          />
          <AdvancedField
            label={t("settings.models.reasoningEffort")}
            description={t("settings.models.reasoningEffortHint")}
            value={values.reasoningEffort}
            onChange={(v) => setValues((s) => ({ ...s, reasoningEffort: v }))}
            onSave={() =>
              void saveOne("reasoningEffort", values.reasoningEffort, (s) => s)
            }
            saving={saving === "reasoningEffort"}
            placeholder="low | medium | high"
          />
          {error ? (
            <div className="col-span-full text-[12px] text-destructive">{error}</div>
          ) : null}
        </div>
      ) : null}
    </div>
  );
}

function AdvancedField({
  label,
  description,
  value,
  onChange,
  onSave,
  saving,
  placeholder,
  inputMode,
}: {
  label: string;
  description: string;
  value: string;
  onChange: (v: string) => void;
  onSave: () => void;
  saving: boolean;
  placeholder?: string;
  inputMode?: "numeric" | "decimal" | "text";
}) {
  const { t } = useTranslation();
  return (
    <div className="flex flex-col gap-1">
      <div className="text-[12px] font-medium">{label}</div>
      <div className="text-[11px] text-muted-foreground">{description}</div>
      <div className="flex items-center gap-2">
        <Input
          value={value}
          onChange={(e) => onChange(e.target.value)}
          placeholder={placeholder}
          inputMode={inputMode}
          className="h-8 rounded-full text-[13px]"
        />
        <Button
          size="sm"
          variant="outline"
          disabled={saving}
          onClick={onSave}
          className="rounded-full"
        >
          {saving ? t("settings.models.saving") : t("settings.models.save")}
        </Button>
      </div>
    </div>
  );
}

function ModelBlockRows({
  token,
  model,
  provider,
  configuredProviders,
}: {
  token: string;
  model: string;
  provider: string;
  configuredProviders: Array<{ name: string; label: string }>;
}) {
  const { t } = useTranslation();
  const [caps, setCaps] = useState<ModelCapabilities | null>(null);
  const [config, setConfig] = useState<Record<string, unknown> | null>(null);
  const [busy, setBusy] = useState<string | null>(null);

  useEffect(() => {
    if (!model) {
      setCaps(null);
      return;
    }
    let cancelled = false;
    getModelCapabilities(token, model, provider)
      .then((c) => {
        if (!cancelled) setCaps(c);
      })
      .catch(() => {
        if (!cancelled) setCaps(null);
      });
    return () => {
      cancelled = true;
    };
  }, [token, model, provider]);

  const loadConfig = useCallback(async () => {
    try {
      setConfig((await getConfig(token)).config);
    } catch {
      // leave aux rows empty on failure
    }
  }, [token]);
  useEffect(() => {
    void loadConfig();
  }, [loadConfig]);

  const saveAux = useCallback(
    async (kind: string, value: AuxModel | null) => {
      setBusy(kind);
      try {
        setConfig(await setConfigValue(token, `agents.auxModels.${kind}`, value));
      } catch {
        // ignore — the row keeps its previous value
      } finally {
        setBusy(null);
      }
    },
    [token],
  );

  return (
    <>
      <SettingsRow title={t("settings.models.capabilities")}>
        <span className="text-[12px] text-muted-foreground">
          {modelCapsSummary(caps, t)}
        </span>
      </SettingsRow>
      <AdvancedModelRow token={token} />
      <SettingsRow
        title={t("settings.models.vision")}
        description={t("settings.models.visionHint")}
      >
        <AuxControl
          current={readAux(config, "vision")}
          busy={busy === "vision"}
          onSave={(v) => void saveAux("vision", v)}
          onClear={() => void saveAux("vision", null)}
          configuredProviders={configuredProviders}
          token={token}
          capability="vision"
        />
      </SettingsRow>
      <SettingsRow
        title={t("settings.models.audio")}
        description={t("settings.models.audioHint")}
      >
        <AuxControl
          current={readAux(config, "audio")}
          busy={busy === "audio"}
          onSave={(v) => void saveAux("audio", v)}
          onClear={() => void saveAux("audio", null)}
          configuredProviders={configuredProviders}
          token={token}
          capability="audio"
        />
      </SettingsRow>
      <SettingsRow
        title={t("settings.models.memory")}
        description={t("settings.models.memoryHint")}
      >
        <AuxControl
          current={readAux(config, "memory")}
          busy={busy === "memory"}
          onSave={(v) => void saveAux("memory", v)}
          onClear={() => void saveAux("memory", null)}
          configuredProviders={configuredProviders}
          token={token}
          capability="text"
        />
      </SettingsRow>
    </>
  );
}

function ProviderPicker({
  providers,
  value,
  emptyLabel,
  onChange,
}: {
  providers: Array<{ name: string; label: string }>;
  value: string;
  emptyLabel: string;
  onChange: (provider: string) => void;
}) {
  const selectedProvider = providers.find((provider) => provider.name === value) ?? null;
  const disabled = providers.length === 0;

  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild disabled={disabled}>
        <Button
          type="button"
          variant="outline"
          disabled={disabled}
          className={cn(
            "h-8 w-[210px] justify-between rounded-full border-input bg-background px-3 text-[13px] font-normal shadow-none",
            "hover:bg-accent/55 focus-visible:ring-2 focus-visible:ring-ring",
            disabled && "text-muted-foreground",
          )}
        >
          <span className="truncate">{selectedProvider?.label ?? emptyLabel}</span>
          <ChevronDown className="ml-2 h-3.5 w-3.5 shrink-0 text-muted-foreground" aria-hidden />
        </Button>
      </DropdownMenuTrigger>
      <DropdownMenuContent
        align="end"
        className="max-h-[18rem] w-[240px] overflow-y-auto rounded-[18px] border-border/65 bg-popover p-1.5 text-popover-foreground shadow-[0_18px_55px_rgba(15,23,42,0.18)] dark:border-white/10 dark:shadow-[0_22px_55px_rgba(0,0,0,0.45)]"
      >
        {providers.map((provider) => {
          const selected = provider.name === value;
          return (
            <DropdownMenuItem
              key={provider.name}
              onSelect={() => onChange(provider.name)}
              className={cn(
                "flex cursor-default items-center justify-between gap-2 rounded-[12px] px-3 py-2 text-[13px]",
                "focus:bg-muted focus:text-foreground",
                selected && "bg-primary/10 text-primary focus:bg-primary/12 focus:text-primary",
              )}
            >
              <span className="truncate">{provider.label}</span>
              {selected ? <Check className="h-3.5 w-3.5 shrink-0" aria-hidden /> : null}
            </DropdownMenuItem>
          );
        })}
      </DropdownMenuContent>
    </DropdownMenu>
  );
}

function WebSearchByokSettings({
  settings,
  form,
  keyVisible,
  keyEditing,
  saving,
  onChangeForm,
  onChangeProvider,
  onToggleKey,
  onToggleKeyEditing,
  onSave,
}: {
  settings: SettingsPayload;
  form: WebSearchSettingsUpdate;
  keyVisible: boolean;
  keyEditing: boolean;
  saving: boolean;
  onChangeForm: Dispatch<SetStateAction<WebSearchSettingsUpdate>>;
  onChangeProvider: (provider: string) => void;
  onToggleKey: () => void;
  onToggleKeyEditing: () => void;
  onSave: () => void;
}) {
  const { t } = useTranslation();
  const selectedProvider =
    settings.web_search.providers.find((provider) => provider.name === form.provider) ??
    settings.web_search.providers[0];
  const hasExistingSecret =
    selectedProvider?.credential === "api_key" &&
    form.provider === settings.web_search.provider &&
    !!settings.web_search.api_key_hint;
  const showKeyInput = selectedProvider?.credential === "api_key" && (!hasExistingSecret || keyEditing);
  const apiKey = form.apiKey?.trim() ?? "";
  const baseUrl = form.baseUrl?.trim() ?? "";
  const dirty =
    form.provider !== settings.web_search.provider ||
    apiKey.length > 0 ||
    baseUrl !== (settings.web_search.base_url ?? "");
  const missingCredential =
    selectedProvider?.credential === "api_key"
      ? !apiKey && !hasExistingSecret
      : selectedProvider?.credential === "base_url"
        ? !baseUrl
        : false;

  return (
    <section className="space-y-4">
      <SettingsGroup>
        <SettingsRow
          title={t("settings.byok.webSearch.provider")}
          description={t("settings.byok.webSearch.providerHelp")}
        >
          <ProviderPicker
            providers={settings.web_search.providers}
            value={form.provider}
            emptyLabel={t("settings.byok.webSearch.selectProvider")}
            onChange={onChangeProvider}
          />
        </SettingsRow>

        {selectedProvider?.credential === "none" ? (
          <SettingsRow
            title={t("settings.byok.webSearch.credentials")}
            description={t("settings.byok.webSearch.noCredentialHelp")}
          >
            <span className="rounded-full bg-emerald-500/10 px-2.5 py-1 text-[12px] font-medium text-emerald-700 dark:text-emerald-300">
              {t("settings.byok.webSearch.noCredentialRequired")}
            </span>
          </SettingsRow>
        ) : null}

        {selectedProvider?.credential === "api_key" ? (
          <SettingsRow
            title={t("settings.byok.apiKey")}
            description={t("settings.byok.webSearch.apiKeyHelp")}
          >
            <div className="relative w-[280px] max-w-full">
              {showKeyInput ? (
                <>
                  <Input
                    type={keyVisible ? "text" : "password"}
                    value={form.apiKey ?? ""}
                    onChange={(event) =>
                      onChangeForm((prev) => ({ ...prev, apiKey: event.target.value }))
                    }
                    placeholder={
                      hasExistingSecret
                        ? t("settings.byok.apiKeyConfiguredPlaceholder")
                        : t("settings.byok.apiKeyPlaceholder")
                    }
                    className="h-9 rounded-full pr-11 text-[13px]"
                  />
                  <Button
                    type="button"
                    variant="ghost"
                    size="icon"
                    onClick={onToggleKey}
                    aria-label={
                      keyVisible ? t("settings.byok.hideApiKey") : t("settings.byok.showApiKey")
                    }
                    className="absolute right-1 top-1/2 h-7 w-7 -translate-y-1/2 rounded-full text-muted-foreground hover:bg-muted hover:text-foreground"
                  >
                    {keyVisible ? (
                      <EyeOff className="h-3.5 w-3.5" aria-hidden />
                    ) : (
                      <Eye className="h-3.5 w-3.5" aria-hidden />
                    )}
                  </Button>
                </>
              ) : (
                <>
                  <div className="flex h-9 items-center rounded-full border border-input bg-background px-3 pr-11 text-[13px] text-muted-foreground">
                    {settings.web_search.api_key_hint ?? t("settings.byok.configuredKeyHint")}
                  </div>
                  <Button
                    type="button"
                    variant="ghost"
                    size="icon"
                    onClick={onToggleKeyEditing}
                    aria-label={t("settings.actions.edit")}
                    className="absolute right-1 top-1/2 h-7 w-7 -translate-y-1/2 rounded-full text-muted-foreground hover:bg-muted hover:text-foreground"
                  >
                    <Pencil className="h-3.5 w-3.5" aria-hidden />
                  </Button>
                </>
              )}
            </div>
          </SettingsRow>
        ) : null}

        {selectedProvider?.credential === "base_url" ? (
          <SettingsRow
            title={t("settings.byok.webSearch.baseUrl")}
            description={t("settings.byok.webSearch.baseUrlHelp")}
          >
            <Input
              value={form.baseUrl ?? ""}
              onChange={(event) =>
                onChangeForm((prev) => ({ ...prev, baseUrl: event.target.value }))
              }
              placeholder={t("settings.byok.webSearch.baseUrlPlaceholder")}
              className="h-9 w-[280px] rounded-full text-[13px]"
            />
          </SettingsRow>
        ) : null}

        <div className="flex min-h-[58px] items-center justify-between gap-4 px-4 py-3 sm:px-5">
          <div className="text-[13px] text-muted-foreground">
            {missingCredential
              ? t("settings.byok.webSearch.missingCredential")
              : t("settings.byok.webSearch.saveHint")}
          </div>
          <Button
            size="sm"
            variant="outline"
            onClick={onSave}
            disabled={!dirty || missingCredential || saving}
            className="rounded-full"
          >
            {saving ? t("settings.actions.saving") : t("settings.actions.save")}
          </Button>
        </div>
      </SettingsGroup>
    </section>
  );
}

function ByokSettings({
  settings,
  expandedProvider,
  providerForms,
  visibleProviderKeys,
  editingProviderKeys,
  providerSaving,
  webSearchForm,
  webSearchKeyVisible,
  webSearchKeyEditing,
  webSearchSaving,
  onToggleProvider,
  onToggleProviderKey,
  onToggleProviderKeyEditing,
  onChangeProviderForm,
  onSaveProvider,
  onRefreshSettings,
  onChangeWebSearchForm,
  onChangeWebSearchProvider,
  onToggleWebSearchKey,
  onToggleWebSearchKeyEditing,
  onResetProviderDraft,
  onResetWebSearchDraft,
  onSaveWebSearch,
  forcePane,
}: {
  settings: SettingsPayload;
  forcePane?: ByokPaneKey;
  expandedProvider: string | null;
  providerForms: Record<string, { apiKey: string; apiBase: string }>;
  visibleProviderKeys: Record<string, boolean>;
  editingProviderKeys: Record<string, boolean>;
  providerSaving: string | null;
  webSearchForm: WebSearchSettingsUpdate;
  webSearchKeyVisible: boolean;
  webSearchKeyEditing: boolean;
  webSearchSaving: boolean;
  onToggleProvider: (provider: string) => void;
  onToggleProviderKey: (provider: string) => void;
  onToggleProviderKeyEditing: (provider: string) => void;
  onChangeProviderForm: (provider: string, value: Partial<{ apiKey: string; apiBase: string }>) => void;
  onSaveProvider: (provider: string) => void;
  onRefreshSettings: () => void;
  onChangeWebSearchForm: Dispatch<SetStateAction<WebSearchSettingsUpdate>>;
  onChangeWebSearchProvider: (provider: string) => void;
  onToggleWebSearchKey: () => void;
  onToggleWebSearchKeyEditing: () => void;
  onResetProviderDraft: (provider: string) => void;
  onResetWebSearchDraft: () => void;
  onSaveWebSearch: () => void;
}) {
  const { t } = useTranslation();
  const { token: codexToken } = useClient();
  const [activePane, setActivePane] = useState<ByokPaneKey>(forcePane ?? "llm");
  // When the parent pins a pane (the section *is* the pane), follow the
  // prop — the internal tab state is only for the un-pinned, tabbed case.
  const pane = forcePane ?? activePane;
  const [showAllUnconfigured, setShowAllUnconfigured] = useState(false);
  const configuredProviders = settings.providers.filter((provider) => provider.configured);
  const unconfiguredProviders = settings.providers.filter((provider) => !provider.configured);
  const initialUnconfiguredCount = 6;
  const visibleUnconfiguredProviders = showAllUnconfigured
    ? unconfiguredProviders
    : unconfiguredProviders.slice(0, initialUnconfiguredCount);
  const hiddenUnconfiguredCount = Math.max(
    0,
    unconfiguredProviders.length - visibleUnconfiguredProviders.length,
  );
  const renderProviderRow = (provider: SettingsPayload["providers"][number]) => {
    const expanded = expandedProvider === provider.name;
    const form = providerForms[provider.name] ?? {
      apiKey: "",
      apiBase: provider.api_base ?? provider.default_api_base ?? "",
    };
    const saving = providerSaving === provider.name;
    const keyVisible = !!visibleProviderKeys[provider.name];
    const editingKey = !provider.configured || !!editingProviderKeys[provider.name];
    return (
      <div
        key={provider.name}
        className="divide-y divide-border/45"
      >
        <button
          type="button"
          onClick={() => onToggleProvider(provider.name)}
          className="flex min-h-[70px] w-full items-center justify-between gap-4 px-4 py-3 text-left transition-colors hover:bg-muted/35 sm:px-5"
        >
          <span className="flex min-w-0 items-center gap-3">
            <ProviderIcon provider={provider.name} />
            <span className="min-w-0">
              <span className="block truncate text-[15px] font-semibold leading-5 text-foreground">
                {provider.label}
              </span>
            </span>
          </span>
          <span
            className={cn(
              "rounded-full px-2.5 py-1 text-[12px] font-medium",
              provider.configured
                ? "bg-emerald-500/10 text-emerald-700 dark:text-emerald-300"
                : "bg-muted text-muted-foreground",
            )}
          >
            {provider.configured
              ? t("settings.byok.configured")
              : t("settings.byok.notConfigured")}
          </span>
        </button>

        {expanded && provider.oauth ? (
          <div className="bg-muted/18 px-4 py-4 sm:px-5">
            <CodexOAuthCard token={codexToken} embedded onChanged={onRefreshSettings} />
          </div>
        ) : null}

        {expanded && !provider.oauth ? (
          <div className="space-y-3 bg-muted/18 px-4 py-4 sm:px-5">
            <label className="block space-y-1.5">
              <span className="text-[12px] font-medium text-muted-foreground">
                {t("settings.byok.apiKey")}
              </span>
              <div className="relative">
                {editingKey ? (
                  <>
                    <Input
                      type={keyVisible ? "text" : "password"}
                      value={form.apiKey}
                      onChange={(event) =>
                        onChangeProviderForm(provider.name, { apiKey: event.target.value })
                      }
                      placeholder={
                        provider.configured
                          ? t("settings.byok.apiKeyConfiguredPlaceholder")
                          : t("settings.byok.apiKeyPlaceholder")
                      }
                      className="h-9 rounded-full pr-11 text-[13px]"
                    />
                    <Button
                      type="button"
                      variant="ghost"
                      size="icon"
                      onClick={() => onToggleProviderKey(provider.name)}
                      aria-label={
                        keyVisible
                          ? t("settings.byok.hideApiKey")
                          : t("settings.byok.showApiKey")
                      }
                      className="absolute right-1 top-1/2 h-7 w-7 -translate-y-1/2 rounded-full text-muted-foreground hover:bg-muted hover:text-foreground"
                    >
                      {keyVisible ? (
                        <EyeOff className="h-3.5 w-3.5" aria-hidden />
                      ) : (
                        <Eye className="h-3.5 w-3.5" aria-hidden />
                      )}
                    </Button>
                  </>
                ) : (
                  <>
                    <div className="flex h-9 items-center rounded-full border border-input bg-background px-3 pr-11 text-[13px] text-muted-foreground">
                      {provider.api_key_hint ?? t("settings.byok.configuredKeyHint")}
                    </div>
                    <Button
                      type="button"
                      variant="ghost"
                      size="icon"
                      onClick={() => onToggleProviderKeyEditing(provider.name)}
                      aria-label={t("settings.actions.edit")}
                      className="absolute right-1 top-1/2 h-7 w-7 -translate-y-1/2 rounded-full text-muted-foreground hover:bg-muted hover:text-foreground"
                    >
                      <Pencil className="h-3.5 w-3.5" aria-hidden />
                    </Button>
                  </>
                )}
              </div>
            </label>
            <label className="block space-y-1.5">
              <span className="text-[12px] font-medium text-muted-foreground">
                {t("settings.byok.apiBase")}
              </span>
              <Input
                value={form.apiBase}
                onChange={(event) =>
                  onChangeProviderForm(provider.name, { apiBase: event.target.value })
                }
                placeholder={provider.default_api_base ?? t("settings.byok.apiBasePlaceholder")}
                className="h-9 rounded-full text-[13px]"
              />
            </label>
            <div className="flex items-center justify-end">
              <Button
                size="sm"
                variant="outline"
                onClick={() => onSaveProvider(provider.name)}
                disabled={saving || (!provider.configured && !form.apiKey.trim())}
                className="rounded-full"
              >
                {saving ? t("settings.actions.saving") : t("settings.actions.save")}
              </Button>
            </div>
          </div>
        ) : null}
      </div>
    );
  };
  const panes: Array<{ key: ByokPaneKey; label: string }> = [
    { key: "llm", label: t("settings.byok.tabs.llm") },
    { key: "web-search", label: t("settings.byok.tabs.webSearch") },
  ];
  return (
    <div className="space-y-6">
      <p className="max-w-[42rem] text-[13px] leading-6 text-muted-foreground">
        {t(pane === "web-search" ? "settings.byok.webSearchDescription" : "settings.byok.description")}
      </p>
      <div
        role="tablist"
        aria-label={t("settings.byok.tabs.ariaLabel")}
        className={cn(
          "grid rounded-[22px] border border-border/35 bg-muted/35 p-1 shadow-[inset_0_1px_2px_rgba(15,23,42,0.04)] backdrop-blur-xl sm:grid-cols-2",
          forcePane && "hidden",
        )}
      >
        {panes.map((pane) => {
          const selected = activePane === pane.key;
          return (
            <button
              key={pane.key}
              type="button"
              role="tab"
              aria-selected={selected}
              onClick={() => {
                if (pane.key === activePane) return;
                if (activePane === "llm" && expandedProvider) {
                  onResetProviderDraft(expandedProvider);
                }
                if (activePane === "web-search") {
                  onResetWebSearchDraft();
                }
                setActivePane(pane.key);
              }}
              className={cn(
                "h-10 rounded-[18px] text-[13px] font-semibold transition-all",
                selected
                  ? "bg-background text-foreground shadow-[0_8px_28px_rgba(15,23,42,0.10)]"
                  : "text-muted-foreground hover:text-foreground",
              )}
            >
              {pane.label}
            </button>
          );
        })}
      </div>
      {pane === "llm" ? (
        <div className="space-y-8">
          <section className="space-y-3">
            <ByokSectionHeader
              title={t("settings.byok.configuredSection")}
              count={configuredProviders.length}
            />
            <div className="overflow-hidden rounded-[22px] border border-border/45 bg-card/86 shadow-[0_18px_65px_rgba(15,23,42,0.07)] backdrop-blur-xl dark:border-white/10 dark:shadow-[0_18px_65px_rgba(0,0,0,0.22)]">
              {configuredProviders.length > 0 ? (
                <div className="divide-y divide-border/45">
                  {configuredProviders.map(renderProviderRow)}
                </div>
              ) : (
                <ByokEmptyState>{t("settings.byok.noConfiguredProviders")}</ByokEmptyState>
              )}
            </div>
          </section>

          <section className="space-y-3">
            <ByokSectionHeader
              title={t("settings.byok.notConfiguredSection")}
              count={unconfiguredProviders.length}
            />
            <div className="overflow-hidden rounded-[22px] border border-border/45 bg-card/86 shadow-[0_18px_65px_rgba(15,23,42,0.07)] backdrop-blur-xl dark:border-white/10 dark:shadow-[0_18px_65px_rgba(0,0,0,0.22)]">
              <div className="divide-y divide-border/45">
                {visibleUnconfiguredProviders.map(renderProviderRow)}
              </div>
            </div>
            {hiddenUnconfiguredCount > 0 ? (
              <Button
                type="button"
                variant="ghost"
                onClick={() => setShowAllUnconfigured(true)}
                className="h-9 rounded-full px-3 text-[13px] text-muted-foreground hover:bg-muted/60 hover:text-foreground"
              >
                {t("settings.byok.showMore", { count: hiddenUnconfiguredCount })}
              </Button>
            ) : showAllUnconfigured && unconfiguredProviders.length > initialUnconfiguredCount ? (
              <Button
                type="button"
                variant="ghost"
                onClick={() => setShowAllUnconfigured(false)}
                className="h-9 rounded-full px-3 text-[13px] text-muted-foreground hover:bg-muted/60 hover:text-foreground"
              >
                {t("settings.byok.showLess")}
              </Button>
            ) : null}
          </section>
        </div>
      ) : (
        <WebSearchByokSettings
          settings={settings}
          form={webSearchForm}
          keyVisible={webSearchKeyVisible}
          keyEditing={webSearchKeyEditing}
          saving={webSearchSaving}
          onChangeForm={onChangeWebSearchForm}
          onChangeProvider={onChangeWebSearchProvider}
          onToggleKey={onToggleWebSearchKey}
          onToggleKeyEditing={onToggleWebSearchKeyEditing}
          onSave={onSaveWebSearch}
        />
      )}
    </div>
  );
}

function ByokSectionHeader({ title, count }: { title: string; count: number }) {
  return (
    <div className="flex items-center justify-between px-1">
      <h2 className="text-[13px] font-semibold tracking-[-0.01em] text-foreground/85">
        {title}
      </h2>
      <span className="rounded-full bg-muted px-2 py-0.5 text-[11.5px] font-medium text-muted-foreground">
        {count}
      </span>
    </div>
  );
}

function ByokEmptyState({ children }: { children: ReactNode }) {
  return (
    <div className="rounded-[18px] border border-dashed border-border/65 bg-card/45 px-4 py-5 text-[13px] text-muted-foreground">
      {children}
    </div>
  );
}

const PROVIDER_ICONS: Record<string, LucideIcon> = {
  custom: Hexagon,
  openrouter: Sparkles,
  aihubmix: Triangle,
  anthropic: Brain,
  openai: Bot,
  deepseek: Waves,
  zhipu: Grid3X3,
  dashscope: Cloud,
  moonshot: Moon,
  minimax: Zap,
  minimax_anthropic: Brain,
  groq: Cpu,
  huggingface: Layers,
  gemini: Gem,
  mistral: Orbit,
  siliconflow: Layers,
  volcengine: Cloud,
  volcengine_coding_plan: Cloud,
  byteplus: Cloud,
  byteplus_coding_plan: Cloud,
  qianfan: Database,
  azure_openai: Cloud,
  bedrock: Database,
};

function ProviderIcon({ provider }: { provider: string }) {
  const Icon = PROVIDER_ICONS[provider] ?? Hexagon;
  return (
    <span className="grid h-10 w-10 shrink-0 place-items-center rounded-2xl bg-muted text-foreground/82 shadow-[inset_0_0_0_1px_rgba(0,0,0,0.025)] dark:bg-muted/70">
      <Icon className="h-5 w-5" strokeWidth={2} aria-hidden />
    </span>
  );
}

interface SecretFormState {
  name: string;
  service: string;
  account: string;
  description: string;
  scope: string;
  value: string;
}

const EMPTY_SECRET_FORM: SecretFormState = {
  name: "",
  service: "",
  account: "",
  description: "",
  scope: "",
  value: "",
};

function SecretsSettings({ token }: { token: string }) {
  const { t, i18n } = useTranslation();
  const { client } = useClient();
  const [secrets, setSecrets] = useState<SecretEntry[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [form, setForm] = useState<SecretFormState>(EMPTY_SECRET_FORM);
  const [expandedName, setExpandedName] = useState<string | null>(null);
  const [editingName, setEditingName] = useState<string | null>(null);
  // Anchor for scrolling to the form when the user enters edit mode.
  const formAnchorRef = useRef<HTMLDivElement | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      setSecrets(await listSecrets(token));
    } catch {
      setError(t("settings.secrets.loadError"));
    } finally {
      setLoading(false);
    }
  }, [token, t]);

  useEffect(() => {
    void load();
  }, [load]);

  // In edit mode, value is optional — the backend treats an empty
  // value on an existing secret as a metadata-only update and
  // preserves the stored value.
  const canSave =
    form.name.trim() !== "" && form.service.trim() !== "" && !busy;

  const resetForm = useCallback(() => {
    setForm(EMPTY_SECRET_FORM);
    setEditingName(null);
  }, []);

  const onSubmit = useCallback(async () => {
    if (!canSave) return;
    setBusy(true);
    setError(null);
    try {
      await client.storeSecret({
        name: form.name.trim(),
        service: form.service.trim(),
        account: form.account.trim(),
        description: form.description.trim(),
        scope: form.scope
          .split(",")
          .map((tag) => tag.trim())
          .filter(Boolean),
        value: form.value,
      });
      resetForm();
      await load();
    } catch {
      setError(t("settings.secrets.loadError"));
    } finally {
      setBusy(false);
    }
  }, [canSave, client, form, load, resetForm, t]);

  const onEdit = useCallback((secret: SecretEntry) => {
    setEditingName(secret.name);
    setForm({
      name: secret.name,
      service: secret.service,
      account: secret.account || "",
      description: secret.description || "",
      scope: secret.scope.join(", "),
      value: "",
    });
    setExpandedName(secret.name);
    // Defer to next tick so the form section has re-rendered
    // (the title/buttons swap to edit-mode copy) before scrolling.
    setTimeout(() => {
      formAnchorRef.current?.scrollIntoView({
        behavior: "smooth", block: "start",
      });
    }, 0);
  }, []);

  const onDelete = useCallback(
    async (name: string) => {
      if (!window.confirm(t("settings.secrets.confirmDelete"))) return;
      setBusy(true);
      setError(null);
      try {
        await deleteSecret(token, name);
        // If we were editing/expanding this one, clear that state.
        if (editingName === name) resetForm();
        if (expandedName === name) setExpandedName(null);
        await load();
      } catch {
        setError(t("settings.secrets.loadError"));
      } finally {
        setBusy(false);
      }
    },
    [token, load, t, editingName, expandedName, resetForm],
  );

  const field = (key: keyof SecretFormState) => (
    e: React.ChangeEvent<HTMLInputElement>,
  ) => setForm((prev) => ({ ...prev, [key]: e.target.value }));

  const formatTimestamp = (iso: string): string => {
    if (!iso) return "—";
    const ms = Date.parse(iso);
    if (Number.isNaN(ms)) return iso;
    try {
      return new Date(ms).toLocaleString(i18n.language, {
        year: "numeric", month: "short", day: "numeric",
        hour: "2-digit", minute: "2-digit",
      });
    } catch {
      return new Date(ms).toISOString();
    }
  };

  return (
    <div className="space-y-5">
      <p className="px-1 text-[13px] leading-5 text-muted-foreground">
        {t("settings.secrets.description")}
      </p>

      {error ? (
        <div className="rounded-[18px] border border-destructive/20 bg-destructive/5 px-4 py-3 text-[13px] text-destructive">
          {error}
        </div>
      ) : null}

      <section>
        <SettingsSectionTitle>{t("settings.secrets.stored")}</SettingsSectionTitle>
        <SettingsGroup>
          {loading ? (
            <SettingsRow title={t("settings.status.loading")} />
          ) : secrets.length === 0 ? (
            <SettingsRow title={t("settings.secrets.empty")} />
          ) : (
            secrets.map((secret) => {
              const isExpanded = expandedName === secret.name;
              const isEditing = editingName === secret.name;
              return (
                <div key={secret.name}>
                  <SettingsRow
                    title={
                      <button
                        type="button"
                        onClick={() =>
                          setExpandedName(isExpanded ? null : secret.name)
                        }
                        className="flex w-full items-center gap-2 text-left hover:text-foreground/80"
                        aria-expanded={isExpanded}
                        aria-controls={`secret-detail-${secret.name}`}
                      >
                        <ChevronDown
                          className={`h-3.5 w-3.5 text-muted-foreground transition-transform ${
                            isExpanded ? "" : "-rotate-90"
                          }`}
                          aria-hidden
                        />
                        <span>{secret.name}</span>
                        {isEditing ? (
                          <span className="rounded-full bg-muted px-1.5 py-0.5 text-[10px] uppercase tracking-wide text-muted-foreground">
                            {t("settings.secrets.editingBadge")}
                          </span>
                        ) : null}
                      </button>
                    }
                    description={[
                      secret.service,
                      secret.account ? `· ${secret.account}` : "",
                      secret.scope.length ? `· ${secret.scope.join(", ")}` : "",
                      secret.description ? `— ${secret.description}` : "",
                    ]
                      .filter(Boolean)
                      .join(" ")}
                  >
                    <div className="flex items-center gap-1">
                      <Button
                        size="sm"
                        variant="ghost"
                        disabled={busy}
                        onClick={() => onEdit(secret)}
                        className="rounded-full"
                      >
                        <Pencil className="mr-1.5 h-3.5 w-3.5" aria-hidden />
                        {t("settings.secrets.edit")}
                      </Button>
                      <Button
                        size="sm"
                        variant="ghost"
                        disabled={busy}
                        onClick={() => onDelete(secret.name)}
                        className="rounded-full text-destructive hover:text-destructive"
                      >
                        <Trash2 className="mr-1.5 h-3.5 w-3.5" aria-hidden />
                        {t("settings.secrets.delete")}
                      </Button>
                    </div>
                  </SettingsRow>
                  {isExpanded ? (
                    <div
                      id={`secret-detail-${secret.name}`}
                      className="grid grid-cols-[auto,1fr] gap-x-4 gap-y-1.5 bg-muted/30 px-5 py-3 text-[12px] leading-5"
                    >
                      <span className="text-muted-foreground">
                        {t("settings.secrets.fieldService")}
                      </span>
                      <span className="font-mono">{secret.service || "—"}</span>
                      <span className="text-muted-foreground">
                        {t("settings.secrets.fieldAccount")}
                      </span>
                      <span className="font-mono">{secret.account || "—"}</span>
                      <span className="text-muted-foreground">
                        {t("settings.secrets.fieldDescription")}
                      </span>
                      <span>{secret.description || "—"}</span>
                      <span className="text-muted-foreground">
                        {t("settings.secrets.fieldScope")}
                      </span>
                      <span className="font-mono">
                        {secret.scope.length ? secret.scope.join(", ") : "—"}
                      </span>
                      <span className="text-muted-foreground">
                        {t("settings.secrets.fieldValue")}
                      </span>
                      <span className="font-mono text-muted-foreground">
                        {secret.value_hint
                          ? `●●●●●●●● (${secret.value_hint})`
                          : t("settings.secrets.valueRedacted")}
                      </span>
                      <span className="text-muted-foreground">
                        {t("settings.secrets.fieldOrigin")}
                      </span>
                      <span className="font-mono">{secret.origin || "—"}</span>
                      <span className="text-muted-foreground">
                        {t("settings.secrets.fieldCreated")}
                      </span>
                      <span>{formatTimestamp(secret.created_at)}</span>
                    </div>
                  ) : null}
                </div>
              );
            })
          )}
        </SettingsGroup>
      </section>

      <section ref={formAnchorRef}>
        <SettingsSectionTitle>
          {editingName
            ? t("settings.secrets.editTitle", { name: editingName })
            : t("settings.secrets.addTitle")}
        </SettingsSectionTitle>
        <SettingsGroup>
          <SettingsRow
            title={t("settings.secrets.fieldName")}
            description={t("settings.secrets.hintName")}
          >
            <Input
              value={form.name}
              onChange={field("name")}
              placeholder="ATLASSIAN_API_TOKEN"
              className="w-[260px]"
              readOnly={editingName !== null}
              disabled={editingName !== null}
            />
          </SettingsRow>
          <SettingsRow
            title={t("settings.secrets.fieldService")}
            description={t("settings.secrets.hintService")}
          >
            <Input
              value={form.service}
              onChange={field("service")}
              placeholder="atlassian"
              className="w-[260px]"
            />
          </SettingsRow>
          <SettingsRow
            title={`${t("settings.secrets.fieldAccount")} (${t("settings.secrets.optional")})`}
          >
            <Input
              value={form.account}
              onChange={field("account")}
              placeholder="work"
              className="w-[260px]"
            />
          </SettingsRow>
          <SettingsRow
            title={`${t("settings.secrets.fieldDescription")} (${t("settings.secrets.optional")})`}
          >
            <Input
              value={form.description}
              onChange={field("description")}
              className="w-[260px]"
            />
          </SettingsRow>
          <SettingsRow
            title={t("settings.secrets.fieldScope")}
            description={t("settings.secrets.hintScope")}
          >
            <Input
              value={form.scope}
              onChange={field("scope")}
              placeholder="exec, skill:*"
              className="w-[260px]"
            />
          </SettingsRow>
          <SettingsRow
            title={t("settings.secrets.fieldValue")}
            description={
              editingName
                ? t("settings.secrets.hintValueEdit")
                : t("settings.secrets.hintValue")
            }
          >
            <Input
              type="password"
              value={form.value}
              onChange={field("value")}
              placeholder={
                editingName
                  ? t("settings.secrets.valuePlaceholderEdit")
                  : ""
              }
              className="w-[260px]"
            />
          </SettingsRow>
          <div className="flex items-center justify-end gap-2 px-4 py-3 sm:px-5">
            {editingName ? (
              <Button
                size="sm"
                variant="ghost"
                onClick={resetForm}
                disabled={busy}
                className="rounded-full"
              >
                {t("settings.secrets.cancel")}
              </Button>
            ) : null}
            <Button
              size="sm"
              variant="outline"
              onClick={() => void onSubmit()}
              disabled={!canSave}
              className="rounded-full"
            >
              {editingName ? (
                <Pencil className="mr-1.5 h-3.5 w-3.5" aria-hidden />
              ) : (
                <Plus className="mr-1.5 h-3.5 w-3.5" aria-hidden />
              )}
              {busy
                ? t("settings.secrets.saving")
                : editingName
                  ? t("settings.secrets.saveEdit")
                  : t("settings.secrets.save")}
            </Button>
          </div>
        </SettingsGroup>
      </section>
    </div>
  );
}

function SettingsFooter({
  dirty,
  saving,
  saved,
  onSave,
}: {
  dirty: boolean;
  saving: boolean;
  saved: boolean;
  onSave: () => void;
}) {
  const { t } = useTranslation();
  return (
    <div className="flex min-h-[58px] items-center justify-between gap-4 px-4 py-3 sm:px-5">
      <div className="text-[13px] text-muted-foreground">
        {saved ? t("settings.status.savedRestart") : t("settings.status.unsaved")}
      </div>
      <Button size="sm" variant="outline" onClick={onSave} disabled={!dirty || saving} className="rounded-full">
        {saving ? t("settings.actions.saving") : t("settings.actions.save")}
      </Button>
    </div>
  );
}

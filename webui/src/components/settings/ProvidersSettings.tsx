import { useCallback, useEffect, useState, type ReactNode } from "react";
import { useTranslation } from "react-i18next";
import {
  Bot,
  Brain,
  ChevronDown,
  ChevronUp,
  Cloud,
  Cpu,
  Database,
  Gem,
  Grid3X3,
  Hexagon,
  Layers,
  Moon,
  Orbit,
  Pencil,
  Plus,
  Search,
  Sparkles,
  Star,
  Trash2,
  Triangle,
  Waves,
  Zap,
  type LucideIcon,
} from "lucide-react";

import {
  fetchProviderModels,
  removeProviderModel,
  updateProviderSettings,
  upsertProviderModel,
  type ProviderModelEntry,
} from "@/lib/api";
import type { SettingsPayload } from "@/lib/types";
import { cn } from "@/lib/utils";

type ProviderRow = SettingsPayload["providers"][number];
type ModelsState = ProviderModelEntry[] | "loading" | "error";

const PROVIDER_ICONS: Record<string, LucideIcon> = {
  custom: Hexagon,
  openrouter: Sparkles,
  aihubmix: Triangle,
  anthropic: Brain,
  openai: Bot,
  openai_codex: Bot,
  deepseek: Waves,
  zhipu: Grid3X3,
  zai_coding_plan: Grid3X3,
  dashscope: Cloud,
  moonshot: Moon,
  minimax: Zap,
  minimax_anthropic: Brain,
  groq: Cpu,
  huggingface: Layers,
  gemini: Gem,
  mistral: Orbit,
  siliconflow: Layers,
  azure_openai: Cloud,
  bedrock: Database,
};

function ProviderGlyph({ provider }: { provider: string }) {
  const Icon = PROVIDER_ICONS[provider] ?? Hexagon;
  return (
    <span className="grid h-9 w-9 shrink-0 place-items-center rounded-xl bg-muted text-foreground/82 dark:bg-muted/70">
      <Icon className="h-[18px] w-[18px]" strokeWidth={2} aria-hidden />
    </span>
  );
}

function formatCtx(n?: number | null): string | null {
  if (!n) return null;
  if (n >= 1_000_000) {
    const m = n / 1_000_000;
    return `${Number.isInteger(m) ? m : m.toFixed(1)}M`;
  }
  if (n >= 1_000) return `${Math.round(n / 1_000)}K`;
  return String(n);
}

function parseNum(s: string): number | null {
  const t = s.trim();
  if (!t) return null;
  const n = Number(t);
  return Number.isFinite(n) ? n : null;
}

function capsText(m: ProviderModelEntry, t: (k: string) => string): string {
  const parts: string[] = [];
  const ctx = formatCtx(m.max_input_tokens);
  if (ctx) parts.push(`${ctx} ctx`);
  if (m.supports_vision) parts.push(t("settings.providerModels.capVision"));
  if (m.supports_audio_input) parts.push(t("settings.providerModels.capAudio"));
  if (m.supports_reasoning) parts.push(t("settings.providerModels.capReasoning"));
  return parts.join(" · ");
}

interface DraftParams {
  context_window_tokens: string;
  max_tokens: string;
  temperature: string;
  reasoning_effort: string;
}

function toDraft(m: ProviderModelEntry): DraftParams {
  return {
    context_window_tokens: m.context_window_tokens != null ? String(m.context_window_tokens) : "",
    max_tokens: m.max_tokens != null ? String(m.max_tokens) : "",
    temperature: m.temperature != null ? String(m.temperature) : "",
    reasoning_effort: m.reasoning_effort ?? "",
  };
}

interface ProvidersSettingsProps {
  token: string;
  settings: SettingsPayload;
  onRefresh: () => void;
}

export function ProvidersSettings({ token, settings, onRefresh }: ProvidersSettingsProps) {
  const { t } = useTranslation();
  const [expanded, setExpanded] = useState<string | null>(null);
  const [showAll, setShowAll] = useState(false);
  const [models, setModels] = useState<Record<string, ModelsState>>({});

  const configured = settings.providers.filter((p) => p.configured);
  const unconfigured = settings.providers.filter((p) => !p.configured);
  const defaultProvider = settings.agent.resolved_provider ?? settings.agent.provider;

  const loadModels = useCallback(
    async (provider: string) => {
      setModels((m) => ({ ...m, [provider]: "loading" }));
      try {
        const rows = await fetchProviderModels(token, provider);
        setModels((m) => ({ ...m, [provider]: rows }));
      } catch {
        setModels((m) => ({ ...m, [provider]: "error" }));
      }
    },
    [token],
  );

  useEffect(() => {
    settings.providers
      .filter((p) => p.configured)
      .forEach((p) => void loadModels(p.name));
  }, [settings, loadModels]);

  const visibleUnconfigured = showAll ? unconfigured : unconfigured.slice(0, 6);

  return (
    <div className="space-y-5">
      <p className="px-1 text-[12.5px] text-muted-foreground">
        {t("settings.providers.summary", {
          configured: configured.length,
          available: unconfigured.length,
        })}
      </p>

      <div className="overflow-hidden rounded-2xl border border-border/60">
        {configured.map((p, i) => (
          <ProviderItem
            key={p.name}
            provider={p}
            token={token}
            first={i === 0}
            expanded={expanded === p.name}
            onToggle={() => setExpanded((e) => (e === p.name ? null : p.name))}
            models={models[p.name]}
            onModelsChanged={() => loadModels(p.name)}
            defaultModel={defaultProvider === p.name ? settings.agent.model : null}
            onConnectionSaved={onRefresh}
          />
        ))}
      </div>

      {unconfigured.length > 0 ? (
        <div>
          <p className="mb-2 px-1 text-[12px] font-medium uppercase tracking-wide text-muted-foreground/70">
            {t("settings.providers.notConfigured")} ({unconfigured.length})
          </p>
          <div className="overflow-hidden rounded-2xl border border-border/60">
            {visibleUnconfigured.map((p, i) => (
              <ProviderItem
                key={p.name}
                provider={p}
                token={token}
                first={i === 0}
                expanded={expanded === p.name}
                onToggle={() => setExpanded((e) => (e === p.name ? null : p.name))}
                models={undefined}
                onModelsChanged={() => {}}
                defaultModel={null}
                onConnectionSaved={onRefresh}
              />
            ))}
          </div>
          {unconfigured.length > 6 ? (
            <button
              type="button"
              onClick={() => setShowAll((v) => !v)}
              className="mt-2 px-1 text-[12px] text-muted-foreground hover:text-foreground"
            >
              {showAll
                ? t("settings.providers.showLess")
                : t("settings.providers.showMore", { n: unconfigured.length - 6 })}
            </button>
          ) : null}
        </div>
      ) : null}
    </div>
  );
}

interface ProviderItemProps {
  provider: ProviderRow;
  token: string;
  first: boolean;
  expanded: boolean;
  onToggle: () => void;
  models: ModelsState | undefined;
  onModelsChanged: () => void;
  defaultModel: string | null;
  onConnectionSaved: () => void;
}

function ProviderItem(props: ProviderItemProps) {
  const { t } = useTranslation();
  const { provider, expanded, onToggle, models, defaultModel } = props;
  const count = Array.isArray(models) ? models.length : null;

  return (
    <div className={cn(!props.first && "border-t border-border/50")}>
      <button
        type="button"
        onClick={onToggle}
        className="flex w-full items-center gap-3 px-4 py-3 text-left hover:bg-muted/40"
      >
        <ProviderGlyph provider={provider.name} />
        <span className="min-w-0 flex-1">
          <span className="block text-[14px] font-medium">{provider.label}</span>
          {provider.configured && defaultModel ? (
            <span className="block truncate text-[11.5px] text-muted-foreground">
              {t("settings.providers.defaultModel", { model: defaultModel })}
            </span>
          ) : null}
        </span>
        {provider.configured ? (
          <>
            {count != null ? (
              <span className="shrink-0 rounded-full bg-muted px-2 py-0.5 text-[11px] text-muted-foreground">
                {t("settings.providers.modelsCount", { count })}
              </span>
            ) : null}
            <span className="shrink-0 rounded-full bg-emerald-500/12 px-2.5 py-0.5 text-[11px] text-emerald-600 dark:text-emerald-400">
              {t("settings.providers.connected")}
            </span>
          </>
        ) : (
          <span className="shrink-0 rounded-full border border-border/70 px-2.5 py-0.5 text-[11px] text-muted-foreground">
            {t("settings.providers.connect")}
          </span>
        )}
        {expanded ? (
          <ChevronUp className="h-4 w-4 shrink-0 text-muted-foreground" aria-hidden />
        ) : (
          <ChevronDown className="h-4 w-4 shrink-0 text-muted-foreground" aria-hidden />
        )}
      </button>

      {expanded ? (
        <div className="space-y-4 bg-muted/35 px-4 pb-4 pt-1">
          <ConnectionEditor
            provider={provider}
            token={props.token}
            onSaved={props.onConnectionSaved}
          />
          {provider.configured ? (
            <ModelsSection
              token={props.token}
              provider={provider.name}
              models={models}
              defaultModel={defaultModel}
              onChanged={props.onModelsChanged}
            />
          ) : null}
        </div>
      ) : null}
    </div>
  );
}

function SectionLabel({ children }: { children: ReactNode }) {
  return (
    <p className="mb-1.5 mt-2 text-[11px] font-medium uppercase tracking-wide text-muted-foreground/70">
      {children}
    </p>
  );
}

function ConnectionEditor({
  provider,
  token,
  onSaved,
}: {
  provider: ProviderRow;
  token: string;
  onSaved: () => void;
}) {
  const { t } = useTranslation();
  const [editing, setEditing] = useState(!provider.configured);
  const [apiKey, setApiKey] = useState("");
  const [apiBase, setApiBase] = useState(provider.api_base ?? "");
  const [saving, setSaving] = useState(false);

  const save = async () => {
    setSaving(true);
    try {
      await updateProviderSettings(token, {
        provider: provider.name,
        apiKey: apiKey.trim() || undefined,
        apiBase: apiBase.trim() || undefined,
      });
      setApiKey("");
      setEditing(false);
      onSaved();
    } finally {
      setSaving(false);
    }
  };

  const isLocal = !!provider.is_local;

  if (provider.configured && !editing) {
    return (
      <div>
        <SectionLabel>{t("settings.providers.connection")}</SectionLabel>
        <div className="flex flex-wrap items-center gap-2">
          {!isLocal ? (
            <span className="flex items-center gap-2 rounded-lg border border-border/60 bg-background px-2.5 py-1.5">
              <span className="text-[11.5px] text-muted-foreground">{t("settings.byok.apiKey")}</span>
              <span className="font-mono text-[12px]">{provider.api_key_hint ?? "••••"}</span>
            </span>
          ) : null}
          {provider.api_base ? (
            <span className="flex items-center gap-2 rounded-lg border border-border/60 bg-background px-2.5 py-1.5">
              <span className="text-[11.5px] text-muted-foreground">{t("settings.byok.apiBase")}</span>
              <span className="max-w-[320px] truncate font-mono text-[12px]">{provider.api_base}</span>
            </span>
          ) : null}
          <button
            type="button"
            onClick={() => setEditing(true)}
            className="flex items-center gap-1 rounded-lg px-2 py-1.5 text-[12px] text-muted-foreground hover:text-foreground"
          >
            <Pencil className="h-3.5 w-3.5" aria-hidden /> {t("settings.providerModels.edit")}
          </button>
        </div>
      </div>
    );
  }

  return (
    <div>
      <SectionLabel>{t("settings.providers.connection")}</SectionLabel>
      <div className="space-y-2">
        {isLocal ? (
          <p className="text-[11.5px] text-muted-foreground">{t("settings.providers.localHint")}</p>
        ) : (
          <label className="block">
            <span className="mb-1 block text-[11.5px] text-muted-foreground">{t("settings.byok.apiKey")}</span>
            <input
              type="password"
              value={apiKey}
              onChange={(e) => setApiKey(e.target.value)}
              placeholder={
                provider.configured
                  ? t("settings.byok.apiKeyConfiguredPlaceholder")
                  : t("settings.byok.apiKeyPlaceholder")
              }
              className="h-9 w-full rounded-lg border border-border/60 bg-background px-3 text-[13px]"
            />
          </label>
        )}
        <label className="block">
          <span className="mb-1 block text-[11.5px] text-muted-foreground">{t("settings.byok.apiBase")}</span>
          <input
            value={apiBase}
            onChange={(e) => setApiBase(e.target.value)}
            placeholder={provider.default_api_base ?? t("settings.byok.apiBasePlaceholder")}
            className="h-9 w-full rounded-lg border border-border/60 bg-background px-3 font-mono text-[12.5px]"
          />
        </label>
        <div className="flex items-center gap-2">
          <button
            type="button"
            disabled={saving || (!provider.configured && (isLocal ? !apiBase.trim() : !apiKey.trim()))}
            onClick={() => void save()}
            className="rounded-lg border border-border/60 bg-background px-3 py-1.5 text-[12px] font-medium disabled:opacity-50"
          >
            {provider.configured ? t("settings.providerModels.save") : t("settings.providers.connect")}
          </button>
          {provider.configured ? (
            <button
              type="button"
              onClick={() => {
                setEditing(false);
                setApiKey("");
                setApiBase(provider.api_base ?? "");
              }}
              className="px-2 py-1.5 text-[12px] text-muted-foreground"
            >
              {t("settings.providerModels.cancel")}
            </button>
          ) : null}
        </div>
      </div>
    </div>
  );
}

function ModelsSection({
  token,
  provider,
  models,
  defaultModel,
  onChanged,
}: {
  token: string;
  provider: string;
  models: ModelsState | undefined;
  defaultModel: string | null;
  onChanged: () => void;
}) {
  const { t } = useTranslation();
  const [query, setQuery] = useState("");
  const [editing, setEditing] = useState<string | null>(null);
  const [draft, setDraft] = useState<DraftParams | null>(null);
  const [busy, setBusy] = useState(false);
  const [confirmDelete, setConfirmDelete] = useState<string | null>(null);
  const [addId, setAddId] = useState("");

  const save = async (id: string) => {
    if (!draft) return;
    setBusy(true);
    try {
      await upsertProviderModel(token, provider, id, {
        context_window_tokens: parseNum(draft.context_window_tokens),
        max_tokens: parseNum(draft.max_tokens),
        temperature: parseNum(draft.temperature),
        reasoning_effort: draft.reasoning_effort.trim() || null,
      });
      setEditing(null);
      setDraft(null);
      onChanged();
    } finally {
      setBusy(false);
    }
  };

  const addCustom = async () => {
    const id = addId.trim();
    if (!id) return;
    setBusy(true);
    try {
      await upsertProviderModel(token, provider, id, {});
      setAddId("");
      onChanged();
    } finally {
      setBusy(false);
    }
  };

  const remove = async (id: string) => {
    setBusy(true);
    try {
      await removeProviderModel(token, provider, id);
      setConfirmDelete(null);
      onChanged();
    } finally {
      setBusy(false);
    }
  };

  return (
    <div>
      <SectionLabel>{t("settings.providerModels.title")}</SectionLabel>
      {models === "loading" || models === undefined ? (
        <div className="space-y-1.5">
          {[0, 1, 2].map((i) => (
            <div key={i} className="h-8 animate-pulse rounded-lg bg-muted/60" />
          ))}
        </div>
      ) : models === "error" ? (
        <p className="text-[12.5px] text-muted-foreground">{t("settings.providerModels.loadError")}</p>
      ) : models.length === 0 ? (
        <p className="rounded-lg border border-dashed border-border/60 bg-background px-3 py-3 text-[12.5px] text-muted-foreground">
          {t("settings.providerModels.emptyTeach")}
        </p>
      ) : (
        <>
          <div className="mb-2 flex items-center gap-2 rounded-lg border border-border/60 bg-background px-2.5 py-1.5">
            <Search className="h-3.5 w-3.5 text-muted-foreground" aria-hidden />
            <input
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              placeholder={t("settings.providerModels.search")}
              className="flex-1 bg-transparent text-[12.5px] focus:outline-none"
            />
          </div>
          <div className="max-h-[300px] overflow-y-auto rounded-lg border border-border/60 bg-background">
            {models
              .filter((m) => !query.trim() || m.id.toLowerCase().includes(query.trim().toLowerCase()))
              .map((m, i) => (
                <ModelRow
                  key={m.id}
                  model={m}
                  first={i === 0}
                  isDefault={m.id === defaultModel}
                  editing={editing === m.id}
                  draft={editing === m.id ? draft : null}
                  busy={busy}
                  confirming={confirmDelete === m.id}
                  onEdit={() => {
                    setEditing(m.id);
                    setDraft(toDraft(m));
                    setConfirmDelete(null);
                  }}
                  onCancel={() => {
                    setEditing(null);
                    setDraft(null);
                  }}
                  onChange={setDraft}
                  onSave={() => void save(m.id)}
                  onAskDelete={() => setConfirmDelete(m.id)}
                  onCancelDelete={() => setConfirmDelete(null)}
                  onConfirmDelete={() => void remove(m.id)}
                />
              ))}
          </div>
        </>
      )}
      <div className="mt-2 flex items-center gap-2">
        <input
          value={addId}
          onChange={(e) => setAddId(e.target.value)}
          placeholder={t("settings.providerModels.addPlaceholder")}
          className="h-8 flex-1 rounded-lg border border-dashed border-border/60 bg-background px-2.5 text-[12px]"
        />
        <button
          type="button"
          disabled={busy || !addId.trim()}
          onClick={() => void addCustom()}
          className="flex items-center gap-1 rounded-lg border border-border/60 px-2.5 py-1.5 text-[12px] disabled:opacity-50"
        >
          <Plus className="h-3.5 w-3.5" aria-hidden /> {t("settings.providerModels.add")}
        </button>
      </div>
    </div>
  );
}

interface ModelRowProps {
  model: ProviderModelEntry;
  first: boolean;
  isDefault: boolean;
  editing: boolean;
  draft: DraftParams | null;
  busy: boolean;
  confirming: boolean;
  onEdit: () => void;
  onCancel: () => void;
  onChange: (d: DraftParams) => void;
  onSave: () => void;
  onAskDelete: () => void;
  onCancelDelete: () => void;
  onConfirmDelete: () => void;
}

function ModelRow(props: ModelRowProps) {
  const { t } = useTranslation();
  const { model, editing, draft, isDefault } = props;
  const caps = capsText(model, t);

  return (
    <div className={cn("px-3 py-2", !props.first && "border-t border-border/50")}>
      <div className="flex items-center gap-2">
        <span className="min-w-0 flex-1 truncate font-mono text-[12.5px]">{model.id}</span>
        {isDefault ? (
          <span className="flex shrink-0 items-center gap-1 rounded-full bg-emerald-500/12 px-2 py-0.5 text-[10.5px] text-emerald-600 dark:text-emerald-400">
            <Star className="h-3 w-3" aria-hidden /> {t("settings.providers.default")}
          </span>
        ) : null}
        {caps ? <span className="shrink-0 text-[11px] text-muted-foreground">{caps}</span> : null}
        {!editing ? (
          <button
            type="button"
            onClick={props.onEdit}
            className="shrink-0 text-muted-foreground hover:text-foreground"
            aria-label={t("settings.providerModels.edit")}
          >
            <Pencil className="h-3.5 w-3.5" aria-hidden />
          </button>
        ) : null}
      </div>
      {editing && draft ? (
        <div className="mt-2 space-y-2 rounded-lg bg-muted/50 p-2.5">
          <div className="grid grid-cols-2 gap-2">
            <ParamField
              label={t("settings.providerModels.contextWindow")}
              value={draft.context_window_tokens}
              onChange={(v) => props.onChange({ ...draft, context_window_tokens: v })}
            />
            <ParamField
              label={t("settings.providerModels.maxTokens")}
              value={draft.max_tokens}
              onChange={(v) => props.onChange({ ...draft, max_tokens: v })}
            />
            <ParamField
              label={t("settings.providerModels.temperature")}
              value={draft.temperature}
              onChange={(v) => props.onChange({ ...draft, temperature: v })}
            />
            <ParamField
              label={t("settings.providerModels.reasoningEffort")}
              value={draft.reasoning_effort}
              onChange={(v) => props.onChange({ ...draft, reasoning_effort: v })}
            />
          </div>
          <div className="flex items-center gap-2">
            <button
              type="button"
              disabled={props.busy}
              onClick={props.onSave}
              className="rounded-lg border border-border/60 bg-background px-2.5 py-1 text-[12px] font-medium disabled:opacity-50"
            >
              {t("settings.providerModels.save")}
            </button>
            <button type="button" disabled={props.busy} onClick={props.onCancel} className="px-2 py-1 text-[12px] text-muted-foreground">
              {t("settings.providerModels.cancel")}
            </button>
            {props.confirming ? (
              <span className="ml-auto flex items-center gap-1.5">
                <span className="text-[11px] text-muted-foreground">{t("settings.providerModels.confirmRemove")}</span>
                <button type="button" disabled={props.busy} onClick={props.onConfirmDelete} className="text-[11px] text-destructive">
                  {t("settings.providerModels.remove")}
                </button>
                <button type="button" disabled={props.busy} onClick={props.onCancelDelete} className="text-[11px] text-muted-foreground">
                  {t("settings.providerModels.cancel")}
                </button>
              </span>
            ) : (
              <button
                type="button"
                disabled={props.busy}
                onClick={props.onAskDelete}
                className="ml-auto flex items-center gap-1 text-[11px] text-destructive"
              >
                <Trash2 className="h-3.5 w-3.5" aria-hidden /> {t("settings.providerModels.remove")}
              </button>
            )}
          </div>
        </div>
      ) : null}
    </div>
  );
}

function ParamField(props: { label: string; value: string; onChange: (v: string) => void }) {
  const { t } = useTranslation();
  return (
    <label className="flex flex-col gap-1 text-[10.5px] text-muted-foreground">
      {props.label}
      <input
        value={props.value}
        placeholder={t("settings.providerModels.catalogDefault")}
        onChange={(e) => props.onChange(e.target.value)}
        className="rounded-md border border-border/60 bg-background px-2 py-1 text-[12px] text-foreground"
      />
    </label>
  );
}

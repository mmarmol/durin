import { useCallback, useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { ChevronDown, ChevronRight, Loader2, Plus, Trash2 } from "lucide-react";

import {
  fetchProviderModels,
  removeProviderModel,
  upsertProviderModel,
  type ProviderModelEntry,
} from "@/lib/api";

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

interface ProviderModelsSettingsProps {
  token: string;
  provider: string;
  label: string;
}

export function ProviderModelsSettings({ token, provider, label }: ProviderModelsSettingsProps) {
  const { t } = useTranslation();
  const [models, setModels] = useState<ProviderModelEntry[]>([]);
  const [loading, setLoading] = useState(true);
  const [open, setOpen] = useState(false);
  const [editing, setEditing] = useState<string | null>(null);
  const [draft, setDraft] = useState<DraftParams | null>(null);
  const [busy, setBusy] = useState(false);
  const [confirmDelete, setConfirmDelete] = useState<string | null>(null);
  const [addId, setAddId] = useState("");

  const load = useCallback(async () => {
    setLoading(true);
    try {
      setModels(await fetchProviderModels(token, provider));
    } catch {
      setModels([]);
    } finally {
      setLoading(false);
    }
  }, [token, provider]);

  useEffect(() => {
    if (open) void load();
  }, [open, load]);

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
      await load();
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
      await load();
    } finally {
      setBusy(false);
    }
  };

  const remove = async (id: string) => {
    setBusy(true);
    try {
      await removeProviderModel(token, provider, id);
      setConfirmDelete(null);
      await load();
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="rounded-lg border border-border/60 bg-card/40">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        className="flex w-full items-center gap-2 px-3 py-2 text-left text-[13px] font-medium"
      >
        {open ? <ChevronDown className="h-4 w-4" aria-hidden /> : <ChevronRight className="h-4 w-4" aria-hidden />}
        <span>{label}</span>
        <span className="text-[11px] text-muted-foreground">{t("settings.providerModels.title")}</span>
      </button>
      {open ? (
        <div className="border-t border-border/50 px-3 py-2">
          {loading ? (
            <div className="flex items-center gap-2 py-3 text-[12px] text-muted-foreground">
              <Loader2 className="h-3.5 w-3.5 animate-spin" aria-hidden /> {t("settings.providerModels.loading")}
            </div>
          ) : (
            <>
              <div className="max-h-[320px] space-y-1 overflow-y-auto">
                {models.map((m) => (
                  <ModelRow
                    key={m.id}
                    model={m}
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
                {models.length === 0 ? (
                  <p className="py-2 text-[12px] text-muted-foreground">{t("settings.providerModels.empty")}</p>
                ) : null}
              </div>
              <div className="mt-2 flex items-center gap-2 border-t border-border/40 pt-2">
                <input
                  value={addId}
                  onChange={(e) => setAddId(e.target.value)}
                  placeholder={t("settings.providerModels.addPlaceholder")}
                  className="flex-1 rounded-md border border-border/60 bg-background px-2 py-1 text-[12px]"
                />
                <button
                  type="button"
                  disabled={busy || !addId.trim()}
                  onClick={() => void addCustom()}
                  className="flex items-center gap-1 rounded-md px-2 py-1 text-[12px] disabled:opacity-50"
                >
                  <Plus className="h-3.5 w-3.5" aria-hidden /> {t("settings.providerModels.add")}
                </button>
              </div>
            </>
          )}
        </div>
      ) : null}
    </div>
  );
}

interface ModelRowProps {
  model: ProviderModelEntry;
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
  const { model, editing, draft } = props;
  const caps: string[] = [];
  const ctx = formatCtx(model.max_input_tokens);
  if (ctx) caps.push(ctx);
  if (model.supports_vision) caps.push(t("settings.providerModels.capVision"));
  if (model.supports_audio_input) caps.push(t("settings.providerModels.capAudio"));
  if (model.supports_reasoning) caps.push(t("settings.providerModels.capReasoning"));

  return (
    <div className="rounded-md px-2 py-1.5 hover:bg-muted/40">
      <div className="flex items-center gap-2">
        <span className="min-w-0 flex-1 truncate font-mono text-[12px]">{model.id}</span>
        {model.configured ? <span className="text-[10px] text-emerald-500" title={t("settings.providerModels.configured")}>●</span> : null}
        {caps.length ? <span className="text-[10px] text-muted-foreground">{caps.join(" · ")}</span> : null}
        {!editing ? (
          <button type="button" onClick={props.onEdit} className="text-[11px] text-muted-foreground hover:text-foreground">
            {t("settings.providerModels.edit")}
          </button>
        ) : null}
      </div>
      {editing && draft ? (
        <div className="mt-1.5 space-y-1.5">
          <div className="grid grid-cols-2 gap-2">
            <LabeledInput label={t("settings.providerModels.contextWindow")} value={draft.context_window_tokens} placeholder={t("settings.providerModels.catalogDefault")} onChange={(v) => props.onChange({ ...draft, context_window_tokens: v })} />
            <LabeledInput label={t("settings.providerModels.maxTokens")} value={draft.max_tokens} placeholder={t("settings.providerModels.catalogDefault")} onChange={(v) => props.onChange({ ...draft, max_tokens: v })} />
            <LabeledInput label={t("settings.providerModels.temperature")} value={draft.temperature} placeholder={t("settings.providerModels.catalogDefault")} onChange={(v) => props.onChange({ ...draft, temperature: v })} />
            <LabeledInput label={t("settings.providerModels.reasoningEffort")} value={draft.reasoning_effort} placeholder={t("settings.providerModels.catalogDefault")} onChange={(v) => props.onChange({ ...draft, reasoning_effort: v })} />
          </div>
          <div className="flex items-center gap-2">
            <button type="button" disabled={props.busy} onClick={props.onSave} className="rounded-md px-2 py-1 text-[12px] font-medium disabled:opacity-50">
              {t("settings.providerModels.save")}
            </button>
            <button type="button" disabled={props.busy} onClick={props.onCancel} className="rounded-md px-2 py-1 text-[12px]">
              {t("settings.providerModels.cancel")}
            </button>
            {model.configured ? (
              props.confirming ? (
                <span className="ml-auto flex items-center gap-1.5">
                  <span className="text-[11px] text-muted-foreground">{t("settings.providerModels.confirmRemove")}</span>
                  <button type="button" disabled={props.busy} onClick={props.onConfirmDelete} className="text-[11px] text-destructive">
                    {t("settings.providerModels.remove")}
                  </button>
                  <button type="button" disabled={props.busy} onClick={props.onCancelDelete} className="text-[11px]">
                    {t("settings.providerModels.cancel")}
                  </button>
                </span>
              ) : (
                <button type="button" disabled={props.busy} onClick={props.onAskDelete} className="ml-auto flex items-center gap-1 text-[11px] text-destructive">
                  <Trash2 className="h-3.5 w-3.5" aria-hidden /> {t("settings.providerModels.remove")}
                </button>
              )
            ) : null}
          </div>
        </div>
      ) : null}
    </div>
  );
}

function LabeledInput(props: { label: string; value: string; placeholder: string; onChange: (v: string) => void }) {
  return (
    <label className="flex flex-col gap-0.5 text-[10.5px] text-muted-foreground">
      {props.label}
      <input
        value={props.value}
        placeholder={props.placeholder}
        onChange={(e) => props.onChange(e.target.value)}
        className="rounded border border-border/60 bg-background px-1.5 py-0.5 text-[12px] text-foreground"
      />
    </label>
  );
}

import { useCallback, useEffect, useMemo, useState } from "react";
import { Loader2 } from "lucide-react";
import { useTranslation } from "react-i18next";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { getConfig, getExtraStatus, setConfigValue, type ExtraStatus } from "@/lib/api";
import { cn } from "@/lib/utils";
import { ExtraInstallPrompt } from "./ExtraInstallPrompt";
import {
  SettingsGroup,
  SettingsRow,
  SettingsSectionTitle,
} from "./primitives";

interface DocumentsConfigShape {
  // /api/config returns the canonical snake_case shape; setConfigValue
  // *paths* are snake_case too, normalized server-side before writing.
  documents?: {
    ocr?: { enabled?: boolean; inline_max_pages?: number };
    max_file_size_mb?: number;
    max_text_chars?: number;
  };
}

interface DocumentsState {
  ocrEnabled: boolean;
  ocrInlineMaxPages: number;
  maxFileSizeMb: number;
  maxTextChars: number;
}

function readDocuments(config: Record<string, unknown> | null): DocumentsState {
  const documents = (config as DocumentsConfigShape | null)?.documents ?? {};
  const ocr = documents.ocr ?? {};
  return {
    ocrEnabled: typeof ocr.enabled === "boolean" ? ocr.enabled : false,
    ocrInlineMaxPages:
      typeof ocr.inline_max_pages === "number" ? ocr.inline_max_pages : 5,
    maxFileSizeMb:
      typeof documents.max_file_size_mb === "number" ? documents.max_file_size_mb : 50,
    maxTextChars:
      typeof documents.max_text_chars === "number" ? documents.max_text_chars : 200_000,
  };
}

/** Documents settings — the local OCR toggle for scanned PDFs, the inline
 *  page budget that decides when a document becomes a background job, and
 *  the two extraction limits shared by attachments, convert_to_markdown,
 *  and memory_ingest. */
export function DocumentsSettings({ token }: { token: string }) {
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
      setError(t("settings.documents.loadError"));
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
        setError(t("settings.documents.saveError", { path }));
      } finally {
        setSavingPath(null);
      }
    },
    [token, t],
  );

  const state = useMemo(() => readDocuments(config), [config]);

  const [pendingExtra, setPendingExtra] = useState<
    { feature: string; status: ExtraStatus; after: () => void } | null
  >(null);
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
        after(); // status check failed — let the action surface its own error
      }
    },
    [token],
  );

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
        {t("settings.documents.description")}
      </p>

      {error ? (
        <div className="rounded-[18px] border border-destructive/20 bg-destructive/5 px-4 py-3 text-[13px] text-destructive">
          {error}
        </div>
      ) : null}

      <section>
        <SettingsSectionTitle>{t("settings.documents.sections.ocr")}</SettingsSectionTitle>
        <SettingsGroup>
          <SettingsRow
            title={t("settings.documents.rows.ocrEnabled")}
            description={t("settings.documents.help.ocrEnabled")}
          >
            <ToggleSwitch
              checked={state.ocrEnabled}
              disabled={savingPath === "documents.ocr.enabled"}
              label={t("settings.documents.rows.ocrEnabled")}
              onToggle={() =>
                state.ocrEnabled
                  ? void onSave("documents.ocr.enabled", false)
                  : void ensureThen("ocr", () =>
                      void onSave("documents.ocr.enabled", true),
                    )
              }
            />
          </SettingsRow>
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
          <NumberRow
            title={t("settings.documents.rows.ocrInlineMaxPages")}
            description={t("settings.documents.help.ocrInlineMaxPages")}
            value={state.ocrInlineMaxPages}
            min={0}
            saving={savingPath === "documents.ocr.inline_max_pages"}
            onSave={(n) => void onSave("documents.ocr.inline_max_pages", n)}
          />
        </SettingsGroup>
      </section>

      <section>
        <SettingsSectionTitle>{t("settings.documents.sections.limits")}</SettingsSectionTitle>
        <SettingsGroup>
          <NumberRow
            title={t("settings.documents.rows.maxFileSizeMb")}
            description={t("settings.documents.help.maxFileSizeMb")}
            value={state.maxFileSizeMb}
            min={1}
            saving={savingPath === "documents.max_file_size_mb"}
            onSave={(n) => void onSave("documents.max_file_size_mb", n)}
          />
          <NumberRow
            title={t("settings.documents.rows.maxTextChars")}
            description={t("settings.documents.help.maxTextChars")}
            value={state.maxTextChars}
            min={1000}
            saving={savingPath === "documents.max_text_chars"}
            onSave={(n) => void onSave("documents.max_text_chars", n)}
          />
        </SettingsGroup>
      </section>
    </div>
  );
}

/** Accessible on/off switch (`role="switch"`) — the codebase has no shared
 *  switch primitive yet, so this builds the minimal WAI-ARIA button pattern
 *  directly rather than a checkbox styled to look like one. */
function ToggleSwitch({
  checked,
  disabled,
  label,
  onToggle,
}: {
  checked: boolean;
  disabled: boolean;
  label: string;
  onToggle: () => void;
}) {
  return (
    <button
      type="button"
      role="switch"
      aria-checked={checked}
      aria-label={label}
      disabled={disabled}
      onClick={onToggle}
      className={cn(
        "relative h-6 w-11 shrink-0 rounded-full transition-colors",
        "disabled:cursor-not-allowed disabled:opacity-50",
        checked ? "bg-primary" : "bg-muted",
      )}
    >
      <span
        aria-hidden
        className={cn(
          "block h-5 w-5 rounded-full bg-background shadow transition-transform",
          checked ? "translate-x-[22px]" : "translate-x-0.5",
        )}
      />
    </button>
  );
}

/** Numeric input row shared by the three document reading limits. Commits
 *  on Enter or Save; the Save button stays disabled until the draft is a
 *  valid integer that differs from the saved value. */
function NumberRow({
  title,
  description,
  value,
  min,
  saving,
  onSave,
}: {
  title: string;
  description: string;
  value: number;
  min: number;
  saving: boolean;
  onSave: (n: number) => void;
}) {
  const { t } = useTranslation();
  const [draft, setDraft] = useState(String(value));
  useEffect(() => setDraft(String(value)), [value]);

  const parsed = Number(draft);
  const valid = Number.isFinite(parsed) && Number.isInteger(parsed) && parsed >= min;
  const dirty = valid && parsed !== value;
  const commit = () => {
    if (!dirty) return;
    onSave(parsed);
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
          disabled={saving}
          className="h-8 w-[110px] rounded-full text-[13px]"
        />
        <Button
          size="sm"
          variant="outline"
          disabled={!dirty || saving}
          onClick={commit}
          aria-label={t("settings.documents.saveRowLabel", { row: title })}
          className="rounded-full"
        >
          {t("settings.config.save")}
        </Button>
      </div>
    </SettingsRow>
  );
}

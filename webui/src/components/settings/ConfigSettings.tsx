import { useCallback, useEffect, useMemo, useState } from "react";
import { ChevronDown, ChevronRight, Loader2 } from "lucide-react";
import { useTranslation } from "react-i18next";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { getConfig, setConfigValue } from "@/lib/api";
import { cn } from "@/lib/utils";

type Json = unknown;

/** A leaf the form can edit inline. Objects recurse; arrays/null are
 *  shown read-only (edit those with `durin config` for now). */
function isEditableScalar(value: Json): value is string | number | boolean {
  return (
    typeof value === "string" ||
    typeof value === "number" ||
    typeof value === "boolean"
  );
}

function isMaskedSecret(value: Json): boolean {
  return value === "***";
}

/** One editable scalar field. Local draft state; saves on demand. */
function ConfigField({
  path,
  value,
  saving,
  onSave,
}: {
  path: string;
  value: string | number | boolean;
  saving: boolean;
  onSave: (path: string, value: Json) => void;
}) {
  const { t } = useTranslation();
  const label = path.split(".").slice(-1)[0];

  if (typeof value === "boolean") {
    return (
      <div className="flex min-h-[52px] items-center justify-between gap-3 px-4 py-2.5 sm:px-5">
        <code className="min-w-0 truncate text-[13px] text-foreground/85">{label}</code>
        <Button
          size="sm"
          variant="outline"
          disabled={saving}
          onClick={() => onSave(path, !value)}
          className="w-[68px] rounded-full"
        >
          {value ? t("settings.config.on") : t("settings.config.off")}
        </Button>
      </div>
    );
  }

  return (
    <ConfigTextField
      path={path}
      label={label}
      value={value}
      numeric={typeof value === "number"}
      saving={saving}
      onSave={onSave}
    />
  );
}

function ConfigTextField({
  path,
  label,
  value,
  numeric,
  saving,
  onSave,
}: {
  path: string;
  label: string;
  value: string | number;
  numeric: boolean;
  saving: boolean;
  onSave: (path: string, value: Json) => void;
}) {
  const { t } = useTranslation();
  const [draft, setDraft] = useState(String(value));
  useEffect(() => setDraft(String(value)), [value]);
  const dirty = draft !== String(value);

  const commit = () => {
    if (!dirty) return;
    if (numeric) {
      const n = Number(draft);
      if (!Number.isFinite(n)) return;
      onSave(path, n);
    } else {
      onSave(path, draft);
    }
  };

  return (
    <div className="flex min-h-[52px] items-center justify-between gap-3 px-4 py-2.5 sm:px-5">
      <code className="min-w-0 truncate text-[13px] text-foreground/85">{label}</code>
      <div className="flex shrink-0 items-center gap-2">
        <Input
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter") commit();
          }}
          inputMode={numeric ? "numeric" : undefined}
          className="w-[220px]"
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
    </div>
  );
}

/** Recursively render a config subtree. */
function ConfigNode({
  path,
  value,
  saving,
  onSave,
}: {
  path: string;
  value: Json;
  saving: string | null;
  onSave: (path: string, value: Json) => void;
}) {
  const label = path.split(".").slice(-1)[0];

  if (isMaskedSecret(value)) {
    return (
      <div className="flex min-h-[52px] items-center justify-between gap-3 px-4 py-2.5 sm:px-5">
        <code className="truncate text-[13px] text-foreground/85">{label}</code>
        <span className="text-[12px] text-muted-foreground">•••• (managed)</span>
      </div>
    );
  }

  if (isEditableScalar(value)) {
    return (
      <ConfigField path={path} value={value} saving={saving === path} onSave={onSave} />
    );
  }

  if (Array.isArray(value) || value === null) {
    return (
      <div className="flex min-h-[52px] items-start justify-between gap-3 px-4 py-2.5 sm:px-5">
        <code className="truncate text-[13px] text-foreground/85">{label}</code>
        <span className="max-w-[60%] truncate text-right text-[12px] text-muted-foreground">
          {value === null ? "—" : JSON.stringify(value)}
        </span>
      </div>
    );
  }

  // Nested object — render its entries indented.
  const entries = Object.entries(value as Record<string, Json>);
  return (
    <div>
      <div className="px-4 pt-2.5 text-[12px] font-medium uppercase tracking-wide text-muted-foreground/70 sm:px-5">
        {label}
      </div>
      <div className="pl-3">
        {entries.map(([key, child]) => (
          <ConfigNode
            key={key}
            path={`${path}.${key}`}
            value={child}
            saving={saving}
            onSave={onSave}
          />
        ))}
      </div>
    </div>
  );
}

/** Collapsible top-level config section. */
function ConfigGroup({
  name,
  value,
  saving,
  onSave,
}: {
  name: string;
  value: Json;
  saving: string | null;
  onSave: (path: string, value: Json) => void;
}) {
  const [open, setOpen] = useState(false);
  const entries =
    value && typeof value === "object" && !Array.isArray(value)
      ? Object.entries(value as Record<string, Json>)
      : [];
  return (
    <div className="overflow-hidden rounded-[18px] border border-border/45 bg-card/86">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        className="flex w-full items-center gap-2 px-4 py-3 text-left sm:px-5"
      >
        {open ? (
          <ChevronDown className="h-4 w-4 text-muted-foreground" aria-hidden />
        ) : (
          <ChevronRight className="h-4 w-4 text-muted-foreground" aria-hidden />
        )}
        <span className="text-[14px] font-semibold text-foreground">{name}</span>
        <span className="ml-auto text-[12px] text-muted-foreground">
          {entries.length}
        </span>
      </button>
      {open ? (
        <div className="divide-y divide-border/40 border-t border-border/40">
          {entries.map(([key, child]) => (
            <ConfigNode
              key={key}
              path={`${name}.${key}`}
              value={child}
              saving={saving}
              onSave={onSave}
            />
          ))}
        </div>
      ) : null}
    </div>
  );
}

/** Phase C: the generic, schema-driven "All settings" section.
 *  Renders every config field from `GET /api/config` and writes single
 *  values through `POST /api/config/set`. */
export function ConfigSettings({ token }: { token: string }) {
  const { t } = useTranslation();
  const [config, setConfig] = useState<Record<string, Json> | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [saving, setSaving] = useState<string | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const snap = await getConfig(token);
      setConfig(snap.config as Record<string, Json>);
    } catch {
      setError(t("settings.config.loadError"));
    } finally {
      setLoading(false);
    }
  }, [token, t]);

  useEffect(() => {
    void load();
  }, [load]);

  const onSave = useCallback(
    async (path: string, value: Json) => {
      setSaving(path);
      setError(null);
      try {
        const next = await setConfigValue(token, path, value);
        setConfig(next as Record<string, Json>);
      } catch {
        setError(t("settings.config.saveError", { path }));
      } finally {
        setSaving(null);
      }
    },
    [token, t],
  );

  const sections = useMemo(
    () => (config ? Object.entries(config) : []),
    [config],
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
    <div className="space-y-3">
      <p className={cn("px-1 text-[13px] leading-5 text-muted-foreground")}>
        {t("settings.config.description")}
      </p>
      {error ? (
        <div className="rounded-[18px] border border-destructive/20 bg-destructive/5 px-4 py-3 text-[13px] text-destructive">
          {error}
        </div>
      ) : null}
      {sections.map(([name, value]) => (
        <ConfigGroup
          key={name}
          name={name}
          value={value}
          saving={saving}
          onSave={onSave}
        />
      ))}
    </div>
  );
}

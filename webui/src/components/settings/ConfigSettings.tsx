import { useCallback, useEffect, useMemo, useState } from "react";
import { ChevronDown, ChevronRight, Loader2 } from "lucide-react";
import { useTranslation } from "react-i18next";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { getConfig, setConfigValue } from "@/lib/api";
import { SettingsRow, settingsCardClass } from "./primitives";
import { MaskedSecret } from "@/components/settings/secrets/MaskedSecret";

type Json = unknown;

/** One flattened, addressable config value. `path` is the full dotted
 *  key the API writes to; `display` is the path relative to its group. */
interface Leaf {
  display: string;
  path: string;
  value: Json;
}

function isMaskedSecret(value: Json): boolean {
  return value === "***";
}

/** Detect a `${secret:NAME}` reference and pull the secret name out so
 *  the UI can present it as a managed handle (rotate value / disconnect)
 *  instead of a raw editable string. Format mirrors what
 *  `durin/security/secrets.py::resolve_secret` parses on the backend. */
const SECRET_REF_PATTERN = /^\$\{secret:([A-Za-z0-9_.-]+)\}$/;
function parseSecretRef(value: Json): string | null {
  if (typeof value !== "string") return null;
  const m = SECRET_REF_PATTERN.exec(value.trim());
  return m ? m[1] : null;
}

/** Walk a config subtree into editable leaves. Plain objects recurse so
 *  every scalar gets its own row; arrays and null stay whole (read-only). */
function flatten(value: Json, path: string, display: string, out: Leaf[]): void {
  if (value !== null && typeof value === "object" && !Array.isArray(value)) {
    const entries = Object.entries(value as Record<string, Json>);
    for (const [key, child] of entries) {
      flatten(child, `${path}.${key}`, display ? `${display}.${key}` : key, out);
    }
    return;
  }
  out.push({
    display: display || path.split(".").slice(-1)[0],
    path,
    value,
  });
}

/** A scalar (string/number) editor row. Local draft; saves on demand. */
function ConfigTextRow({
  leaf,
  numeric,
  busy,
  onSave,
}: {
  leaf: Leaf;
  numeric: boolean;
  busy: boolean;
  onSave: (path: string, value: Json) => void;
}) {
  const { t } = useTranslation();
  const [draft, setDraft] = useState(String(leaf.value));
  useEffect(() => setDraft(String(leaf.value)), [leaf.value]);
  const dirty = draft !== String(leaf.value);

  const commit = () => {
    if (!dirty) return;
    if (numeric) {
      const n = Number(draft);
      if (!Number.isFinite(n)) return;
      onSave(leaf.path, n);
    } else {
      onSave(leaf.path, draft);
    }
  };

  return (
    <SettingsRow title={leaf.display}>
      <div className="flex items-center gap-2">
        <Input
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter") commit();
          }}
          inputMode={numeric ? "numeric" : undefined}
          className="h-8 w-[220px] rounded-full text-[13px]"
        />
        <Button
          size="sm"
          variant="outline"
          disabled={!dirty || busy}
          onClick={commit}
          className="rounded-full"
        >
          {t("settings.config.save")}
        </Button>
      </div>
    </SettingsRow>
  );
}

/** A config leaf that holds a `${secret:NAME}` reference. Surfaced as a
 *  managed-handle badge with two actions:
 *  - Rotate: open a masked dialog to write a new value to the secret
 *    store (over the websocket — never on a URL). Updates EVERY config
 *    field that references `${secret:NAME}` in one move.
 *  - Disconnect: replace the ref with a plaintext input by writing an
 *    empty string to this single config path (the next render falls
 *    through to ConfigTextRow). */
function SecretRefRow({
  leaf,
  secretName,
  busy,
  onSave,
}: {
  leaf: Leaf; secretName: string; busy: boolean; onSave: (path: string, value: Json) => void;
}) {
  return (
    <SettingsRow title={leaf.display}>
      <MaskedSecret
        secretName={secretName}
        serviceLabel={deriveServiceLabel(leaf.path)}
        busy={busy}
        onDisconnect={() => onSave(leaf.path, "")}
      />
    </SettingsRow>
  );
}

function deriveServiceLabel(path: string): string {
  // `providers.zhipu.api_key` → `provider:zhipu`
  // `channels.telegram.bot_token` → `channel:telegram`
  // anything else → first dotted segment
  const parts = path.split(".");
  const head = parts[0] ?? "config";
  const mapping: Record<string, string> = {
    providers: "provider",
    channels: "channel",
  };
  const prefix = mapping[head] ?? head;
  const tail = parts[1] ?? "";
  return tail ? `${prefix}:${tail}` : prefix;
}

/** One config leaf, picking the right control for its type. */
function LeafRow({
  leaf,
  saving,
  onSave,
}: {
  leaf: Leaf;
  saving: string | null;
  onSave: (path: string, value: Json) => void;
}) {
  const { t } = useTranslation();
  const busy = saving === leaf.path;
  const { value } = leaf;

  if (isMaskedSecret(value)) {
    return (
      <SettingsRow title={leaf.display}>
        <span className="text-[12px] text-muted-foreground">
          {t("settings.config.managed")}
        </span>
      </SettingsRow>
    );
  }

  // Secret references render as a managed-handle badge with rotate /
  // disconnect actions instead of an editable text field, so the
  // operator can't accidentally turn a `${secret:KEY}` reference into
  // a literal plaintext value just by typing in the input.
  const secretName = parseSecretRef(value);
  if (secretName) {
    return (
      <SecretRefRow leaf={leaf} secretName={secretName} busy={busy} onSave={onSave} />
    );
  }

  if (typeof value === "boolean") {
    return (
      <SettingsRow title={leaf.display}>
        <Button
          size="sm"
          variant="outline"
          disabled={busy}
          onClick={() => onSave(leaf.path, !value)}
          className="min-w-[68px] rounded-full"
        >
          {value ? t("settings.config.on") : t("settings.config.off")}
        </Button>
      </SettingsRow>
    );
  }

  if (typeof value === "string" || typeof value === "number") {
    return (
      <ConfigTextRow
        leaf={leaf}
        numeric={typeof value === "number"}
        busy={busy}
        onSave={onSave}
      />
    );
  }

  // Array or null — shown read-only; edit those with `durin config`. The value
  // MUST be inline-block: `truncate` (overflow-hidden + max-width) is inert on an
  // inline <span>, which let long arrays (e.g. the allowlist) sprawl across and
  // overlap the row title. The full value stays reachable via the tooltip.
  const preview = value === null ? "—" : JSON.stringify(value);
  return (
    <SettingsRow title={leaf.display}>
      <span
        title={value === null ? undefined : preview}
        className="inline-block max-w-[280px] truncate align-middle text-right text-[12px] text-muted-foreground"
      >
        {preview}
      </span>
    </SettingsRow>
  );
}

/** A collapsible top-level config section. Uses the shared settings card
 *  chrome so it reads the same as every other settings group. */
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
  const { t } = useTranslation();
  const [open, setOpen] = useState(false);
  const leaves = useMemo(() => {
    const out: Leaf[] = [];
    flatten(value, name, "", out);
    return out;
  }, [value, name]);

  return (
    <div className={settingsCardClass}>
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        className="flex min-h-[56px] w-full items-center gap-2.5 px-4 py-3.5 text-left sm:px-5"
      >
        {open ? (
          <ChevronDown className="h-4 w-4 shrink-0 text-muted-foreground" aria-hidden />
        ) : (
          <ChevronRight className="h-4 w-4 shrink-0 text-muted-foreground" aria-hidden />
        )}
        <span className="text-[14px] font-medium text-foreground">{name}</span>
        <span className="ml-auto text-[12px] tabular-nums text-muted-foreground">
          {leaves.length}
        </span>
      </button>
      {open ? (
        <div className="divide-y divide-border/45 border-t border-border/45">
          {leaves.length === 0 ? (
            <div className="px-4 py-3.5 text-[13px] text-muted-foreground sm:px-5">
              {t("settings.config.empty")}
            </div>
          ) : (
            leaves.map((leaf) => (
              <LeafRow
                key={leaf.path}
                leaf={leaf}
                saving={saving}
                onSave={onSave}
              />
            ))
          )}
        </div>
      ) : null}
    </div>
  );
}

/** The generic, schema-driven "All settings" section. Renders every
 *  config field from `GET /api/config` and writes single values through
 *  `POST /api/config/set`. */
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
      <p className="px-1 text-[13px] leading-5 text-muted-foreground">
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

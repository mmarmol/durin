import { useCallback, useEffect, useMemo, useState } from "react";
import { ChevronDown, ChevronRight, Loader2 } from "lucide-react";
import { useTranslation } from "react-i18next";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { getConfig, setConfigValue } from "@/lib/api";
import { SettingsRow, settingsCardClass } from "./primitives";
import { MaskedSecret } from "@/components/settings/secrets/MaskedSecret";

type Json = unknown;
type SchemaNode = Record<string, unknown>;

/** One flattened, addressable config value. `path` is the full dotted
 *  key the API writes to; `display` is the path relative to its group;
 *  `keys` are the keys from the config root down to the value. */
interface Leaf {
  display: string;
  path: string;
  keys: string[];
  value: Json;
}

/** What the config schema says about a number field. */
interface NumberSpec {
  integer: boolean;
  nullable: boolean;
  minimum?: number;
}

/** `node` with its `$ref` chain followed, plus each `anyOf` branch. */
function schemaVariants(node: SchemaNode, defs: Record<string, SchemaNode>): SchemaNode[] {
  const resolve = (n: SchemaNode): SchemaNode => {
    let cur = n;
    for (let hops = 0; typeof cur.$ref === "string" && hops < 32; hops++) {
      const next = defs[String(cur.$ref).split("/").pop() ?? ""];
      if (!next) break;
      cur = next;
    }
    return cur;
  };
  const resolved = resolve(node);
  const branches = Array.isArray(resolved.anyOf) ? (resolved.anyOf as SchemaNode[]) : [];
  return [resolved, ...branches.map(resolve)];
}

/** The number spec of the field at `keys` in the config's JSON schema, or
 *  null when the schema does not describe it as a number. A key of a map
 *  (a model or preset name) is matched through `additionalProperties`. */
function numberSpecAt(schema: SchemaNode | null, keys: string[]): NumberSpec | null {
  if (!schema) return null;
  const defs = (schema.$defs ?? {}) as Record<string, SchemaNode>;
  let node: SchemaNode | null = schema;
  for (const key of keys) {
    if (!node) return null;
    const variants: SchemaNode[] = schemaVariants(node, defs);
    const field = variants
      .map((v) => (v.properties as Record<string, SchemaNode> | undefined)?.[key])
      .find((n) => n !== undefined);
    const entry = variants
      .map((v) => v.additionalProperties)
      .find((n): n is SchemaNode => typeof n === "object" && n !== null);
    node = field ?? entry ?? null;
  }
  if (!node) return null;
  const variants = schemaVariants(node, defs);
  const numeric = variants.find((v) => v.type === "integer" || v.type === "number");
  if (!numeric) return null;
  return {
    integer: numeric.type === "integer",
    nullable: variants.some((v) => v.type === "null"),
    minimum: typeof numeric.minimum === "number" ? numeric.minimum : undefined,
  };
}

/** What a number field's draft saves as: null when it is empty and the
 *  field may be null, the number when the field accepts it, otherwise
 *  undefined — nothing to save. An empty draft never becomes 0, which is
 *  what `Number("")` returns. */
function parseNumberDraft(draft: string, spec: NumberSpec): number | null | undefined {
  const text = draft.trim();
  if (text === "") return spec.nullable ? null : undefined;
  const n = Number(text);
  if (!Number.isFinite(n)) return undefined;
  if (spec.integer && !Number.isInteger(n)) return undefined;
  if (spec.minimum !== undefined && n < spec.minimum) return undefined;
  return n;
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

/** The API path of `key` under `path`. A key that contains a dot (a model
 *  name such as `glm-5.3`), a bracket or a quote goes in brackets, since the
 *  dots of a plain dotted path would split it into several keys. */
function childPath(path: string, key: string): string {
  if (!/[.[\]"']/.test(key)) return `${path}.${key}`;
  const quote = key.includes('"') ? "'" : '"';
  return `${path}[${quote}${key}${quote}]`;
}

/** Walk a config subtree into editable leaves. Plain objects recurse so
 *  every scalar gets its own row; arrays and null stay whole. */
function flatten(value: Json, path: string, display: string, out: Leaf[], keys: string[]): void {
  if (value !== null && typeof value === "object" && !Array.isArray(value)) {
    const entries = Object.entries(value as Record<string, Json>);
    for (const [key, child] of entries) {
      flatten(child, childPath(path, key), display ? `${display}.${key}` : key, out, [...keys, key]);
    }
    return;
  }
  out.push({
    display: display || path.split(".").slice(-1)[0],
    path,
    keys,
    value,
  });
}

/** A scalar (string/number) editor row. Local draft; saves on demand. A
 *  number row follows its schema spec: an empty draft saves null when the
 *  field may be null, and a draft the field does not accept is not saved. */
function ConfigTextRow({
  leaf,
  number,
  busy,
  onSave,
}: {
  leaf: Leaf;
  number: NumberSpec | null;
  busy: boolean;
  onSave: (path: string, value: Json) => void;
}) {
  const { t } = useTranslation();
  const shown = leaf.value === null ? "" : String(leaf.value);
  const [draft, setDraft] = useState(shown);
  useEffect(() => setDraft(shown), [shown]);
  const dirty = draft !== shown;
  const parsed = number ? parseNumberDraft(draft, number) : draft;
  const valid = parsed !== undefined;

  const commit = () => {
    if (!dirty || !valid) return;
    onSave(leaf.path, parsed);
  };

  return (
    <SettingsRow title={leaf.display}>
      <div className="flex items-center gap-2">
        <Input
          value={draft}
          aria-label={leaf.display}
          onChange={(e) => setDraft(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter") commit();
          }}
          inputMode={number ? "numeric" : undefined}
          placeholder={number?.nullable ? t("settings.config.unset") : undefined}
          aria-invalid={dirty && !valid ? true : undefined}
          className="h-8 w-[220px] rounded-full text-[13px]"
        />
        <Button
          size="sm"
          variant="outline"
          disabled={!dirty || !valid || busy}
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
        busy={busy}
        onDisconnect={() => onSave(leaf.path, "")}
      />
    </SettingsRow>
  );
}

/** One config leaf, picking the right control for its type. */
function LeafRow({
  leaf,
  schema,
  saving,
  onSave,
}: {
  leaf: Leaf;
  schema: SchemaNode | null;
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

  // A number — or a null the schema types as a number, such as an unset
  // output cap — is edited against its schema spec. Without one (the schema
  // does not describe the key) a number still edits, but never saves empty.
  const spec = numberSpecAt(schema, leaf.keys);
  if (typeof value === "number" || (value === null && spec)) {
    return (
      <ConfigTextRow
        leaf={leaf}
        number={spec ?? { integer: false, nullable: false }}
        busy={busy}
        onSave={onSave}
      />
    );
  }
  if (typeof value === "string") {
    return <ConfigTextRow leaf={leaf} number={null} busy={busy} onSave={onSave} />;
  }

  // Array or other null — shown read-only; edit those with `durin config`. The value
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
  schema,
  saving,
  onSave,
}: {
  name: string;
  value: Json;
  schema: SchemaNode | null;
  saving: string | null;
  onSave: (path: string, value: Json) => void;
}) {
  const { t } = useTranslation();
  const [open, setOpen] = useState(false);
  const leaves = useMemo(() => {
    const out: Leaf[] = [];
    flatten(value, name, "", out, [name]);
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
                schema={schema}
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
  const [schema, setSchema] = useState<SchemaNode | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [saving, setSaving] = useState<string | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const snap = await getConfig(token);
      setConfig(snap.config as Record<string, Json>);
      setSchema(snap.json_schema ?? null);
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

  // "loops" is migration-only legacy input (read once by a boot migration
  // into automations.*, then ignored — the loops subsystem no longer
  // exists), so the editor must not offer it as a section to edit.
  const sections = useMemo(
    () => (config ? Object.entries(config).filter(([name]) => name !== "loops") : []),
    [config],
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
          schema={schema}
          saving={saving}
          onSave={onSave}
        />
      ))}
    </div>
  );
}

import { useCallback, useEffect, useState } from "react";
import { ChevronDown, Loader2, Mail, MessageCircle, Plug, Send, type LucideIcon } from "lucide-react";
import { useTranslation } from "react-i18next";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { ChannelSecretField } from "@/components/settings/secrets/ChannelSecretField";
import { useClient } from "@/providers/ClientProvider";
import { getConfig, listChannels, setConfigValue, type ChannelField, type ChannelInfo } from "@/lib/api";
import { TelegramGuided } from "@/components/settings/channels/TelegramGuided";

// Groups that are always visible in the form.
const ESSENTIAL_GROUPS = ["access", "imap", "smtp"] as const;
// Groups hidden behind "Advanced" collapsible.
const ADVANCED_GROUPS = ["behavior", "security", "attachments"] as const;

const GROUP_LABEL: Record<string, string> = {
  access: "settings.channels.groupAccess",
  imap: "settings.channels.groupImap",
  smtp: "settings.channels.groupSmtp",
  behavior: "settings.channels.groupBehavior",
  security: "settings.channels.groupSecurity",
  attachments: "settings.channels.groupAttachments",
  "": "",
};

/** Build an env-var-safe secret name for a channel credential. */
function secretName(channel: string, field: string): string {
  return `${channel}_${field}`.toUpperCase().replace(/[^A-Z0-9_]/g, "_");
}

/** Generic per-type glyphs so channel rows carry an icon like the Providers
 *  tab does (lucide has no brand marks for these platforms — match by kind). */
const CHANNEL_ICONS: Record<string, LucideIcon> = {
  email: Mail,
  websocket: Plug,
  telegram: Send,
};

/** Icon container mirroring the Providers tab's ProviderIcon treatment. */
function ChannelIcon({ name }: { name: string }) {
  const Icon = CHANNEL_ICONS[name] ?? MessageCircle;
  return (
    <span className="grid h-10 w-10 shrink-0 place-items-center rounded-2xl bg-muted text-foreground/82 shadow-[inset_0_0_0_1px_rgba(0,0,0,0.025)] dark:bg-muted/70">
      <Icon className="h-5 w-5" strokeWidth={2} aria-hidden />
    </span>
  );
}

/** Green status pill matching the Providers "connected" pill. */
function ActivePill({ label }: { label: string }) {
  return (
    <span className="inline-flex shrink-0 items-center gap-1.5 rounded-full bg-emerald-500/12 px-2.5 py-0.5 text-[11px] text-emerald-600 dark:text-emerald-400">
      <span className="h-1.5 w-1.5 rounded-full bg-current" aria-hidden />
      {label}
    </span>
  );
}

/** One typed field → the right input. `value` is the channel's current value. */
function FieldInput({
  channel, field, value, token, busy, onChange,
}: {
  channel: ChannelInfo;
  field: ChannelField;
  value: unknown;
  token: string;
  busy: boolean;
  onChange: (v: unknown) => void;
}) {
  const { t } = useTranslation();
  if (field.type === "secret") {
    const ref = typeof value === "string" ? value : null;
    const name = secretName(channel.name, field.name);
    return (
      <ChannelSecretField
        secretRef={ref}
        secretName={name}
        serviceLabel={`channel:${channel.name}`}
        busy={busy}
        token={token}
        onSet={(r) => onChange(r)}
        onClear={() => onChange("")}
      />
    );
  }
  if (field.type === "bool") {
    // consent_granted gets a labelled checkbox so the consent text is always visible.
    if (field.name === "consent_granted") {
      return (
        <label className="flex items-center gap-2 text-[13px] text-foreground/80">
          <input
            type="checkbox"
            checked={Boolean(value)}
            disabled={busy}
            onChange={(e) => onChange(e.target.checked)}
          />
          {t("settings.channels.consentLabel")}
        </label>
      );
    }
    return (
      <input
        type="checkbox"
        checked={Boolean(value)}
        disabled={busy}
        onChange={(e) => onChange(e.target.checked)}
      />
    );
  }
  if (field.type === "int") {
    return (
      <Input
        type="number"
        defaultValue={String(value ?? "")}
        disabled={busy}
        onBlur={(e) => onChange(Number(e.target.value))}
        className="w-[160px]"
      />
    );
  }
  if (field.type === "string_list") {
    const text = Array.isArray(value) ? value.join(", ") : "";
    return (
      <Input
        defaultValue={text}
        disabled={busy}
        onBlur={(e) =>
          onChange(
            e.target.value
              .split(",")
              .map((s) => s.trim())
              .filter(Boolean),
          )
        }
        className="w-[280px]"
        placeholder="a@b.com, c@d.com"
      />
    );
  }
  return (
    <Input
      defaultValue={String(value ?? "")}
      disabled={busy}
      onBlur={(e) => onChange(e.target.value)}
      className="w-[280px]"
    />
  );
}

/** Renders one named group of fields with a header. */
function FieldGroup({
  groupKey,
  channel,
  channelValues,
  token,
  busy,
  onFieldChange,
}: {
  groupKey: string;
  channel: ChannelInfo;
  channelValues: Record<string, unknown>;
  token: string;
  busy: boolean;
  onFieldChange: (fieldName: string, value: unknown) => void;
}) {
  const { t } = useTranslation();
  const groupFields = channel.fields.filter((f) => f.group === groupKey);
  if (groupFields.length === 0) return null;
  const labelKey = GROUP_LABEL[groupKey];
  return (
    <div className="space-y-2">
      {labelKey ? (
        <div className="text-[11px] font-semibold uppercase tracking-wide text-muted-foreground">
          {t(labelKey)}
        </div>
      ) : null}
      {groupFields.map((field) => (
        <div key={field.name} className="flex flex-wrap items-center gap-2">
          {/* consent_granted renders its own inline label inside FieldInput */}
          {field.name !== "consent_granted" ? (
            <span className="w-[160px] shrink-0 text-[13px] text-foreground/80">
              {t(`settings.channels.field.${field.name}`, field.name)}
            </span>
          ) : null}
          <FieldInput
            channel={channel}
            field={field}
            value={channelValues[field.name]}
            token={token}
            busy={busy}
            onChange={(v) => onFieldChange(field.name, v)}
          />
        </div>
      ))}
    </div>
  );
}

/** One channel: rendered according to its schema type. */
function ChannelRow({
  channel,
  channelValues,
  token,
  busy,
  onEnable,
  onDisable,
  onFieldChange,
  onChanged,
}: {
  channel: ChannelInfo;
  channelValues: Record<string, unknown>;
  token: string;
  busy: boolean;
  onEnable: (credential: string) => void;
  onDisable: () => void;
  onFieldChange: (fieldName: string, value: unknown) => void;
  onChanged: () => void;
}) {
  const { t, i18n } = useTranslation();
  const [open, setOpen] = useState(false);
  const [credential, setCredential] = useState("");
  const [advancedOpen, setAdvancedOpen] = useState(false);

  const hasFields = channel.fields.length > 0;
  const isActive = channel.enabled || channel.always_on;

  // Prefer a localized description; fall back to the backend string.
  const descKey = `settings.channels.desc.${channel.name}`;
  const desc = i18n.exists(descKey) ? t(descKey) : channel.description;

  // Whether this channel has any advanced-group fields.
  const hasAdvancedFields = ADVANCED_GROUPS.some(
    (g) => channel.fields.some((f) => f.group === g),
  );

  return (
    <div className="px-4 py-3 sm:px-5">
      {/* Accordion header — clicking anywhere here toggles open, except the action buttons */}
      <div
        className="flex cursor-pointer items-center justify-between gap-3"
        onClick={() => setOpen((v) => !v)}
        aria-expanded={open}
      >
        <div className="flex min-w-0 items-center gap-3">
          <ChannelIcon name={channel.name} />
          <div className="min-w-0">
            <div className="text-[14px] font-medium text-foreground">
              {channel.display_name}
            </div>
            {isActive ? (
              <ActivePill
                label={
                  channel.always_on
                    ? t("settings.channels.alwaysOn")
                    : t("settings.channels.enabled")
                }
              />
            ) : (
              <div className="text-[12px] text-muted-foreground">
                {t("settings.channels.disabled")}
              </div>
            )}
          </div>
        </div>
        {/* Action buttons + chevron — stopPropagation keeps buttons independent of accordion */}
        <div
          className="flex shrink-0 items-center gap-2"
          onClick={(e) => e.stopPropagation()}
        >
          {channel.always_on ? null : (
            <>
              {channel.enabled ? (
                <Button
                  size="sm"
                  variant="ghost"
                  disabled={busy}
                  onClick={onDisable}
                  className="rounded-full text-muted-foreground"
                >
                  {t("settings.channels.disable")}
                </Button>
              ) : null}
              {!hasFields ? (
                <Button
                  size="sm"
                  variant="outline"
                  disabled={busy}
                  onClick={() => setOpen((v) => !v)}
                  className="rounded-full"
                >
                  {channel.enabled
                    ? t("settings.channels.reconfigure")
                    : t("settings.channels.enable")}
                </Button>
              ) : null}
              {hasFields && !channel.enabled ? (
                <Button
                  size="sm"
                  variant="outline"
                  disabled={busy}
                  onClick={() => void onEnable("")}
                  className="rounded-full"
                >
                  {t("settings.channels.enable")}
                </Button>
              ) : null}
            </>
          )}
          <ChevronDown
            className={`h-4 w-4 transition-transform text-muted-foreground ${open ? "" : "-rotate-90"}`}
            aria-hidden
          />
        </div>
      </div>

      {/* Accordion body — description + config form, gated by open */}
      {open ? (
        <div>
          {/* description line for always_on and schema-driven channels */}
          {(channel.always_on || hasFields) && desc ? (
            <p className="mt-1 text-[12px] text-muted-foreground">{desc}</p>
          ) : null}

          {/* Telegram gets its own guided/manual panel instead of the generic form */}
          {channel.name === "telegram" ? (
            <TelegramGuided
              channel={channel}
              channelValues={channelValues}
              token={token}
              onChanged={onChanged}
            />
          ) : null}

          {/* Schema-driven grouped field form (websocket / email) */}
          {hasFields && channel.name !== "telegram" ? (
            <div className="mt-3 space-y-4">
              {ESSENTIAL_GROUPS.map((g) => (
                <FieldGroup
                  key={g}
                  groupKey={g}
                  channel={channel}
                  channelValues={channelValues}
                  token={token}
                  busy={busy}
                  onFieldChange={onFieldChange}
                />
              ))}
              {/* Ungrouped fields (group === "") */}
              <FieldGroup
                groupKey=""
                channel={channel}
                channelValues={channelValues}
                token={token}
                busy={busy}
                onFieldChange={onFieldChange}
              />
              {hasAdvancedFields ? (
                <div className="mt-3">
                  <button
                    type="button"
                    onClick={() => setAdvancedOpen((v) => !v)}
                    aria-expanded={advancedOpen}
                    className="flex items-center gap-1.5 text-[12px] font-medium text-muted-foreground hover:text-foreground"
                  >
                    <ChevronDown
                      className={`h-3.5 w-3.5 transition-transform ${advancedOpen ? "" : "-rotate-90"}`}
                      aria-hidden
                    />
                    {t("settings.channels.advanced")}
                  </button>
                  {advancedOpen ? (
                    <div className="mt-3 space-y-4">
                      {ADVANCED_GROUPS.map((g) => (
                        <FieldGroup
                          key={g}
                          groupKey={g}
                          channel={channel}
                          channelValues={channelValues}
                          token={token}
                          busy={busy}
                          onFieldChange={onFieldChange}
                        />
                      ))}
                    </div>
                  ) : null}
                </div>
              ) : null}
            </div>
          ) : null}

          {/* Legacy single-credential path: channels with empty fields (slack/discord/etc) */}
          {!hasFields && channel.name !== "telegram" ? (
            <div className="mt-3 flex flex-wrap items-center gap-2">
              {channel.credential_field ? (
                <Input
                  type="password"
                  value={credential}
                  onChange={(e) => setCredential(e.target.value)}
                  placeholder={t("settings.channels.credentialPlaceholder", {
                    field: channel.credential_field,
                  })}
                  className="w-[280px]"
                />
              ) : (
                <span className="text-[12px] text-muted-foreground">
                  {t("settings.channels.noCredential")}
                </span>
              )}
              <Button
                size="sm"
                variant="outline"
                disabled={busy}
                onClick={() => {
                  onEnable(credential);
                  setCredential("");
                  setOpen(false);
                }}
                className="rounded-full"
              >
                {t("settings.channels.save")}
              </Button>
            </div>
          ) : null}
        </div>
      ) : null}
    </div>
  );
}

/** Card container shared by both Active and Available sections. */
function ChannelSection({
  channels,
  configValues,
  token,
  busy,
  onEnable,
  onDisable,
  onFieldChange,
  onChanged,
}: {
  channels: ChannelInfo[];
  configValues: Record<string, Record<string, unknown>>;
  token: string;
  busy: string | null;
  onEnable: (channel: ChannelInfo, credential: string) => void;
  onDisable: (channel: ChannelInfo) => void;
  onFieldChange: (channel: ChannelInfo, fieldName: string, value: unknown) => void;
  onChanged: () => void;
}) {
  return (
    <div className="overflow-hidden rounded-[22px] border border-border/45 bg-card/86">
      <div className="divide-y divide-border/45">
        {channels.map((channel) => (
          <ChannelRow
            key={channel.name}
            channel={channel}
            channelValues={configValues[channel.name] ?? {}}
            token={token}
            busy={busy === channel.name}
            onEnable={(credential) => onEnable(channel, credential)}
            onDisable={() => onDisable(channel)}
            onFieldChange={(fieldName, value) => onFieldChange(channel, fieldName, value)}
            onChanged={onChanged}
          />
        ))}
      </div>
    </div>
  );
}

/** Curated Channels section — enable a channel and set its credential.
 *  Enabling from scratch is config the generic form can't create. */
export function ChannelsSettings({ token }: { token: string }) {
  const { t } = useTranslation();
  const { client } = useClient();
  const [channels, setChannels] = useState<ChannelInfo[]>([]);
  const [configValues, setConfigValues] = useState<Record<string, Record<string, unknown>>>({});
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const [ch, snap] = await Promise.all([listChannels(token), getConfig(token)]);
      setChannels(ch);
      // Extract per-channel values from the config snapshot: config.channels.<name>.*
      const raw = snap.config as Record<string, unknown>;
      const channelsRaw = (raw.channels ?? {}) as Record<string, unknown>;
      const perChannel: Record<string, Record<string, unknown>> = {};
      for (const c of ch) {
        const cv = channelsRaw[c.name];
        perChannel[c.name] =
          cv && typeof cv === "object" && !Array.isArray(cv)
            ? (cv as Record<string, unknown>)
            : {};
      }
      setConfigValues(perChannel);
    } catch {
      setError(t("settings.channels.loadError"));
    } finally {
      setLoading(false);
    }
  }, [token, t]);

  useEffect(() => {
    void load();
  }, [load]);

  const enable = useCallback(
    async (channel: ChannelInfo, credential: string) => {
      setBusy(channel.name);
      setError(null);
      try {
        if (channel.credential_field && credential.trim()) {
          const name = secretName(channel.name, channel.credential_field);
          await client.storeSecret({
            name,
            value: credential.trim(),
            service: `channel:${channel.name}`,
            scope: [`channel:${channel.name}`],
          });
          await setConfigValue(
            token,
            `channels.${channel.name}.${channel.credential_field}`,
            `\${secret:${name}}`,
          );
        }
        await setConfigValue(token, `channels.${channel.name}.enabled`, true);
        await load();
      } catch {
        setError(t("settings.channels.saveError"));
      } finally {
        setBusy(null);
      }
    },
    [token, load, t, client],
  );

  const disable = useCallback(
    async (channel: ChannelInfo) => {
      setBusy(channel.name);
      setError(null);
      try {
        await setConfigValue(token, `channels.${channel.name}.enabled`, false);
        await load();
      } catch {
        setError(t("settings.channels.saveError"));
      } finally {
        setBusy(null);
      }
    },
    [token, load, t],
  );

  const handleFieldChange = useCallback(
    async (channel: ChannelInfo, fieldName: string, value: unknown) => {
      setBusy(channel.name);
      setError(null);
      try {
        await setConfigValue(token, `channels.${channel.name}.${fieldName}`, value);
        await load();
      } catch {
        setError(t("settings.channels.saveError"));
      } finally {
        setBusy(null);
      }
    },
    [token, load, t],
  );

  if (loading) {
    return (
      <div className="flex h-40 items-center justify-center text-sm text-muted-foreground">
        <Loader2 className="mr-2 h-4 w-4 animate-spin" />
        {t("settings.status.loading")}
      </div>
    );
  }

  // Split into active (always_on || enabled) and available (the rest).
  // Active: always_on first, then enabled.
  const active = [
    ...channels.filter((c) => c.always_on),
    ...channels.filter((c) => !c.always_on && c.enabled),
  ];
  const available = channels.filter((c) => !c.always_on && !c.enabled);

  return (
    <div className="space-y-4">
      <p className="px-1 text-[13px] leading-5 text-muted-foreground">
        {t("settings.channels.description")}
      </p>
      {error ? (
        <div className="rounded-[18px] border border-destructive/20 bg-destructive/5 px-4 py-3 text-[13px] text-destructive">
          {error}
        </div>
      ) : null}

      {active.length > 0 ? (
        <div>
          <div className="mb-2 px-1 text-[12px] font-medium uppercase tracking-wide text-muted-foreground/80">
            {t("settings.channels.sectionActive")}
          </div>
          <ChannelSection
            channels={active}
            configValues={configValues}
            token={token}
            busy={busy}
            onEnable={enable}
            onDisable={disable}
            onFieldChange={handleFieldChange}
            onChanged={load}
          />
        </div>
      ) : null}

      {available.length > 0 ? (
        <div>
          <div className="mb-2 px-1 text-[12px] font-medium uppercase tracking-wide text-muted-foreground/80">
            {t("settings.channels.sectionAvailable")} ({available.length})
          </div>
          <ChannelSection
            channels={available}
            configValues={configValues}
            token={token}
            busy={busy}
            onEnable={enable}
            onDisable={disable}
            onFieldChange={handleFieldChange}
            onChanged={load}
          />
        </div>
      ) : null}
    </div>
  );
}

import { useCallback, useEffect, useState } from "react";
import { Loader2, Mail, MessageCircle, Plug, Send, type LucideIcon } from "lucide-react";
import { useTranslation } from "react-i18next";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { ChannelSecretField } from "@/components/settings/secrets/ChannelSecretField";
import { useClient } from "@/providers/ClientProvider";
import { getConfig, listChannels, setConfigValue, type ChannelField, type ChannelInfo } from "@/lib/api";

const GROUP_ORDER = ["consent", "imap", "smtp", "behavior", "security", "attachments", ""];
const GROUP_LABEL: Record<string, string> = {
  consent: "settings.channels.groupConsent",
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
        help={field.name === "token" ? t("settings.channels.wsTokenHelp") : undefined}
        busy={busy}
        token={token}
        onSet={(r) => onChange(r)}
        onClear={() => onChange("")}
      />
    );
  }
  if (field.type === "bool") {
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

/** One channel: rendered according to its schema type. */
function ChannelRow({
  channel,
  channelValues,
  token,
  busy,
  onEnable,
  onDisable,
  onFieldChange,
}: {
  channel: ChannelInfo;
  channelValues: Record<string, unknown>;
  token: string;
  busy: boolean;
  onEnable: (credential: string) => void;
  onDisable: () => void;
  onFieldChange: (fieldName: string, value: unknown) => void;
}) {
  const { t } = useTranslation();
  const [open, setOpen] = useState(false);
  const [credential, setCredential] = useState("");

  const hasFields = channel.fields.length > 0;

  return (
    <div className="px-4 py-3 sm:px-5">
      <div className="flex items-center justify-between gap-3">
        <div className="flex min-w-0 items-center gap-3">
          <ChannelIcon name={channel.name} />
          <div className="min-w-0">
            <div className="text-[14px] font-medium text-foreground">
              {channel.display_name}
            </div>
            <div className="text-[12px] text-muted-foreground">
              {channel.always_on
                ? t("settings.channels.alwaysOn")
                : channel.enabled
                  ? t("settings.channels.enabled")
                  : t("settings.channels.disabled")}
            </div>
          </div>
        </div>
        <div className="flex shrink-0 items-center gap-2">
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
        </div>
      </div>

      {/* description line for always_on and schema-driven channels */}
      {(channel.always_on || hasFields) && channel.description ? (
        <p className="mt-1 text-[12px] text-muted-foreground">{channel.description}</p>
      ) : null}

      {/* Schema-driven grouped field form (websocket / email) */}
      {hasFields ? (
        <div className="mt-3 space-y-4">
          {GROUP_ORDER.map((g) => {
            const groupFields = channel.fields.filter((f) => f.group === g);
            if (groupFields.length === 0) return null;
            const label = GROUP_LABEL[g] ? t(GROUP_LABEL[g]) : null;
            return (
              <div key={g} className="space-y-2">
                {label ? (
                  <div className="text-[11px] font-semibold uppercase tracking-wide text-muted-foreground">
                    {label}
                  </div>
                ) : null}
                {groupFields.map((field) => (
                  <div key={field.name} className="flex flex-wrap items-center gap-2">
                    <span className="w-[160px] shrink-0 text-[13px] text-foreground/80">
                      {field.name}
                    </span>
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
          })}
        </div>
      ) : null}

      {/* Legacy single-credential path: channels with empty fields (telegram/slack/discord) */}
      {!hasFields && open ? (
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
      <div className="overflow-hidden rounded-[22px] border border-border/45 bg-card/86">
        <div className="divide-y divide-border/45">
          {channels.map((channel) => (
            <ChannelRow
              key={channel.name}
              channel={channel}
              channelValues={configValues[channel.name] ?? {}}
              token={token}
              busy={busy === channel.name}
              onEnable={(credential) => void enable(channel, credential)}
              onDisable={() => void disable(channel)}
              onFieldChange={(fieldName, value) => void handleFieldChange(channel, fieldName, value)}
            />
          ))}
        </div>
      </div>
    </div>
  );
}

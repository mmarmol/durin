import { useCallback, useEffect, useState } from "react";
import { Loader2, Mail, MessageCircle, Plug, Send, type LucideIcon } from "lucide-react";
import { useTranslation } from "react-i18next";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { listChannels, setConfigValue, type ChannelInfo } from "@/lib/api";
import { useClient } from "@/providers/ClientProvider";

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

/** One channel: enable/disable + (when enabling) its credential. */
function ChannelRow({
  channel,
  busy,
  onEnable,
  onDisable,
}: {
  channel: ChannelInfo;
  busy: boolean;
  onEnable: (credential: string) => void;
  onDisable: () => void;
}) {
  const { t } = useTranslation();
  const [open, setOpen] = useState(false);
  const [credential, setCredential] = useState("");

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
              {channel.enabled
                ? t("settings.channels.enabled")
                : t("settings.channels.disabled")}
            </div>
          </div>
        </div>
        <div className="flex shrink-0 items-center gap-2">
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
        </div>
      </div>
      {open ? (
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
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      setChannels(await listChannels(token));
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
    [token, client, load, t],
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
              busy={busy === channel.name}
              onEnable={(credential) => void enable(channel, credential)}
              onDisable={() => void disable(channel)}
            />
          ))}
        </div>
      </div>
    </div>
  );
}

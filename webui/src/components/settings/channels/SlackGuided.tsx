import { useCallback, useEffect, useState, type ReactNode } from "react";
import { ExternalLink, Check, Copy, X, UserCheck } from "lucide-react";
import { useTranslation } from "react-i18next";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { useClient } from "@/providers/ClientProvider";
import {
  setConfigValue,
  startChannel,
  getSlackManifest,
  testSlackTokens,
  getSlackPairing,
  approveSlackPairing,
  denySlackPairing,
  revokeSlackPairing,
  type ChannelInfo,
  type SlackPairing,
  type SlackTestResult,
} from "@/lib/api";

// ---------- mode switch ----------------------------------------------------

function ModeSwitch({
  mode,
  onChange,
}: {
  mode: "guided" | "manual";
  onChange: (m: "guided" | "manual") => void;
}) {
  const { t } = useTranslation();
  return (
    <div className="inline-flex h-8 items-center rounded-full bg-muted p-0.5 text-[12px] font-medium text-muted-foreground">
      <button
        type="button"
        onClick={() => onChange("guided")}
        className={
          "rounded-full px-3 py-1 transition-colors " +
          (mode === "guided"
            ? "bg-background text-foreground shadow-sm"
            : "hover:text-foreground")
        }
      >
        {t("settings.channels.slack.modeGuided")}
      </button>
      <button
        type="button"
        onClick={() => onChange("manual")}
        className={
          "rounded-full px-3 py-1 transition-colors " +
          (mode === "manual"
            ? "bg-background text-foreground shadow-sm"
            : "hover:text-foreground")
        }
      >
        {t("settings.channels.slack.modeManual")}
      </button>
    </div>
  );
}

// ---------- pairing panel --------------------------------------------------

function PairingPanel({ token }: { token: string }) {
  const { t } = useTranslation();
  const [pairing, setPairing] = useState<SlackPairing | null>(null);
  const [actioning, setActioning] = useState<string | null>(null);

  const fetchPairing = useCallback(async () => {
    try {
      const data = await getSlackPairing(token);
      setPairing(data);
    } catch {
      // ignore fetch errors; we'll retry on the next interval tick
    }
  }, [token]);

  useEffect(() => {
    void fetchPairing();
    const id = setInterval(() => void fetchPairing(), 5000);
    return () => clearInterval(id);
  }, [fetchPairing]);

  const act = useCallback(
    async (action: () => Promise<unknown>, key: string) => {
      setActioning(key);
      try {
        await action();
        await fetchPairing();
      } finally {
        setActioning(null);
      }
    },
    [fetchPairing],
  );

  return (
    <div className="mt-3 space-y-3">
      <p className="text-[13px] text-muted-foreground">
        {t("settings.channels.slack.pairingInstruction")}
      </p>

      {pairing && pairing.pending.length > 0 ? (
        <div className="space-y-1.5">
          <div className="text-[11px] font-semibold uppercase tracking-wide text-muted-foreground">
            {t("settings.channels.slack.pendingRequests")}
          </div>
          {pairing.pending.map((req) => (
            <div
              key={req.code}
              className="flex flex-wrap items-center gap-2 rounded-xl border border-border/40 bg-muted/30 px-3 py-2 text-[13px]"
            >
              <span className="flex-1 font-mono text-[12px] text-foreground/70">
                {req.sender_id}
              </span>
              <span className="text-[11px] text-muted-foreground">
                #{req.code}
              </span>
              <Button
                size="sm"
                variant="outline"
                disabled={actioning !== null}
                onClick={() =>
                  act(() => approveSlackPairing(token, req.code), `approve-${req.code}`)
                }
                className="h-7 rounded-full px-2.5 text-[12px]"
              >
                <Check className="mr-1 h-3.5 w-3.5" />
                {t("settings.channels.slack.approve")}
              </Button>
              <Button
                size="sm"
                variant="ghost"
                disabled={actioning !== null}
                onClick={() =>
                  act(() => denySlackPairing(token, req.code), `deny-${req.code}`)
                }
                className="h-7 rounded-full px-2.5 text-[12px] text-muted-foreground"
              >
                <X className="mr-1 h-3.5 w-3.5" />
                {t("settings.channels.slack.deny")}
              </Button>
            </div>
          ))}
        </div>
      ) : null}

      {pairing && pairing.approved.length > 0 ? (
        <div className="space-y-1.5">
          <div className="text-[11px] font-semibold uppercase tracking-wide text-muted-foreground">
            {t("settings.channels.slack.approvedSenders")}
          </div>
          {pairing.approved.map((senderId) => (
            <div
              key={senderId}
              className="flex flex-wrap items-center gap-2 rounded-xl border border-border/40 bg-muted/30 px-3 py-2 text-[13px]"
            >
              <UserCheck className="h-3.5 w-3.5 shrink-0 text-emerald-500" />
              <span className="flex-1 font-mono text-[12px]">{senderId}</span>
              <Button
                size="sm"
                variant="ghost"
                disabled={actioning !== null}
                onClick={() =>
                  act(() => revokeSlackPairing(token, senderId), `revoke-${senderId}`)
                }
                className="h-7 rounded-full px-2.5 text-[12px] text-muted-foreground"
              >
                {t("settings.channels.slack.revoke")}
              </Button>
            </div>
          ))}
        </div>
      ) : null}

      {pairing &&
      pairing.pending.length === 0 &&
      pairing.approved.length === 0 ? (
        <p className="text-[12px] text-muted-foreground">
          {t("settings.channels.slack.noPairings")}
        </p>
      ) : null}
    </div>
  );
}

// ---------- guided setup view ----------------------------------------------

function GuidedSetup({
  channel,
  token,
  onChanged,
}: {
  channel: ChannelInfo;
  token: string;
  onChanged: () => void;
}) {
  const { t } = useTranslation();
  const { client } = useClient();

  const [copied, setCopied] = useState(false);
  const [botToken, setBotToken] = useState("");
  const [appToken, setAppToken] = useState("");
  const [validated, setValidated] = useState<SlackTestResult | null>(null);
  const [validating, setValidating] = useState(false);
  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState(false);

  const copyManifest = async () => {
    try {
      const { manifest } = await getSlackManifest(token);
      await navigator.clipboard.writeText(JSON.stringify(manifest, null, 2));
      setCopied(true);
      setTimeout(() => setCopied(false), 2500);
    } catch {
      // clipboard/fetch failure — leave the button as-is; the user can retry
    }
  };

  const validate = async () => {
    const bot = botToken.trim();
    const app = appToken.trim();
    if (!bot || !app) return;
    setValidating(true);
    setValidated(null);
    try {
      setValidated(await testSlackTokens(token, bot, app));
    } catch {
      setValidated({
        ok: false,
        bot_user: null,
        team: null,
        bot_error: t("settings.channels.saveError"),
        app_error: null,
      });
    } finally {
      setValidating(false);
    }
  };

  // Save: storeSecret → write ${secret:...} refs → enable → notify parent.
  // The raw tokens NEVER go to setConfigValue.
  const save = async () => {
    if (!validated?.ok) return;
    setSaving(true);
    setSaveError(false);
    try {
      await client.storeSecret({
        name: "SLACK_BOT_TOKEN",
        value: botToken.trim(),
        service: "channel:slack",
        scope: ["channel:slack"],
      });
      await client.storeSecret({
        name: "SLACK_APP_TOKEN",
        value: appToken.trim(),
        service: "channel:slack",
        scope: ["channel:slack"],
      });
      await setConfigValue(token, "channels.slack.bot_token", "${secret:SLACK_BOT_TOKEN}");
      await setConfigValue(token, "channels.slack.app_token", "${secret:SLACK_APP_TOKEN}");
      await setConfigValue(token, "channels.slack.enabled", true);
      await startChannel(token, "slack");
      onChanged();
    } catch {
      setSaveError(true);
    } finally {
      setSaving(false);
    }
  };

  // Bot is already configured → show pairing panel only
  if (channel.enabled) {
    return <PairingPanel token={token} />;
  }

  // Not yet enabled → show setup steps
  return (
    <div className="mt-3 space-y-4">
      {/* Step 1: create the app from the generated manifest */}
      <div className="space-y-1.5">
        <div className="text-[12px] font-semibold text-foreground/80">
          {t("settings.channels.slack.step1Title")}
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <Button
            size="sm"
            variant="outline"
            onClick={() => void copyManifest()}
            className="rounded-full"
          >
            {copied ? (
              <Check className="mr-1 h-3.5 w-3.5" />
            ) : (
              <Copy className="mr-1 h-3.5 w-3.5" />
            )}
            {copied
              ? t("settings.channels.slack.manifestCopied")
              : t("settings.channels.slack.copyManifest")}
          </Button>
          <a
            href="https://api.slack.com/apps?new_app=1"
            target="_blank"
            rel="noreferrer"
            className="inline-flex items-center gap-1.5 rounded-full border border-border/60 bg-background px-3 py-1 text-[12px] font-medium text-foreground hover:bg-muted/50 transition-colors"
          >
            <ExternalLink className="h-3.5 w-3.5" />
            {t("settings.channels.slack.createAppLink")}
          </a>
        </div>
        <p className="text-[12px] text-muted-foreground">
          {t("settings.channels.slack.step1Hint")}
        </p>
      </div>

      {/* Step 2: app-level token */}
      <div className="space-y-1.5">
        <div className="text-[12px] font-semibold text-foreground/80">
          {t("settings.channels.slack.step2Title")}
        </div>
        <p className="text-[12px] text-muted-foreground">
          {t("settings.channels.slack.step2Hint")}
        </p>
        <Input
          type="password"
          value={appToken}
          onChange={(e) => {
            setAppToken(e.target.value);
            setValidated(null);
          }}
          placeholder="xapp-…"
          className="w-[280px]"
          autoComplete="off"
        />
      </div>

      {/* Step 3: bot token + validate */}
      <div className="space-y-1.5">
        <div className="text-[12px] font-semibold text-foreground/80">
          {t("settings.channels.slack.step3Title")}
        </div>
        <p className="text-[12px] text-muted-foreground">
          {t("settings.channels.slack.step3Hint")}
        </p>
        <div className="flex flex-wrap items-center gap-2">
          <Input
            type="password"
            value={botToken}
            onChange={(e) => {
              setBotToken(e.target.value);
              setValidated(null);
            }}
            placeholder="xoxb-…"
            className="w-[280px]"
            autoComplete="off"
          />
          <Button
            size="sm"
            variant="outline"
            disabled={validating || !botToken.trim() || !appToken.trim()}
            onClick={() => void validate()}
            className="rounded-full"
          >
            {validating
              ? t("settings.channels.slack.validating")
              : t("settings.channels.slack.validate")}
          </Button>
        </div>

        {validated?.ok ? (
          <p className="text-[12px] text-emerald-600 dark:text-emerald-400">
            {t("settings.channels.slack.connectedAs", {
              user: validated.bot_user ?? "",
              team: validated.team ?? "",
            })}
          </p>
        ) : null}
        {validated && !validated.ok ? (
          <p className="text-[12px] text-muted-foreground">
            {validated.bot_error
              ? t("settings.channels.slack.botTokenError", { error: validated.bot_error })
              : null}
            {validated.bot_error && validated.app_error ? " · " : null}
            {validated.app_error
              ? t("settings.channels.slack.appTokenError", { error: validated.app_error })
              : null}
          </p>
        ) : null}
      </div>

      {/* Step 4: save (only available once validated) */}
      <Button
        size="sm"
        variant="outline"
        disabled={saving || !validated?.ok}
        onClick={() => void save()}
        className="rounded-full"
      >
        {saving
          ? t("settings.channels.slack.saving")
          : t("settings.channels.slack.saveAndEnable")}
      </Button>
      {saveError ? (
        <p className="text-[12px] text-muted-foreground">
          {t("settings.channels.saveError")}
        </p>
      ) : null}
    </div>
  );
}

// ---------- top-level export -----------------------------------------------

/** Guided Slack setup with a manual escape hatch.
 *
 * `children` is the generic schema-driven field form rendered by
 * ChannelsSettings — manual mode reuses it verbatim so both modes write the
 * exact same config keys. */
export function SlackGuided({
  channel,
  token,
  onChanged,
  children,
}: {
  channel: ChannelInfo;
  token: string;
  onChanged: () => void;
  children: ReactNode;
}) {
  const [mode, setMode] = useState<"guided" | "manual">("guided");

  return (
    <div>
      <div className="mt-2">
        <ModeSwitch mode={mode} onChange={setMode} />
      </div>

      {mode === "guided" ? (
        <GuidedSetup channel={channel} token={token} onChanged={onChanged} />
      ) : (
        children
      )}
    </div>
  );
}

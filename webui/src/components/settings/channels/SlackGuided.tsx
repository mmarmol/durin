import { useCallback, useEffect, useRef, useState, type ReactNode } from "react";
import { ExternalLink, Check, Copy, Hash, Loader2, Lock, X, UserCheck } from "lucide-react";
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
  getSlackChannels,
  joinSlackChannel,
  type ChannelInfo,
  type SlackChannelEntry,
  type SlackPairing,
} from "@/lib/api";

// ---------- shared bits ------------------------------------------------------

/** Map structured/Slack error codes to readable messages; fall back to the code. */
function useTokenErrorLabel() {
  const { t } = useTranslation();
  return (code: string | null): string | null => {
    if (!code) return null;
    const key = `settings.channels.slack.errors.${code}`;
    return t(key, code);
  };
}

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

/** Numbered step header whose badge flips to a check once the step is done. */
function StepHeader({ number, done, title }: { number: number; done: boolean; title: string }) {
  return (
    <div className="flex items-center gap-2">
      <span
        className={
          "grid h-5 w-5 shrink-0 place-items-center rounded-full text-[11px] font-semibold " +
          (done
            ? "bg-emerald-500/15 text-emerald-600 dark:text-emerald-400"
            : "bg-muted text-muted-foreground")
        }
      >
        {done ? <Check className="h-3 w-3" /> : number}
      </span>
      <span className="text-[12px] font-semibold text-foreground/80">{title}</span>
    </div>
  );
}

// ---------- per-token validated input ---------------------------------------

type TokenCheck =
  | { state: "idle" }
  | { state: "checking" }
  | { state: "ok"; user?: string | null; team?: string | null }
  | { state: "error"; code: string };

/** Password input that auto-validates the pasted token against Slack. */
function TokenField({
  value,
  onValue,
  check,
  placeholder,
}: {
  value: string;
  onValue: (v: string) => void;
  check: TokenCheck;
  placeholder: string;
}) {
  const errorLabel = useTokenErrorLabel();
  return (
    <div className="space-y-1">
      <div className="flex flex-wrap items-center gap-2">
        <Input
          type="password"
          value={value}
          onChange={(e) => onValue(e.target.value)}
          placeholder={placeholder}
          className="w-[280px]"
          autoComplete="off"
        />
        {check.state === "checking" ? (
          <Loader2 className="h-3.5 w-3.5 animate-spin text-muted-foreground" />
        ) : null}
        {check.state === "ok" ? (
          <Check className="h-3.5 w-3.5 text-emerald-500" />
        ) : null}
      </div>
      {check.state === "error" ? (
        <p className="text-[12px] text-amber-600 dark:text-amber-400">
          {errorLabel(check.code)}
        </p>
      ) : null}
    </div>
  );
}

// ---------- pairing panel ----------------------------------------------------

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
    <div className="mt-4 space-y-1.5">
      <div className="text-[11px] font-semibold uppercase tracking-wide text-muted-foreground">
        {t("settings.channels.slack.pairingTitle")}
      </div>
      <p className="text-[12px] text-muted-foreground">
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

// ---------- channels panel ---------------------------------------------------

function ChannelsPanel({ token }: { token: string }) {
  const { t } = useTranslation();
  const errorLabel = useTokenErrorLabel();
  const [channels, setChannels] = useState<SlackChannelEntry[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [joining, setJoining] = useState<string | null>(null);

  const fetchChannels = useCallback(async () => {
    try {
      const data = await getSlackChannels(token);
      if (data.ok) {
        setChannels(data.channels);
        setError(null);
      } else {
        setError(data.error ?? "error");
      }
    } catch {
      setError("error");
    }
  }, [token]);

  useEffect(() => {
    void fetchChannels();
  }, [fetchChannels]);

  const join = async (channelId: string) => {
    setJoining(channelId);
    try {
      const res = await joinSlackChannel(token, channelId);
      if (!res.ok) setError(res.error ?? "error");
      await fetchChannels();
    } finally {
      setJoining(null);
    }
  };

  return (
    <div className="mt-4 space-y-1.5">
      <div className="text-[11px] font-semibold uppercase tracking-wide text-muted-foreground">
        {t("settings.channels.slack.channelsTitle")}
      </div>
      <p className="text-[12px] text-muted-foreground">
        {t("settings.channels.slack.channelsHint")}
      </p>
      {error ? (
        <p className="text-[12px] text-amber-600 dark:text-amber-400">
          {errorLabel(error)}
        </p>
      ) : null}
      {channels?.map((ch) => (
        <div
          key={ch.id}
          className="flex flex-wrap items-center gap-2 rounded-xl border border-border/40 bg-muted/30 px-3 py-2 text-[13px]"
        >
          {ch.is_private ? (
            <Lock className="h-3.5 w-3.5 shrink-0 text-muted-foreground" />
          ) : (
            <Hash className="h-3.5 w-3.5 shrink-0 text-muted-foreground" />
          )}
          <span className="flex-1 font-mono text-[12px]">{ch.name}</span>
          {ch.is_member ? (
            <span className="inline-flex items-center gap-1 text-[11px] text-emerald-600 dark:text-emerald-400">
              <Check className="h-3.5 w-3.5" />
              {t("settings.channels.slack.joined")}
            </span>
          ) : ch.is_private ? (
            <span className="text-[11px] text-muted-foreground">
              {t("settings.channels.slack.privateInviteHint")}
            </span>
          ) : (
            <Button
              size="sm"
              variant="outline"
              disabled={joining !== null}
              onClick={() => void join(ch.id)}
              className="h-7 rounded-full px-2.5 text-[12px]"
            >
              {joining === ch.id
                ? t("settings.channels.slack.joining")
                : t("settings.channels.slack.join")}
            </Button>
          )}
        </div>
      ))}
      {channels && channels.length === 0 ? (
        <p className="text-[12px] text-muted-foreground">
          {t("settings.channels.slack.noChannels")}
        </p>
      ) : null}
    </div>
  );
}

// ---------- connected panel --------------------------------------------------

/** Shown once tokens are stored: proves the saved secrets work (live
 *  auth check against Slack), then pairing + channel management. */
function ConnectedPanel({
  token,
  onReplaceTokens,
}: {
  token: string;
  onReplaceTokens: () => void;
}) {
  const { t } = useTranslation();
  const errorLabel = useTokenErrorLabel();
  const [status, setStatus] = useState<
    | { state: "checking" }
    | { state: "ok"; user: string | null; team: string | null }
    | { state: "error"; code: string }
  >({ state: "checking" });

  useEffect(() => {
    let cancelled = false;
    // Empty tokens → the backend health-checks the CONFIGURED secrets.
    testSlackTokens(token, "", "")
      .then((res) => {
        if (cancelled) return;
        if (res.ok) {
          setStatus({ state: "ok", user: res.bot_user, team: res.team });
        } else {
          setStatus({ state: "error", code: res.bot_error ?? res.app_error ?? "error" });
        }
      })
      .catch(() => {
        if (!cancelled) setStatus({ state: "error", code: "error" });
      });
    return () => {
      cancelled = true;
    };
  }, [token]);

  return (
    <div className="mt-3">
      <div className="flex flex-wrap items-center gap-2 rounded-xl border border-border/40 bg-muted/30 px-3 py-2 text-[13px]">
        {status.state === "checking" ? (
          <>
            <Loader2 className="h-3.5 w-3.5 animate-spin text-muted-foreground" />
            <span className="flex-1 text-muted-foreground">
              {t("settings.channels.slack.statusChecking")}
            </span>
          </>
        ) : status.state === "ok" ? (
          <>
            <span className="h-1.5 w-1.5 rounded-full bg-emerald-500" aria-hidden />
            <span className="flex-1">
              {t("settings.channels.slack.connectedAs", {
                user: status.user ?? "",
                team: status.team ?? "",
              })}
            </span>
          </>
        ) : (
          <>
            <span className="h-1.5 w-1.5 rounded-full bg-amber-500" aria-hidden />
            <span className="flex-1 text-amber-600 dark:text-amber-400">
              {errorLabel(status.code)}
            </span>
          </>
        )}
        <Button
          size="sm"
          variant="ghost"
          onClick={onReplaceTokens}
          className="h-7 rounded-full px-2.5 text-[12px] text-muted-foreground"
        >
          {t("settings.channels.slack.replaceTokens")}
        </Button>
      </div>
      <p className="mt-1 text-[11px] text-muted-foreground">
        {t("settings.channels.slack.secretsStored")}
      </p>

      <PairingPanel token={token} />
      <ChannelsPanel token={token} />
    </div>
  );
}

// ---------- guided setup wizard ----------------------------------------------

const AUTO_VALIDATE_DEBOUNCE_MS = 700;

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
  const [manifestTouched, setManifestTouched] = useState(false);
  const [botToken, setBotToken] = useState("");
  const [appToken, setAppToken] = useState("");
  const [botCheck, setBotCheck] = useState<TokenCheck>({ state: "idle" });
  const [appCheck, setAppCheck] = useState<TokenCheck>({ state: "idle" });
  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState(false);
  const timers = useRef<{ bot?: number; app?: number }>({});

  const copyManifest = async () => {
    try {
      const { manifest } = await getSlackManifest(token);
      await navigator.clipboard.writeText(JSON.stringify(manifest, null, 2));
      setCopied(true);
      setManifestTouched(true);
      setTimeout(() => setCopied(false), 2500);
    } catch {
      // clipboard/fetch failure — leave the button as-is; the user can retry
    }
  };

  // Each token validates on its own as soon as it's pasted, so errors land on
  // the field that caused them instead of a combined validate-both mystery.
  const scheduleCheck = (kind: "bot" | "app", value: string) => {
    const setCheck = kind === "bot" ? setBotCheck : setAppCheck;
    const prefix = kind === "bot" ? "xoxb-" : "xapp-";
    window.clearTimeout(timers.current[kind]);
    const v = value.trim();
    if (!v) {
      setCheck({ state: "idle" });
      return;
    }
    if (!v.startsWith(prefix)) {
      setCheck({
        state: "error",
        code: kind === "bot" ? "expected_bot_token" : "expected_app_token",
      });
      return;
    }
    setCheck({ state: "checking" });
    timers.current[kind] = window.setTimeout(async () => {
      try {
        const res = await testSlackTokens(
          token,
          kind === "bot" ? v : "",
          kind === "app" ? v : "",
        );
        const err = kind === "bot" ? res.bot_error : res.app_error;
        if (err) {
          setCheck({ state: "error", code: err });
        } else {
          setCheck({ state: "ok", user: res.bot_user, team: res.team });
        }
      } catch {
        setCheck({ state: "error", code: "error" });
      }
    }, AUTO_VALIDATE_DEBOUNCE_MS);
  };

  const botOk = botCheck.state === "ok";
  const appOk = appCheck.state === "ok";

  // Save: storeSecret → write ${secret:...} refs → enable → notify parent.
  // The raw tokens NEVER go to setConfigValue.
  const save = async () => {
    if (!botOk || !appOk) return;
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

  return (
    <div className="mt-3 space-y-4">
      {channel.enabled ? (
        <p className="text-[12px] text-amber-600 dark:text-amber-400">
          {t("settings.channels.slack.enabledWithoutTokens")}
        </p>
      ) : null}

      {/* Step 1: create the app from the generated manifest */}
      <div className="space-y-1.5">
        <StepHeader
          number={1}
          done={manifestTouched}
          title={t("settings.channels.slack.step1Title")}
        />
        <div className="flex flex-wrap items-center gap-2 pl-7">
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
            onClick={() => setManifestTouched(true)}
          >
            <ExternalLink className="h-3.5 w-3.5" />
            {t("settings.channels.slack.createAppLink")}
          </a>
        </div>
        <p className="pl-7 text-[12px] text-muted-foreground">
          {t("settings.channels.slack.step1Hint")}
        </p>
      </div>

      {/* Step 2: app-level token */}
      <div className="space-y-1.5">
        <StepHeader number={2} done={appOk} title={t("settings.channels.slack.step2Title")} />
        <p className="pl-7 text-[12px] text-muted-foreground">
          {t("settings.channels.slack.step2Hint")}
        </p>
        <div className="pl-7">
          <TokenField
            value={appToken}
            onValue={(v) => {
              setAppToken(v);
              scheduleCheck("app", v);
            }}
            check={appCheck}
            placeholder="xapp-…"
          />
        </div>
      </div>

      {/* Step 3: bot token */}
      <div className="space-y-1.5">
        <StepHeader number={3} done={botOk} title={t("settings.channels.slack.step3Title")} />
        <p className="pl-7 text-[12px] text-muted-foreground">
          {t("settings.channels.slack.step3Hint")}
        </p>
        <div className="pl-7">
          <TokenField
            value={botToken}
            onValue={(v) => {
              setBotToken(v);
              scheduleCheck("bot", v);
            }}
            check={botCheck}
            placeholder="xoxb-…"
          />
          {botCheck.state === "ok" ? (
            <p className="mt-1 text-[12px] text-emerald-600 dark:text-emerald-400">
              {t("settings.channels.slack.connectedAs", {
                user: botCheck.user ?? "",
                team: botCheck.team ?? "",
              })}
            </p>
          ) : null}
        </div>
      </div>

      {/* Step 4: save — stores both tokens as durin secrets, then enables */}
      <div className="space-y-1.5">
        <StepHeader
          number={4}
          done={false}
          title={t("settings.channels.slack.step4Title")}
        />
        <div className="flex flex-wrap items-center gap-2 pl-7">
          <Button
            size="sm"
            variant="outline"
            disabled={saving || !botOk || !appOk}
            onClick={() => void save()}
            className="rounded-full"
          >
            {saving
              ? t("settings.channels.slack.saving")
              : t("settings.channels.slack.saveAndEnable")}
          </Button>
          <span className="text-[11px] text-muted-foreground">
            {t("settings.channels.slack.step4Hint")}
          </span>
        </div>
        {saveError ? (
          <p className="pl-7 text-[12px] text-amber-600 dark:text-amber-400">
            {t("settings.channels.saveError")}
          </p>
        ) : null}
      </div>
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
  channelValues,
  token,
  onChanged,
  children,
}: {
  channel: ChannelInfo;
  channelValues: Record<string, unknown>;
  token: string;
  onChanged: () => void;
  children: ReactNode;
}) {
  const [mode, setMode] = useState<"guided" | "manual">("guided");
  const [replacingTokens, setReplacingTokens] = useState(false);

  const tokensConfigured =
    Boolean(channelValues["bot_token"]) && Boolean(channelValues["app_token"]);

  return (
    <div>
      <div className="mt-2">
        <ModeSwitch mode={mode} onChange={setMode} />
      </div>

      {mode === "manual" ? (
        children
      ) : tokensConfigured && !replacingTokens ? (
        <ConnectedPanel token={token} onReplaceTokens={() => setReplacingTokens(true)} />
      ) : (
        <GuidedSetup
          channel={channel}
          token={token}
          onChanged={() => {
            setReplacingTokens(false);
            onChanged();
          }}
        />
      )}
    </div>
  );
}

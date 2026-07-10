import { useCallback, useEffect, useRef, useState, type ReactNode } from "react";
import { AlertTriangle, Loader2, QrCode, RefreshCw } from "lucide-react";
import { useTranslation } from "react-i18next";
import { QRCodeSVG } from "qrcode.react";

import { Button } from "@/components/ui/button";
import {
  setConfigValue,
  startChannel,
  startWhatsAppLogin,
  pollWhatsAppLogin,
  getChannelsRuntime,
  type ChannelInfo,
  type WhatsAppLoginState,
} from "@/lib/api";

const POLL_INTERVAL_MS = 2000;

// ---------- mode switch ------------------------------------------------------

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
        {t("settings.channels.whatsapp.modeGuided")}
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
        {t("settings.channels.whatsapp.modeManual")}
      </button>
    </div>
  );
}

// ---------- ban-risk warning -------------------------------------------------

/** Always visible in guided mode — WhatsApp's unofficial protocol carries a
 *  real ban risk, so the caution has to be seen before the user scans. */
function BanRiskWarning() {
  const { t } = useTranslation();
  return (
    <div className="flex items-start gap-2 rounded-lg border border-amber-500/30 bg-amber-500/10 px-3 py-2 text-[12px] text-amber-700 dark:text-amber-400">
      <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0" aria-hidden />
      <span>{t("settings.channels.whatsapp.banRiskWarning")}</span>
    </div>
  );
}

// ---------- connected panel --------------------------------------------------

/** Shown once WhatsApp is paired and the channel enabled: no live
 *  credential to re-verify (the session lives in the bridge's local auth
 *  dir), so this only reports transport status and offers a re-link. */
function ConnectedPanel({
  token,
  onRelink,
}: {
  token: string;
  onRelink: () => void;
}) {
  const { t } = useTranslation();
  const [running, setRunning] = useState<boolean | null>(null);
  const [starting, setStarting] = useState(false);
  const [startError, setStartError] = useState<string | null>(null);

  const fetchRuntime = useCallback(async () => {
    try {
      const data = await getChannelsRuntime(token);
      setRunning(Boolean(data.running["whatsapp"]));
    } catch {
      // leave unknown; retried on the next tick
    }
  }, [token]);

  useEffect(() => {
    void fetchRuntime();
    const id = setInterval(() => void fetchRuntime(), 10000);
    return () => clearInterval(id);
  }, [fetchRuntime]);

  const startNow = async () => {
    setStarting(true);
    setStartError(null);
    try {
      const res = await startChannel(token, "whatsapp");
      if (!res.ok) setStartError(res.error ?? "error");
      await fetchRuntime();
    } finally {
      setStarting(false);
    }
  };

  return (
    <div className="mt-3">
      <div className="flex flex-wrap items-center gap-2 rounded-xl border border-border/40 bg-muted/30 px-3 py-2 text-[13px]">
        <span className="h-1.5 w-1.5 rounded-full bg-emerald-500" aria-hidden />
        <span className="flex-1">{t("settings.channels.whatsapp.paired")}</span>
        <Button
          size="sm"
          variant="ghost"
          onClick={onRelink}
          className="h-7 rounded-full px-2.5 text-[12px] text-muted-foreground"
        >
          {t("settings.channels.whatsapp.relink")}
        </Button>
      </div>
      {/* Transport status — separate from pairing state on purpose */}
      <div className="mt-1.5 flex flex-wrap items-center gap-2 rounded-xl border border-border/40 bg-muted/30 px-3 py-2 text-[13px]">
        {running === null ? (
          <>
            <Loader2 className="h-3.5 w-3.5 animate-spin text-muted-foreground" />
            <span className="flex-1 text-muted-foreground">
              {t("settings.channels.whatsapp.runtimeChecking")}
            </span>
          </>
        ) : running ? (
          <>
            <span className="h-1.5 w-1.5 rounded-full bg-emerald-500" aria-hidden />
            <span className="flex-1">{t("settings.channels.whatsapp.runtimeRunning")}</span>
          </>
        ) : (
          <>
            <span className="h-1.5 w-1.5 rounded-full bg-amber-500" aria-hidden />
            <span className="flex-1 text-amber-600 dark:text-amber-400">
              {t("settings.channels.whatsapp.runtimeStopped")}
            </span>
            <Button
              size="sm"
              variant="outline"
              disabled={starting}
              onClick={() => void startNow()}
              className="h-7 rounded-full px-2.5 text-[12px]"
            >
              {starting
                ? t("settings.channels.whatsapp.runtimeStarting")
                : t("settings.channels.whatsapp.runtimeStart")}
            </Button>
          </>
        )}
      </div>
      {startError ? (
        <p className="mt-1 text-[12px] text-amber-600 dark:text-amber-400">{startError}</p>
      ) : null}
      <BanRiskWarning />
    </div>
  );
}

// ---------- guided pairing flow ----------------------------------------------

function GuidedSetup({
  token,
  onChanged,
  forceOnConnect = false,
}: {
  token: string;
  onChanged: () => void;
  /** Re-pair session: force the bridge to drop the existing linked session so
   *  a fresh QR is shown (otherwise it reports "already_paired" instantly and
   *  the user can never link a different number). Retries within the session
   *  keep this intent. */
  forceOnConnect?: boolean;
}) {
  const { t } = useTranslation();
  const [login, setLogin] = useState<WhatsAppLoginState | null>(null);
  const [enabling, setEnabling] = useState(false);
  const [enableError, setEnableError] = useState<string | null>(null);
  const [requestError, setRequestError] = useState<string | null>(null);
  const pollTimer = useRef<number | null>(null);

  const cancelPoll = () => {
    if (pollTimer.current) {
      window.clearTimeout(pollTimer.current);
      pollTimer.current = null;
    }
  };

  useEffect(() => cancelPoll, []);

  // Enable + start the channel once the phone confirms the pairing (or an
  // already-linked session was found), then hand control back to the parent
  // so it swaps in the connected panel.
  const finishPairing = useCallback(async () => {
    setEnabling(true);
    setEnableError(null);
    try {
      await setConfigValue(token, "channels.whatsapp.enabled", true);
      await startChannel(token, "whatsapp");
      onChanged();
    } catch (e) {
      setEnableError((e as Error).message);
    } finally {
      setEnabling(false);
    }
  }, [token, onChanged]);

  const pollUntilDone = useCallback(() => {
    const tick = async () => {
      try {
        const state = await pollWhatsAppLogin(token);
        setLogin(state);
        if (state.status === "connected" || state.status === "already_paired") {
          void finishPairing();
          return;
        }
        if (state.status === "timeout" || state.status === "error") {
          return;
        }
        pollTimer.current = window.setTimeout(tick, POLL_INTERVAL_MS);
      } catch (e) {
        setRequestError((e as Error).message);
      }
    };
    pollTimer.current = window.setTimeout(tick, POLL_INTERVAL_MS);
  }, [token, finishPairing]);

  const connect = async (force: boolean) => {
    setRequestError(null);
    cancelPoll();
    try {
      const state = await startWhatsAppLogin(token, force);
      setLogin(state);
      if (state.status === "connected" || state.status === "already_paired") {
        void finishPairing();
        return;
      }
      if (state.status === "starting" || state.status === "waiting_scan") {
        pollUntilDone();
      }
    } catch (e) {
      setRequestError((e as Error).message);
    }
  };

  return (
    <div className="mt-3 space-y-3">
      <BanRiskWarning />

      {requestError ? (
        <p className="text-[12px] text-amber-600 dark:text-amber-400">{requestError}</p>
      ) : null}

      {!login || login.status === "idle" ? (
        <Button size="sm" variant="outline" onClick={() => void connect(forceOnConnect)} className="rounded-full">
          <QrCode className="mr-1.5 h-3.5 w-3.5" />
          {t("settings.channels.whatsapp.connect")}
        </Button>
      ) : null}

      {login?.status === "starting" ? (
        <div className="flex items-center gap-2 text-[13px] text-muted-foreground">
          <Loader2 className="h-3.5 w-3.5 animate-spin" />
          {t("settings.channels.whatsapp.starting")}
        </div>
      ) : null}

      {login?.status === "waiting_scan" && login.qr ? (
        <div className="space-y-2 rounded-[8px] border border-border/60 bg-muted/40 p-3">
          <div className="flex justify-center rounded-md bg-white p-3">
            <QRCodeSVG value={login.qr} size={200} />
          </div>
          <p className="text-center text-[12px] text-muted-foreground">
            {t("settings.channels.whatsapp.scanInstruction")}
          </p>
        </div>
      ) : null}

      {(enabling || (login?.status === "connected" || login?.status === "already_paired")) ? (
        <div className="flex items-center gap-2 text-[13px] text-muted-foreground">
          <Loader2 className="h-3.5 w-3.5 animate-spin" />
          {t("settings.channels.whatsapp.finishing")}
        </div>
      ) : null}
      {enableError ? <p className="text-[12px] text-amber-600 dark:text-amber-400">{enableError}</p> : null}

      {login?.status === "timeout" ? (
        <div className="space-y-1.5">
          <p className="text-[12px] text-muted-foreground">{t("settings.channels.whatsapp.timeout")}</p>
          <Button size="sm" variant="outline" onClick={() => void connect(forceOnConnect)} className="rounded-full">
            <RefreshCw className="mr-1.5 h-3.5 w-3.5" />
            {t("settings.channels.whatsapp.retry")}
          </Button>
        </div>
      ) : null}

      {login?.status === "error" ? (
        <div className="space-y-1.5">
          <p className="text-[12px] text-amber-600 dark:text-amber-400">
            {login.error ?? t("settings.channels.saveError")}
          </p>
          <Button size="sm" variant="outline" onClick={() => void connect(forceOnConnect)} className="rounded-full">
            <RefreshCw className="mr-1.5 h-3.5 w-3.5" />
            {t("settings.channels.whatsapp.retry")}
          </Button>
        </div>
      ) : null}
    </div>
  );
}

// ---------- top-level export -----------------------------------------------

/** Guided WhatsApp setup with a manual escape hatch.
 *
 * `children` is the generic schema-driven field form rendered by
 * ChannelsSettings — manual mode reuses it verbatim so both modes write the
 * exact same config keys. Unlike Slack/Discord/Telegram there is no bearer
 * token to paste: pairing happens by scanning a QR code, so the guided flow
 * drives the login/start + login/poll state machine instead of a token
 * field. */
export function WhatsAppGuided({
  channel,
  channelValues: _channelValues,
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
  const [relinking, setRelinking] = useState(false);

  const paired = channel.enabled && !relinking;

  return (
    <div>
      <div className="mt-2">
        <ModeSwitch mode={mode} onChange={setMode} />
      </div>

      {mode === "manual" ? (
        children
      ) : paired ? (
        <ConnectedPanel token={token} onRelink={() => setRelinking(true)} />
      ) : (
        <GuidedSetup
          token={token}
          forceOnConnect={relinking}
          onChanged={() => {
            setRelinking(false);
            onChanged();
          }}
        />
      )}
    </div>
  );
}

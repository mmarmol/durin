import { useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";

import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";
import {
  disconnectCodex,
  fetchCodexStatus,
  pollCodexDeviceAuth,
  startCodexDeviceAuth,
  startCodexLoopbackAuth,
  type CodexStatus,
} from "@/lib/api";

type Props = {
  token: string;
  base?: string;
  /** When true, omit the outer card chrome + header (the provider row supplies them). */
  embedded?: boolean;
  /** Called after a successful connect/disconnect so the parent can refresh settings. */
  onChanged?: () => void;
};

export function CodexOAuthCard({ token, base = "", embedded = false, onChanged }: Props) {
  const { t } = useTranslation();
  const [status, setStatus] = useState<CodexStatus | null>(null);
  const [challenge, setChallenge] = useState<{
    user_code: string;
    verification_uri: string;
  } | null>(null);
  const [loopbackUrl, setLoopbackUrl] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [confirmDisconnect, setConfirmDisconnect] = useState(false);
  const pollTimer = useRef<number | null>(null);

  useEffect(() => {
    fetchCodexStatus(token, base).then(setStatus).catch(() => setStatus(null));
    return () => {
      if (pollTimer.current) window.clearTimeout(pollTimer.current);
    };
  }, [token, base]);

  const cancelPoll = () => {
    if (pollTimer.current) {
      window.clearTimeout(pollTimer.current);
      pollTimer.current = null;
    }
  };

  const onConnected = (s: CodexStatus) => {
    setChallenge(null);
    setLoopbackUrl(null);
    setBusy(false);
    setStatus(s);
    onChanged?.();
  };

  // Loopback (local install): the gateway captures the callback on localhost:1455.
  const pollStatusUntilConnected = () => {
    const tick = async () => {
      try {
        const s = await fetchCodexStatus(token, base);
        if (s.connected) {
          onConnected(s);
          return;
        }
        pollTimer.current = window.setTimeout(tick, 2000);
      } catch (e) {
        setError((e as Error).message);
        setBusy(false);
      }
    };
    pollTimer.current = window.setTimeout(tick, 2000);
  };

  const connectLoopback = async () => {
    try {
      const { authorize_url } = await startCodexLoopbackAuth(token, base);
      setLoopbackUrl(authorize_url);
      window.open(authorize_url, "_blank", "noopener");
      pollStatusUntilConnected();
    } catch (e) {
      setError((e as Error).message);
      setBusy(false);
    }
  };

  // Device-code (remote install): user types a code; requires the device-auth
  // toggle in ChatGPT security settings.
  const connectDeviceCode = async () => {
    try {
      const ch = await startCodexDeviceAuth(token, base);
      setChallenge({ user_code: ch.user_code, verification_uri: ch.verification_uri });
      const intervalMs = Math.max(3, ch.interval) * 1000;
      const tick = async () => {
        try {
          const res = await pollCodexDeviceAuth(token, ch.device_auth_id, ch.user_code, base);
          if (res.status === "ok") {
            onConnected({
              connected: true,
              email: res.email,
              plan: res.plan,
              source: res.source,
            });
            return;
          }
          if (res.status === "error") {
            setError(res.error ?? t("settings.oauth.codex.authError"));
            setChallenge(null);
            setBusy(false);
            return;
          }
          pollTimer.current = window.setTimeout(tick, intervalMs);
        } catch (e) {
          setError((e as Error).message);
          setBusy(false);
        }
      };
      pollTimer.current = window.setTimeout(tick, intervalMs);
    } catch (e) {
      setError((e as Error).message);
      setBusy(false);
    }
  };

  const connect = async () => {
    setError(null);
    setBusy(true);
    if (status?.can_loopback) {
      await connectLoopback();
    } else {
      await connectDeviceCode();
    }
  };

  // Escape hatch: loopback detection can be fooled (e.g. an SSH port-forward
  // makes a remote gateway look local), leaving the loopback callback
  // unreachable. Device-code works everywhere, so always offer it as a fallback.
  const switchToDeviceCode = async () => {
    cancelPoll();
    setLoopbackUrl(null);
    setError(null);
    setBusy(true);
    await connectDeviceCode();
  };

  const doDisconnect = async () => {
    setConfirmDisconnect(false);
    setBusy(true);
    try {
      setStatus(await disconnectCodex(token, base));
      onChanged?.();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div
      className={cn(
        "space-y-3",
        embedded ? "" : "rounded-[10px] border border-border/45 p-4",
      )}
    >
      {embedded ? null : (
        <div className="flex items-center justify-between">
          <span className="text-[15px] font-semibold">OpenAI Codex (ChatGPT)</span>
          <span
            className={cn(
              "rounded-full px-2.5 py-1 text-[12px] font-medium",
              status?.connected
                ? "bg-emerald-500/10 text-emerald-700 dark:text-emerald-300"
                : "bg-muted text-muted-foreground",
            )}
          >
            {status?.connected
              ? `${t("settings.oauth.codex.connected")}${status.email ? ` · ${status.email}` : ""}`
              : t("settings.oauth.codex.notConnected")}
          </span>
        </div>
      )}

      {embedded && status?.connected ? (
        <p className="text-[13px] text-muted-foreground">
          {t("settings.oauth.codex.connected")}
          {status.email ? ` · ${status.email}` : ""}
          {status.plan ? ` (${status.plan})` : ""}
        </p>
      ) : null}

      {loopbackUrl ? (
        <div className="space-y-2 rounded-[8px] border border-border/60 bg-muted/40 p-3 text-[13px]">
          <p>{t("settings.oauth.browserOpened", { provider: "ChatGPT" })}</p>
          <p className="text-muted-foreground">
            {t("settings.oauth.didntOpen")}{" "}
            <a className="underline" href={loopbackUrl} target="_blank" rel="noreferrer">
              {t("settings.oauth.openManually")}
            </a>
          </p>
          <p className="text-muted-foreground">{t("settings.oauth.waiting")}</p>
          <p className="text-[12px] text-muted-foreground">
            {t("settings.oauth.codex.notWorking")}{" "}
            <button
              type="button"
              className="underline hover:text-foreground"
              onClick={() => void switchToDeviceCode()}
            >
              {t("settings.oauth.codex.useDeviceCode")}
            </button>
          </p>
        </div>
      ) : null}

      {challenge ? (
        <div className="space-y-2 rounded-[8px] border border-border/60 bg-muted/40 p-3 text-[13px]">
          <p>
            {t("settings.oauth.codex.stepOpen")}{" "}
            <a
              className="underline"
              href={challenge.verification_uri}
              target="_blank"
              rel="noreferrer"
            >
              {challenge.verification_uri}
            </a>
          </p>
          <p>
            {t("settings.oauth.codex.stepCode")}{" "}
            <span className="font-mono font-semibold">{challenge.user_code}</span>
          </p>
          <p className="text-muted-foreground">{t("settings.oauth.waiting")}</p>
          <p className="text-[12px] text-muted-foreground">
            {t("settings.oauth.codex.deviceCodeHint")}
          </p>
        </div>
      ) : null}

      {error ? <p className="text-[12px] text-destructive">{error}</p> : null}

      <div className="flex items-center justify-end gap-2">
        {status?.connected && !confirmDisconnect ? (
          <Button
            size="sm"
            variant="outline"
            disabled={busy}
            onClick={() => setConfirmDisconnect(true)}
          >
            {t("settings.oauth.disconnect")}
          </Button>
        ) : null}
        {confirmDisconnect ? (
          <div className="flex items-center gap-2 rounded-[8px] border border-border/60 bg-muted/40 p-2">
            <span className="text-[12px]">{t("settings.oauth.codex.confirmDisconnect")}</span>
            <Button
              size="sm"
              variant="destructive"
              disabled={busy}
              onClick={() => void doDisconnect()}
            >
              {t("settings.oauth.codex.confirmDisconnectYes")}
            </Button>
            <Button
              size="sm"
              variant="outline"
              disabled={busy}
              onClick={() => setConfirmDisconnect(false)}
            >
              {t("settings.oauth.cancel")}
            </Button>
          </div>
        ) : null}
        {!status?.connected && !challenge && !loopbackUrl ? (
          <div className="flex flex-col items-end gap-1.5">
            <Button size="sm" disabled={busy} onClick={() => void connect()}>
              {t("settings.oauth.codex.connect")}
            </Button>
            {status?.can_loopback ? (
              <button
                type="button"
                className="text-[12px] text-muted-foreground underline hover:text-foreground"
                disabled={busy}
                onClick={() => void switchToDeviceCode()}
              >
                {t("settings.oauth.codex.notWorkingUseDeviceCode")}
              </button>
            ) : null}
          </div>
        ) : null}
      </div>
    </div>
  );
}

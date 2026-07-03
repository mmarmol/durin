import { useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";

import { Button } from "@/components/ui/button";
import {
  disconnectOpenRouter,
  fetchOpenRouterStatus,
  startOpenRouterLoopbackAuth,
  type OpenRouterStatus,
} from "@/lib/api";

type Props = {
  token: string;
  base?: string;
  /** Called after a successful connect/disconnect so the parent can refresh settings. */
  onChanged?: () => void;
};

// The server-side loopback flow gives up at 180s; stop polling shortly after
// so the card surfaces a retryable timeout instead of spinning forever.
const POLL_TIMEOUT_MS = 190_000;

/**
 * "Connect with OpenRouter" — loopback PKCE that ends in a regular API key,
 * stored exactly like a manual paste. Rendered ABOVE the manual key form in
 * the provider row: both paths stay available (OpenRouter has no device-code
 * flow, so on a remote gateway only the manual paste works and the button is
 * not offered).
 */
export function OpenRouterOAuthCard({ token, base = "", onChanged }: Props) {
  const { t } = useTranslation();
  const [status, setStatus] = useState<OpenRouterStatus | null>(null);
  const [loopbackUrl, setLoopbackUrl] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [confirmDisconnect, setConfirmDisconnect] = useState(false);
  const pollTimer = useRef<number | null>(null);

  useEffect(() => {
    fetchOpenRouterStatus(token, base).then(setStatus).catch(() => setStatus(null));
    return () => {
      if (pollTimer.current) window.clearTimeout(pollTimer.current);
    };
  }, [token, base]);

  const pollStatusUntilConnected = () => {
    const deadline = Date.now() + POLL_TIMEOUT_MS;
    const tick = async () => {
      try {
        const s = await fetchOpenRouterStatus(token, base);
        if (s.connected) {
          setLoopbackUrl(null);
          setBusy(false);
          setStatus(s);
          onChanged?.();
          return;
        }
        if (Date.now() >= deadline) {
          setLoopbackUrl(null);
          setBusy(false);
          setError(t("settings.oauth.timeout"));
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

  const connect = async () => {
    setError(null);
    setBusy(true);
    try {
      const { authorize_url } = await startOpenRouterLoopbackAuth(token, base);
      setLoopbackUrl(authorize_url);
      window.open(authorize_url, "_blank", "noopener");
      pollStatusUntilConnected();
    } catch (e) {
      setError((e as Error).message);
      setBusy(false);
    }
  };

  const doDisconnect = async () => {
    setConfirmDisconnect(false);
    setBusy(true);
    try {
      setStatus(await disconnectOpenRouter(token, base));
      onChanged?.();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  // Remote gateway: the loopback callback can't reach the user's browser and
  // OpenRouter has no device-code fallback — the manual form below is the way.
  if (!status || (!status.can_loopback && !status.connected)) return null;

  return (
    <div className="space-y-2 rounded-[8px] border border-border/45 bg-muted/25 p-3">
      <div className="flex items-center justify-between gap-2">
        <p className="text-[13px]">
          {status.connected ? (
            <>
              {t("settings.oauth.openrouter.connectedVia")}
              {status.api_key_hint ? (
                <span className="ml-1.5 font-mono text-[12px] text-muted-foreground">
                  {status.api_key_hint}
                </span>
              ) : null}
            </>
          ) : (
            t("settings.oauth.openrouter.description")
          )}
        </p>
        {!status.connected && !loopbackUrl ? (
          <Button size="sm" disabled={busy} onClick={() => void connect()}>
            {t("settings.oauth.openrouter.connect")}
          </Button>
        ) : null}
        {status.connected && !confirmDisconnect ? (
          <Button
            size="sm"
            variant="outline"
            disabled={busy}
            onClick={() => setConfirmDisconnect(true)}
          >
            {t("settings.oauth.disconnect")}
          </Button>
        ) : null}
      </div>

      {confirmDisconnect ? (
        <div className="flex items-center gap-2 rounded-[8px] border border-border/60 bg-muted/40 p-2">
          <span className="text-[12px]">{t("settings.oauth.openrouter.confirmForget")}</span>
          <Button
            size="sm"
            variant="destructive"
            disabled={busy}
            onClick={() => void doDisconnect()}
          >
            {t("settings.oauth.openrouter.confirmForgetYes")}
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

      {loopbackUrl ? (
        <div className="space-y-1.5 text-[13px]">
          <p>{t("settings.oauth.browserOpened", { provider: "OpenRouter" })}</p>
          <p className="text-muted-foreground">
            {t("settings.oauth.didntOpen")}{" "}
            <a className="underline" href={loopbackUrl} target="_blank" rel="noreferrer">
              {t("settings.oauth.openManually")}
            </a>
          </p>
          <p className="text-muted-foreground">{t("settings.oauth.waiting")}</p>
        </div>
      ) : null}

      {error ? <p className="text-[12px] text-destructive">{error}</p> : null}
    </div>
  );
}

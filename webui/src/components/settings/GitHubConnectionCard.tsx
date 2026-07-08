import { useCallback, useEffect, useRef, useState } from "react";
import { Check, Github, Loader2, RefreshCw } from "lucide-react";
import { useTranslation } from "react-i18next";

import { Button } from "@/components/ui/button";
import { SettingsRow } from "@/components/settings/primitives";
import {
  disconnectGithub,
  fetchGithubStatus,
  pollGithubDeviceFlow,
  startGithubDeviceFlow,
  type GithubStatus,
} from "@/lib/api";

/**
 * The shared GitHub credential, surfaced as one row (not a section). Device-flow
 * connect (one shared token for skills, MCP discovery, and the provider), a live
 * "test" that re-probes GitHub, and an inline-confirmed disconnect. Minimal scope
 * by default (`read:user`); private-repo access is requested only when needed.
 */
export function GitHubConnectionRow({ token, base = "" }: { token: string; base?: string }) {
  const { t } = useTranslation();
  const [status, setStatus] = useState<GithubStatus | null>(null);
  const [challenge, setChallenge] = useState<{ code: string; url: string } | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [confirmForget, setConfirmForget] = useState(false);
  const pollTimer = useRef<number | null>(null);

  const refresh = useCallback(() => {
    fetchGithubStatus(token, base)
      .then(setStatus)
      .catch(() => setStatus({ connected: false }));
  }, [token, base]);

  useEffect(() => {
    refresh();
    return () => {
      if (pollTimer.current) window.clearTimeout(pollTimer.current);
    };
  }, [refresh]);

  const connect = useCallback(async () => {
    setError(null);
    setBusy(true);
    try {
      const ch = await startGithubDeviceFlow(token, {}, base);
      setChallenge({ code: ch.user_code, url: ch.verification_uri_complete });
      window.open(ch.verification_uri_complete, "_blank", "noopener");
      const deadline = Date.now() + ch.expires_in * 1000;
      let interval = ch.interval;
      const tick = async () => {
        try {
          const p = await pollGithubDeviceFlow(token, ch.flow_id, base);
          if (p.status === "authorized") {
            setChallenge(null);
            setBusy(false);
            refresh();
            return;
          }
          if (["expired", "denied", "error"].includes(p.status)) {
            setChallenge(null);
            setBusy(false);
            setError(p.error || t("settings.github.flowEnded"));
            return;
          }
          if (p.status === "slow_down") interval += 5;
          if (Date.now() >= deadline) {
            setChallenge(null);
            setBusy(false);
            setError(t("settings.github.timeout"));
            return;
          }
          pollTimer.current = window.setTimeout(tick, interval * 1000);
        } catch (e) {
          setError((e as Error).message);
          setBusy(false);
        }
      };
      pollTimer.current = window.setTimeout(tick, interval * 1000);
    } catch (e) {
      setError((e as Error).message);
      setBusy(false);
    }
  }, [token, base, refresh, t]);

  const cancel = useCallback(() => {
    if (pollTimer.current) window.clearTimeout(pollTimer.current);
    setChallenge(null);
    setBusy(false);
  }, []);

  const doDisconnect = useCallback(async () => {
    setConfirmForget(false);
    setBusy(true);
    try {
      setStatus(await disconnectGithub(token, base));
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }, [token, base]);

  const connected = !!status?.connected;
  const reachable = !!status?.reachable;

  const title = (
    <span className="flex flex-wrap items-center gap-2">
      <Github className="h-4 w-4" aria-hidden />
      {t("settings.github.title")}
      {connected && reachable ? (
        <>
          {status?.login ? (
            <span className="font-mono text-[12px] font-normal text-muted-foreground">
              @{status.login}
            </span>
          ) : null}
          {status?.scopes ? (
            <span className="rounded-full bg-primary/10 px-2 py-0.5 text-[11px] font-medium text-primary">
              {status.scopes}
            </span>
          ) : null}
        </>
      ) : null}
    </span>
  );

  let description: React.ReactNode;
  if (challenge) {
    description = (
      <span className="flex flex-wrap items-center gap-x-2 gap-y-1">
        <span className="font-mono text-[12px] tracking-[0.15em] text-foreground">
          {challenge.code}
        </span>
        <span>{t("settings.github.enterCode")}</span>
        <a className="underline" href={challenge.url} target="_blank" rel="noreferrer">
          github.com/login/device
        </a>
      </span>
    );
  } else if (connected && reachable) {
    description = (
      <span className="flex items-center gap-1.5 text-emerald-600 dark:text-emerald-400">
        <Check className="h-3.5 w-3.5" aria-hidden />
        {t("settings.github.reachable")}
        {status?.rate_remaining != null && status?.rate_limit != null
          ? ` · ${t("settings.github.rateLeft", {
              remaining: status.rate_remaining.toLocaleString(),
              limit: status.rate_limit.toLocaleString(),
            })}`
          : ""}
      </span>
    );
  } else if (connected && !reachable) {
    description = (
      <span className="text-destructive">{t("settings.github.rejected")}</span>
    );
  } else {
    description = t("settings.github.usedBy");
  }

  let action: React.ReactNode;
  if (challenge) {
    action = (
      <div className="flex items-center gap-2">
        <span className="flex items-center gap-1.5 text-[12px] text-muted-foreground">
          <Loader2 className="h-3.5 w-3.5 animate-spin" aria-hidden />
          {t("settings.github.waiting")}
        </span>
        <Button size="sm" variant="ghost" className="rounded-full" onClick={cancel}>
          {t("settings.github.cancel")}
        </Button>
      </div>
    );
  } else if (connected && confirmForget) {
    action = (
      <div className="flex flex-wrap items-center justify-end gap-2">
        <span className="text-[12px] text-muted-foreground">
          {t("settings.github.forgetConfirm")}
        </span>
        <Button
          size="sm"
          variant="destructive"
          className="rounded-full"
          disabled={busy}
          onClick={() => void doDisconnect()}
        >
          {t("settings.github.forget")}
        </Button>
        <Button
          size="sm"
          variant="outline"
          className="rounded-full"
          onClick={() => setConfirmForget(false)}
        >
          {t("settings.github.cancel")}
        </Button>
      </div>
    );
  } else if (connected) {
    action = (
      <div className="flex gap-2">
        <Button
          size="sm"
          variant="outline"
          className="rounded-full"
          disabled={busy}
          onClick={refresh}
        >
          <RefreshCw className="mr-1.5 h-3.5 w-3.5" aria-hidden />
          {reachable ? t("settings.github.test") : t("settings.github.reconnect")}
        </Button>
        <Button
          size="sm"
          variant="outline"
          className="rounded-full"
          disabled={busy}
          onClick={() => setConfirmForget(true)}
        >
          {t("settings.github.disconnect")}
        </Button>
      </div>
    );
  } else {
    action = (
      <Button size="sm" className="rounded-full" disabled={busy} onClick={() => void connect()}>
        {t("settings.github.connect")}
      </Button>
    );
  }

  return (
    <>
      <SettingsRow title={title} description={description}>
        {action}
      </SettingsRow>
      {error ? (
        <div className="px-5 pb-3 text-[12px] text-destructive">{error}</div>
      ) : null}
    </>
  );
}

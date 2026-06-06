import { useState } from "react";
import { useTranslation } from "react-i18next";

import { Button } from "@/components/ui/button";
import { ensureExtra, type ExtraStatus } from "@/lib/api";

/**
 * Inline confirmation shown when activating a feature whose pip extra is
 * missing. Shows the extra + download size, an optional restart checkbox, and
 * installs via /api/extras/ensure. On success it calls onDone(restarting); the
 * caller proceeds with the original action unless a restart was kicked off.
 */
export function ExtraInstallPrompt({
  token,
  feature,
  status,
  onDone,
  onCancel,
}: {
  token: string;
  feature: string;
  status: ExtraStatus;
  onDone: (restarting: boolean) => void;
  onCancel: () => void;
}) {
  const { t } = useTranslation();
  const [restart, setRestart] = useState(status.needs_restart);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const confirm = async () => {
    setBusy(true);
    setError(null);
    try {
      const res = await ensureExtra(token, feature, restart);
      if (res.status === "failed" || res.status === "disabled") {
        setError(res.message);
        setBusy(false);
        return;
      }
      onDone(Boolean(res.restarting));
    } catch (err) {
      setError((err as Error).message);
      setBusy(false);
    }
  };

  return (
    <div className="mt-2 rounded-lg border border-border bg-muted/40 p-3 text-[13px]">
      <p>
        {t("settings.extras.willInstall", {
          extra: status.extra,
          size: status.approx_size,
        })}
      </p>
      {status.needs_restart ? (
        <label className="mt-2 flex items-center gap-2">
          <input
            type="checkbox"
            checked={restart}
            onChange={(e) => setRestart(e.target.checked)}
          />
          {t("settings.extras.restartAfter")}
        </label>
      ) : null}
      {error ? <p className="mt-2 text-destructive">{error}</p> : null}
      <div className="mt-3 flex gap-2">
        <Button
          size="sm"
          variant="outline"
          disabled={busy}
          onClick={() => void confirm()}
          className="rounded-full"
        >
          {busy ? t("settings.extras.installing") : t("settings.extras.install")}
        </Button>
        <Button
          size="sm"
          variant="ghost"
          disabled={busy}
          onClick={onCancel}
          className="rounded-full"
        >
          {t("settings.extras.cancel")}
        </Button>
      </div>
    </div>
  );
}

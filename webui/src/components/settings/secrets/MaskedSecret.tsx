import { useState } from "react";
import { Link2Off, Lock, RotateCcw } from "lucide-react";
import { useTranslation } from "react-i18next";

import { Button } from "@/components/ui/button";
import {
  Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { useClient } from "@/providers/ClientProvider";

export function MaskedSecret({
  secretName,
  serviceLabel,
  busy,
  onDisconnect,
}: {
  secretName: string;
  serviceLabel: string;
  busy: boolean;
  onDisconnect: () => void;
}) {
  const { t } = useTranslation();
  const { client } = useClient();
  const [dialogOpen, setDialogOpen] = useState(false);
  const [newValue, setNewValue] = useState("");
  const [rotating, setRotating] = useState(false);
  const [rotateError, setRotateError] = useState<string | null>(null);

  const rotate = async () => {
    const v = newValue.trim();
    if (!v) return;
    setRotating(true);
    setRotateError(null);
    try {
      await client.storeSecret({ name: secretName, service: serviceLabel, value: v });
      setDialogOpen(false);
      setNewValue("");
    } catch (e) {
      setRotateError(e instanceof Error ? e.message : String(e));
    } finally {
      setRotating(false);
    }
  };

  return (
    <div className="flex items-center gap-2">
      <span
        className="inline-flex items-center gap-1.5 rounded-full bg-muted px-2.5 py-1 text-[11px] font-mono text-muted-foreground"
        title={`\${secret:${secretName}}`}
      >
        <Lock className="h-3 w-3" aria-hidden />
        {secretName}
      </span>
      <Button size="sm" variant="ghost" disabled={busy} onClick={() => setDialogOpen(true)}
        className="rounded-full" title={t("settings.config.secretRotateHint")}>
        <RotateCcw className="mr-1 h-3 w-3" aria-hidden />
        {t("settings.config.secretRotate")}
      </Button>
      <Button size="sm" variant="ghost" disabled={busy} onClick={onDisconnect}
        className="rounded-full text-muted-foreground" title={t("settings.config.secretDisconnectHint")}>
        <Link2Off className="mr-1 h-3 w-3" aria-hidden />
        {t("settings.config.secretDisconnect")}
      </Button>
      <Dialog open={dialogOpen} onOpenChange={setDialogOpen}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>{t("settings.config.secretRotateTitle", { name: secretName })}</DialogTitle>
            <DialogDescription>{t("settings.config.secretRotateDescription", { name: secretName })}</DialogDescription>
          </DialogHeader>
          <Input type="password" autoFocus value={newValue}
            onChange={(e) => setNewValue(e.target.value)}
            onKeyDown={(e) => { if (e.key === "Enter") void rotate(); }}
            placeholder={t("settings.config.secretRotatePlaceholder")} />
          {rotateError ? <div className="text-[12px] text-destructive">{rotateError}</div> : null}
          <DialogFooter>
            <Button variant="ghost" onClick={() => { setDialogOpen(false); setNewValue(""); setRotateError(null); }}>
              {t("settings.config.secretRotateCancel")}
            </Button>
            <Button disabled={!newValue.trim() || rotating} onClick={() => void rotate()}>
              {rotating ? t("settings.config.secretRotateSaving") : t("settings.config.secretRotateSave")}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  );
}

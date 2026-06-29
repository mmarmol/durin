import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";

import { Button } from "@/components/ui/button";
import { DropdownMenu, DropdownMenuTrigger, DropdownMenuContent, DropdownMenuItem } from "@/components/ui/dropdown-menu";
import { Input } from "@/components/ui/input";
import { listSecrets } from "@/lib/api";
import { useClient } from "@/providers/ClientProvider";
import { ChevronDown } from "lucide-react";
import { MaskedSecret } from "./MaskedSecret";

export function ChannelSecretField({
  secretRef, secretName, serviceLabel, help, busy, token, onSet, onClear,
}: {
  secretRef: string | null;
  secretName: string;     // canonical name to create, e.g. EMAIL_IMAP_PASSWORD
  serviceLabel: string;   // e.g. channel:email
  help?: string;
  busy: boolean;
  token: string;
  onSet: (ref: string) => void;
  onClear: () => void;
}) {
  const { t } = useTranslation();
  const { client } = useClient();
  const [existing, setExisting] = useState<string[]>([]);
  const [mode, setMode] = useState<"choose" | "create">("choose");
  const [value, setValue] = useState("");
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    void listSecrets(token).then((s) => setExisting(s.map((e) => e.name).filter(Boolean)));
  }, [token]);

  // Already wired to a secret → show masked badge with rotate/disconnect.
  // Only extract the name when the value is actually a ${secret:...} reference.
  // A non-matching value (e.g. a masked "***" or a historical plaintext token)
  // must fall through to the choose/create select so the user can replace it.
  const current =
    secretRef && secretRef.startsWith("${secret:")
      ? secretRef.replace(/^\$\{secret:(.+)\}$/, "$1")
      : "";
  if (current) {
    return (
      <div className="space-y-1">
        <MaskedSecret secretName={current} serviceLabel={serviceLabel} busy={busy} onDisconnect={onClear} />
        {help ? <p className="text-[12px] text-muted-foreground">{help}</p> : null}
      </div>
    );
  }

  const createNew = async () => {
    const v = value.trim();
    if (!v) return;
    setSaving(true);
    try {
      await client.storeSecret({ name: secretName, value: v, service: serviceLabel, scope: [serviceLabel] });
      onSet(`\${secret:${secretName}}`);
      setValue("");
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className="space-y-2">
      {mode === "choose" ? (
        <DropdownMenu>
          <DropdownMenuTrigger asChild>
            <Button type="button" variant="outline" disabled={busy}
              className="h-8 min-w-[210px] justify-between rounded-full border-input bg-background px-3 text-[13px] font-normal shadow-none">
              <span className="truncate">{t("settings.channels.secretNone")}</span>
              <ChevronDown className="ml-2 h-3.5 w-3.5 shrink-0 text-muted-foreground" aria-hidden />
            </Button>
          </DropdownMenuTrigger>
          <DropdownMenuContent align="end"
            className="max-h-[18rem] w-[240px] overflow-y-auto rounded-[18px] border-border/65 bg-popover p-1.5">
            <DropdownMenuItem className="rounded-full text-[12px]" onSelect={() => onClear()}>
              {t("settings.channels.secretNone")}
            </DropdownMenuItem>
            {existing.map((s) => (
              <DropdownMenuItem key={s} className="rounded-full text-[12px]"
                onSelect={() => onSet(`\${secret:${s}}`)}>
                <span className="truncate">{s}</span>
              </DropdownMenuItem>
            ))}
            <DropdownMenuItem className="rounded-full text-[12px]" onSelect={() => setMode("create")}>
              {t("settings.channels.secretCreate")}
            </DropdownMenuItem>
          </DropdownMenuContent>
        </DropdownMenu>
      ) : (
        <div className="flex flex-wrap items-center gap-2">
          <Input type="password" value={value} autoFocus
            onChange={(e) => setValue(e.target.value)}
            placeholder={t("settings.channels.secretValuePlaceholder", { name: secretName })}
            className="w-[280px]" />
          <Button size="sm" variant="outline" disabled={saving || !value.trim()}
            onClick={() => void createNew()} className="rounded-full">
            {t("settings.channels.save")}
          </Button>
          <Button size="sm" variant="ghost" onClick={() => { setMode("choose"); setValue(""); }}
            className="rounded-full text-muted-foreground">
            {t("settings.channels.cancel")}
          </Button>
        </div>
      )}
      {help ? <p className="text-[12px] text-muted-foreground">{help}</p> : null}
    </div>
  );
}

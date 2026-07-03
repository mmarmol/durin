import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";

import { listPersonas } from "@/lib/api";

/** Dropdown over the user's personas with an explicit "default" empty option.
 *  Used for per-channel identity (channels.<name>.persona) and per-chat
 *  overrides (channels.slack.chat_personas). */
export function PersonaSelect({
  token,
  value,
  busy,
  onChange,
  compact = false,
}: {
  token: string;
  value: string;
  busy?: boolean;
  onChange: (v: string) => void;
  compact?: boolean;
}) {
  const { t } = useTranslation();
  const [names, setNames] = useState<string[] | null>(null);

  useEffect(() => {
    let cancelled = false;
    listPersonas(token)
      .then((res) => {
        if (!cancelled) setNames(res.personas.map((p) => p.name));
      })
      .catch(() => {
        if (!cancelled) setNames([]);
      });
    return () => {
      cancelled = true;
    };
  }, [token]);

  return (
    <select
      value={value}
      disabled={busy || names === null}
      onChange={(e) => onChange(e.target.value)}
      className={
        (compact ? "h-7 text-[12px]" : "h-9 text-[13px]") +
        " rounded-md border border-border/60 bg-background px-2"
      }
    >
      <option value="">{t("settings.channels.personaDefault")}</option>
      {(names ?? []).map((name) => (
        <option key={name} value={name}>
          {name}
        </option>
      ))}
      {/* Keep an unknown configured value visible instead of silently blanking it */}
      {value && names !== null && !names.includes(value) ? (
        <option value={value}>{value}</option>
      ) : null}
    </select>
  );
}

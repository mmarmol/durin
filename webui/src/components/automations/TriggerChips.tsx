import { useTranslation } from "react-i18next";

import type { AutomationTrigger } from "@/lib/api";
import { fmtDateTime } from "@/lib/format";

const CHANNEL_LABEL_KEYS: Record<string, string> = {
  email: "automations.chips.channel_email",
  telegram: "automations.chips.channel_telegram",
  slack: "automations.chips.channel_slack",
  discord: "automations.chips.channel_discord",
  whatsapp: "automations.chips.channel_whatsapp",
};

const chipCls =
  "inline-flex items-center rounded-full border border-border bg-muted/60 px-2 py-0.5 text-[11px] text-muted-foreground";
const chipMonoCls =
  "inline-flex items-center rounded-full border border-border bg-muted/60 px-2 py-0.5 font-mono text-[11px] text-muted-foreground";

/** One chip per trigger, in the mockup's four flavors (schedule/channel/
 *  webhook/chain), plus a correlate chip when a channel or webhook trigger
 *  sets one. Renders a React fragment (no wrapping element) so the caller's
 *  own flex-wrap row can interleave these chips with its other meta text —
 *  matching the mockup, where triggers and "runs <workflow>" sit in the
 *  same wrapped line. */
export function TriggerChips({ triggers }: { triggers: AutomationTrigger[] }) {
  const { t, i18n } = useTranslation();
  const locale = i18n.resolvedLanguage ?? i18n.language;

  function everyLabel(ms: number): string {
    if (ms % 86_400_000 === 0) return t("automations.chips.everyDays", { count: ms / 86_400_000 });
    if (ms % 3_600_000 === 0) return t("automations.chips.everyHours", { count: ms / 3_600_000 });
    if (ms % 60_000 === 0) return t("automations.chips.everyMinutes", { count: ms / 60_000 });
    return t("automations.chips.everySeconds", { count: Math.round(ms / 1000) });
  }

  function timeOfDay(atMs: number, tz: string | undefined): string {
    try {
      return new Intl.DateTimeFormat(locale, {
        hour: "2-digit",
        minute: "2-digit",
        hour12: false,
        timeZone: tz,
      }).format(new Date(atMs));
    } catch {
      return "";
    }
  }

  function scheduleLabel(schedule: AutomationTrigger["schedule"]): string {
    if (!schedule) return t("automations.chips.scheduleGeneric");
    if (schedule.kind === "every" && schedule.every_ms != null) {
      const every = everyLabel(schedule.every_ms);
      const clock = schedule.at_ms != null ? timeOfDay(schedule.at_ms, schedule.tz) : "";
      return clock ? `${every} · ${clock}` : every;
    }
    if (schedule.kind === "at" && schedule.at_ms != null) {
      return t("automations.chips.scheduleAt", { when: fmtDateTime(schedule.at_ms, locale) });
    }
    if (schedule.kind === "cron") {
      return schedule.expr
        ? `${t("automations.chips.scheduleCron")} · ${schedule.expr}`
        : t("automations.chips.scheduleCron");
    }
    return t("automations.chips.scheduleGeneric");
  }

  function channelLabel(channel: string | undefined): string {
    if (!channel) return "";
    const key = CHANNEL_LABEL_KEYS[channel];
    return key ? t(key) : channel;
  }

  const chips: { key: string; mono?: boolean; content: string }[] = [];
  triggers.forEach((trig, i) => {
    if (trig.source === "schedule") {
      chips.push({ key: `${i}-schedule`, content: `⏰ ${scheduleLabel(trig.schedule)}` });
    } else if (trig.source === "channel") {
      const chat = trig.filters?.chat;
      chips.push({
        key: `${i}-channel`,
        content: `💬 ${channelLabel(trig.channel)}${chat ? ` · ${chat}` : ""}`,
      });
    } else if (trig.source === "webhook") {
      chips.push({ key: `${i}-webhook`, content: `🪝 ${t("automations.chips.webhook", { hook: trig.hook ?? "" })}` });
    } else if (trig.source === "chain") {
      chips.push({
        key: `${i}-chain`,
        content: `⛓ ${t("automations.chips.chain", { name: trig.chain_automation ?? "" })}`,
      });
    }
    if ((trig.source === "channel" || trig.source === "webhook") && trig.correlate) {
      chips.push({ key: `${i}-correlate`, mono: true, content: trig.correlate });
    }
  });

  if (chips.length === 0) return null;
  return (
    <>
      {chips.map((chip) => (
        <span key={chip.key} className={chip.mono ? chipMonoCls : chipCls}>
          {chip.content}
        </span>
      ))}
    </>
  );
}

import { useTranslation } from "react-i18next";

import type { SkillHistory as SkillHistoryData, SkillHistoryEntry } from "@/lib/api";
import { cn } from "@/lib/utils";

interface Props {
  history: SkillHistoryData;
}

/** Dot color for the timeline connector. */
const ACTOR_DOT: Record<SkillHistoryEntry["actor"], string> = {
  user: "bg-primary",
  agent: "bg-sky-500",
  curation: "bg-violet-500",
  import: "bg-amber-500",
  system: "bg-muted-foreground",
};

function ActorBadge({ actor }: { actor: SkillHistoryEntry["actor"] }) {
  return (
    <span
      className={cn(
        "shrink-0 rounded-full px-1.5 py-0.5 text-[10px] font-medium leading-none",
        // background is lighter; we use the text-* class from ACTOR_COLOR
        actor === "user" && "bg-primary/10 text-primary",
        actor === "agent" && "bg-sky-500/10 text-sky-600 dark:text-sky-400",
        actor === "curation" && "bg-violet-500/10 text-violet-600 dark:text-violet-400",
        actor === "import" && "bg-amber-500/10 text-amber-600 dark:text-amber-400",
        actor === "system" && "bg-muted text-muted-foreground",
      )}
    >
      {actor}
    </span>
  );
}

function TimelineEntry({ entry }: { entry: SkillHistoryEntry }) {
  const date = new Date(entry.timestamp);
  const dateStr = Number.isNaN(date.getTime())
    ? entry.timestamp
    : date.toLocaleString(undefined, {
        month: "short",
        day: "numeric",
        hour: "2-digit",
        minute: "2-digit",
      });

  return (
    <div className="flex gap-3">
      {/* Timeline dot + vertical connector */}
      <div className="flex flex-col items-center">
        <div className={cn("mt-1 h-2 w-2 shrink-0 rounded-full", ACTOR_DOT[entry.actor])} />
        <div className="flex-1 w-px bg-border/40" />
      </div>

      {/* Content */}
      <div className="min-w-0 pb-4">
        <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
          <ActorBadge actor={entry.actor} />
          <span className="text-[11px] text-muted-foreground">{dateStr}</span>
          {entry.sha ? (
            <span className="font-mono text-[10px] text-muted-foreground/60">
              {entry.sha.slice(0, 7)}
            </span>
          ) : null}
        </div>
        <p className="mt-0.5 text-[12px] leading-snug text-foreground">{entry.subject}</p>
        {entry.session ? (
          <p className="mt-0.5 truncate font-mono text-[10px] text-muted-foreground/60">
            {entry.session}
          </p>
        ) : null}
      </div>
    </div>
  );
}

/**
 * Presentational timeline of a skill's git history plus a provenance bar
 * (source origin, first-seen date, security verdict, fused-from ancestry).
 * i18n keys (`skills.history.*`, `skills.security`) are wired in Task 17.
 */
export function SkillHistory({ history }: Props) {
  const { t } = useTranslation();
  const { provenance, commits } = history;

  return (
    <div className="flex flex-col gap-4">
      {/* Provenance bar */}
      {(provenance.source ||
        provenance.created_at ||
        provenance.verdict ||
        (provenance.fused_from && provenance.fused_from.length > 0)) ? (
        <div className="rounded-[8px] border border-border/40 bg-muted/20 px-3 py-2">
          <p className="mb-1.5 text-[11px] font-semibold uppercase tracking-wide text-muted-foreground">
            {t("skills.history.provenance")}
          </p>
          <dl className="flex flex-col gap-1">
            {provenance.source ? (
              <div className="flex gap-2 text-[12px]">
                <dt className="shrink-0 text-muted-foreground">{t("skills.history.source")}</dt>
                <dd className="truncate text-foreground">{provenance.source}</dd>
              </div>
            ) : null}
            {provenance.created_at ? (
              <div className="flex gap-2 text-[12px]">
                <dt className="shrink-0 text-muted-foreground">{t("skills.history.createdAt")}</dt>
                <dd className="text-foreground">{provenance.created_at}</dd>
              </div>
            ) : null}
            {provenance.verdict ? (
              <div className="flex gap-2 text-[12px]">
                <dt className="shrink-0 text-muted-foreground">{t("skills.security")}</dt>
                <dd className="text-foreground">{provenance.verdict}</dd>
              </div>
            ) : null}
            {provenance.fused_from && provenance.fused_from.length > 0 ? (
              <div className="flex gap-2 text-[12px]">
                <dt className="shrink-0 text-muted-foreground">{t("skills.history.fusedFrom")}</dt>
                <dd className="truncate text-foreground">{provenance.fused_from.join(", ")}</dd>
              </div>
            ) : null}
          </dl>
        </div>
      ) : null}

      {/* Commit timeline */}
      {commits.length === 0 ? (
        <p className="text-[12px] text-muted-foreground">{t("skills.history.empty")}</p>
      ) : (
        <div>
          <p className="mb-2 text-[11px] font-semibold uppercase tracking-wide text-muted-foreground">
            {t("skills.history.title")}
          </p>
          <div className="flex flex-col">
            {commits.map((entry) => (
              <TimelineEntry key={entry.sha} entry={entry} />
            ))}
          </div>
        </div>
      )}
    </div>
  );
}

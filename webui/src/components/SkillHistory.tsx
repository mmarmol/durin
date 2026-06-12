// webui/src/components/SkillHistory.tsx
import { useTranslation } from "react-i18next";
import type { SkillHistory as SkillHistoryData, SkillHistoryEntry } from "@/lib/api";
import { cn } from "@/lib/utils";

const ACTOR_STYLE: Record<SkillHistoryEntry["actor"], { dot: string; chip: string }> = {
  user: { dot: "bg-emerald-500", chip: "bg-emerald-500/10 text-emerald-600 dark:text-emerald-400" },
  agent: { dot: "bg-primary", chip: "bg-primary/10 text-primary" },
  curation: { dot: "bg-violet-500", chip: "bg-violet-500/10 text-violet-600 dark:text-violet-400" },
  import: { dot: "bg-amber-500", chip: "bg-amber-500/10 text-amber-600 dark:text-amber-400" },
  system: { dot: "bg-muted-foreground/60", chip: "bg-muted text-muted-foreground" },
};

function Meta({ c }: { c: SkillHistoryEntry }) {
  const parts = [c.sha, c.session ? `session ${c.session}` : null, c.agent].filter(Boolean);
  return <div className="mt-0.5 font-mono text-[10.5px] text-muted-foreground">{parts.join(" · ")}</div>;
}

export function SkillHistory({ data }: { data: SkillHistoryData }) {
  const { t } = useTranslation();
  const { provenance: p, commits } = data;
  return (
    <div className="flex flex-col">
      <div className="flex flex-wrap gap-x-4 gap-y-1 border-b border-border/30 px-1 pb-3 text-[12px]">
        {p.source ? (
          <span><span className="text-muted-foreground">{t("skills.history.origin")}:</span> {p.source}</span>
        ) : null}
        {p.created_at ? (
          <span><span className="text-muted-foreground">{t("skills.history.created")}:</span> {p.created_at}</span>
        ) : null}
        {p.verdict ? (
          <span><span className="text-muted-foreground">{t("skills.security")}:</span> {p.verdict}</span>
        ) : null}
      </div>
      {commits.length === 0 ? (
        <p className="px-1 py-4 text-[13px] text-muted-foreground">{t("skills.history.empty")}</p>
      ) : (
        <ul className="flex flex-col">
          {commits.map((c) => {
            const s = ACTOR_STYLE[c.actor] ?? ACTOR_STYLE.system;
            return (
              <li key={c.sha} className="flex gap-3 border-b border-border/20 px-1 py-2.5">
                <span className={cn("mt-1.5 size-2.5 shrink-0 rounded-full", s.dot)} />
                <div className="min-w-0 flex-1">
                  <div className="flex flex-wrap items-center gap-2">
                    <span className={cn("rounded-full px-2 py-0.5 text-[10px] font-semibold", s.chip)}>
                      {t(`skills.history.actor.${c.actor}`)}
                    </span>
                    <span className="text-[12.5px] text-foreground">{c.subject}</span>
                  </div>
                  <Meta c={c} />
                </div>
                <span className="shrink-0 whitespace-nowrap text-[10px] text-muted-foreground">{c.timestamp}</span>
              </li>
            );
          })}
        </ul>
      )}
    </div>
  );
}

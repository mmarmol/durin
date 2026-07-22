import { useTranslation } from "react-i18next";

import type { WorkActivity } from "@/lib/types";

/**
 * One line naming what a running node is doing. The phrase is composed here,
 * from the structured {tool, target} the engine sent — the wire never carries a
 * rendered sentence, so every locale reads this in its own language.
 */
export function NodeActivityLine({ activity }: { activity: WorkActivity }): JSX.Element {
  const { t } = useTranslation();
  const label = t(`work.activity.${activity.tool}`, t("work.activity.fallback", { tool: activity.tool }));
  return (
    <div className="truncate text-[11px] text-muted-foreground" title={activity.target ?? label}>
      {activity.target ? `${label} ${activity.target}` : label}
    </div>
  );
}

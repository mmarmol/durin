import { useTranslation } from "react-i18next";

import { Button } from "@/components/ui/button";
import type { DrawerTarget } from "@/components/DreamDrawer";
import type { QuarantineRow } from "@/lib/api";

interface QuarantineCardProps {
  skill: QuarantineRow;
  onOpen: (target: DrawerTarget) => void;
  /** Opens the import's triage in Skills, where it is approved or rejected. */
  onOpenSkills?: (name: string) => void;
}

/** A skill import waiting in quarantine: its name, verdict and findings, a
 *  quick look in the drawer, and the way to its triage in Skills — the
 *  approval there walks the install gate (confirm, override, replace,
 *  dependencies), which lives with the rest of the Skills surface. */
export function QuarantineCard({ skill, onOpen, onOpenSkills }: QuarantineCardProps) {
  const { t } = useTranslation();

  const verdictSummary = skill.findings.length > 0
    ? skill.findings.map((f) => f.detail).join("; ")
    : skill.verdict;

  function handleView() {
    onOpen({
      ref: skill.name,
      ref_kind: "skill",
      summary: verdictSummary,
    });
  }

  return (
    <div className="flex flex-col gap-2 rounded-[8px] border border-border/40 bg-card px-4 py-3">
      <div className="flex items-start gap-2">
        <div className="flex min-w-0 flex-1 flex-col gap-0.5">
          <div className="flex items-center gap-2">
            <span className="text-[12px] font-medium text-foreground">{skill.name}</span>
            <span className="text-[11px] text-muted-foreground/70">{skill.verdict}</span>
          </div>
          <p className="text-[13px] text-muted-foreground mt-0.5 line-clamp-2">{verdictSummary}</p>
        </div>
        <Button
          type="button"
          variant="ghost"
          size="sm"
          className="shrink-0 text-[12px]"
          onClick={handleView}
        >
          {t("dream.view")}
        </Button>
      </div>
      <div>
        <Button
          type="button"
          variant="outline"
          size="sm"
          className="text-[12px]"
          onClick={() => onOpenSkills?.(skill.name)}
        >
          {t("dream.bandeja.reviewInSkills")}
        </Button>
      </div>
    </div>
  );
}

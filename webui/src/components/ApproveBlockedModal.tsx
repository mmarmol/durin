import { AlertTriangle } from "lucide-react";
import { Button } from "@/components/ui/button";
import { useTranslation } from "react-i18next";

interface Props {
  skillName: string;
  nonInstallableBins: string[];
  onApprove: () => void;
  onCancel: () => void;
}

export function ApproveBlockedModal({ skillName, nonInstallableBins, onApprove, onCancel }: Props) {
  const { t } = useTranslation();
  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40">
      <div className="max-w-md rounded-lg border border-border bg-background p-6 shadow-lg">
        <div className="mb-3 flex items-center gap-2">
          <AlertTriangle className="h-5 w-5 text-amber-500" aria-hidden />
          <h3 className="text-sm font-semibold">
            {t("skills.import.blockedTitle", { name: skillName })}
          </h3>
        </div>
        <p className="mb-3 text-[12px] text-muted-foreground">
          {t("skills.import.blockedDesc")}
        </p>
        {nonInstallableBins.length > 0 && (
          <ul className="mb-4 flex flex-col gap-1">
            {nonInstallableBins.map((b) => (
              <li key={b} className="text-[12px] text-muted-foreground">• {b}</li>
            ))}
          </ul>
        )}
        <div className="flex justify-end gap-2">
          <Button data-testid="cancel-btn" size="sm" variant="outline" onClick={onCancel}>
            {t("skills.import.cancel")}
          </Button>
          <Button data-testid="approve-btn" size="sm" onClick={onApprove}>
            {t("skills.import.approve")}
          </Button>
        </div>
      </div>
    </div>
  );
}

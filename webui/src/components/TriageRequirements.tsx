import { useState } from "react";
import { Check, X, Circle, Apple, Terminal } from "lucide-react";
import { Button } from "@/components/ui/button";
import { installSkillDeps, type SkillRequirements } from "@/lib/api";
import { useTranslation } from "react-i18next";

const PLATFORM_LABELS: Record<string, string> = {
  macos: "macOS",
  linux: "Linux",
  windows: "Windows",
};

const PLATFORM_ICONS: Record<string, typeof Apple> = {
  macos: Apple,
  linux: Terminal,
  windows: Terminal,
};

interface Props {
  requirements: SkillRequirements;
  skillName: string;
  token: string;
  onResolved?: () => void;
  onAskDurin?: (binName: string) => void;
}

export function TriageRequirements({ requirements, skillName,   token, onResolved, onAskDurin }: Props) {
  const { t } = useTranslation();
  const [installing, setInstalling] = useState<string | null>(null);

  async function handleInstall(bin: string) {
    setInstalling(bin);
    try {
      await installSkillDeps(token, skillName, bin);
      onResolved?.();
    } finally {
      setInstalling(null);
    }
  }

  return (
    <div className="flex flex-col gap-3">
      {requirements.platforms.length > 0 && (
        <div className="flex items-center gap-2">
          <span className="text-[11px] font-semibold uppercase tracking-wide text-muted-foreground">
            {t("skills.requirements.platform")}
          </span>
          <div className="flex gap-1">
            {requirements.platforms.map((p) => {
              const Icon = PLATFORM_ICONS[p] || Terminal;
              const isCurrent = requirements.platform_ok;
              return (
                <span
                  key={p}
                  className={`inline-flex items-center gap-1 rounded-full px-2 py-0.5 text-[11px] ${
                    isCurrent
                      ? "bg-primary/10 text-primary"
                      : "bg-muted text-muted-foreground"
                  }`}
                >
                  <Icon className="h-3 w-3" aria-hidden />
                  {PLATFORM_LABELS[p] || p}
                </span>
              );
            })}
          </div>
          {!requirements.platform_ok && (
            <span className="text-[11px] text-destructive">
              {t("skills.requirements.platformMismatch")}
            </span>
          )}
        </div>
      )}

      {requirements.bins.length > 0 && (
        <div>
          <p className="mb-1 text-[11px] font-semibold uppercase tracking-wide text-muted-foreground">
            {t("skills.requirements.tools")}
          </p>
          <ul className="flex flex-col gap-1">
            {requirements.bins.map((bin) => (
              <li key={bin.name} className="flex items-center gap-2 text-[12px]">
                {bin.available ? (
                  <Check className="h-3.5 w-3.5 text-green-500" aria-hidden />
                ) : bin.installable ? (
                  <X className="h-3.5 w-3.5 text-destructive" aria-hidden />
                ) : (
                  <Circle className="h-3.5 w-3.5 text-muted-foreground" aria-hidden />
                )}
                <span className={bin.available ? "text-foreground" : "text-muted-foreground"}>
                  {bin.name}
                </span>
                {!bin.available && bin.installable && (
                  <Button
                    size="sm"
                    variant="outline"
                    className="ml-auto h-6 px-2 text-[11px]"
                    disabled={installing === bin.name}
                    onClick={() => handleInstall(bin.name)}
                  >
                    {installing === bin.name ? "..." : t("skills.requirements.install")}
                  </Button>
                )}
                {!bin.available && !bin.installable && (
                  onAskDurin ? (
                    <button
                      type="button"
                      onClick={() => onAskDurin(bin.name)}
                      className="ml-auto text-[11px] text-primary cursor-pointer hover:underline"
                    >
                      {t("skills.requirements.durinCanHelp")}
                    </button>
                  ) : (
                    <span className="ml-auto text-[11px] text-primary">
                      {t("skills.requirements.durinCanHelp")}
                    </span>
                  )
                )}
              </li>
            ))}
          </ul>
        </div>
      )}

      {requirements.env.length > 0 && (
        <div>
          <p className="mb-1 text-[11px] font-semibold uppercase tracking-wide text-muted-foreground">
            {t("skills.requirements.environment")}
          </p>
          <ul className="flex flex-col gap-1">
            {requirements.env.map((env) => (
              <li key={env.name} className="flex items-center gap-2 text-[12px]">
                {env.available ? (
                  <Check className="h-3.5 w-3.5 text-green-500" aria-hidden />
                ) : (
                  <X className="h-3.5 w-3.5 text-destructive" aria-hidden />
                )}
                <span className={env.available ? "text-foreground" : "text-muted-foreground"}>
                  {env.name}
                </span>
              </li>
            ))}
          </ul>
        </div>
      )}

      {requirements.compatibility && (
        <p className="text-[11px] italic text-muted-foreground">
          {requirements.compatibility}
        </p>
      )}
    </div>
  );
}

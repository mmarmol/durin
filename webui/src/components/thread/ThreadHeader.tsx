import { Menu, Moon, PanelRight, Sun } from "lucide-react";
import { useTranslation } from "react-i18next";

import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";

interface ThreadHeaderProps {
  title: string;
  onToggleSidebar: () => void;
  theme: "light" | "dark";
  onToggleTheme: () => void;
  hideSidebarToggleOnDesktop?: boolean;
  minimal?: boolean;
  /** Toggles the per-chat work panel. Only provided when a session is active. */
  onTogglePanel?: () => void;
  /** Whether the work panel is currently open. */
  panelOpen?: boolean;
  /** Whether there are active work items (drives the badge dot). */
  panelHasActiveWork?: boolean;
}

export function ThreadHeader({
  title,
  onToggleSidebar,
  theme,
  onToggleTheme,
  hideSidebarToggleOnDesktop = false,
  minimal = false,
  onTogglePanel,
  panelOpen = false,
  panelHasActiveWork = false,
}: ThreadHeaderProps) {
  const { t } = useTranslation();
  if (minimal) {
    return (
      <div className="relative z-10 flex h-11 items-center justify-between gap-3 px-3 py-2">
        <Button
          variant="ghost"
          size="icon"
          aria-label={t("thread.header.toggleSidebar")}
          onClick={onToggleSidebar}
          className={cn(
            "h-7 w-7 rounded-md text-muted-foreground hover:bg-accent/35 hover:text-foreground",
            hideSidebarToggleOnDesktop && "lg:pointer-events-none lg:opacity-0",
          )}
        >
          <Menu className="h-3.5 w-3.5" />
        </Button>
        <ThemeButton theme={theme} onToggleTheme={onToggleTheme} label={t("thread.header.toggleTheme")} />
      </div>
    );
  }

  return (
    <div className="relative z-10 flex items-center justify-between gap-3 px-3 py-2">
      <div className="relative flex min-w-0 items-center gap-2">
        <Button
          variant="ghost"
          size="icon"
          aria-label={t("thread.header.toggleSidebar")}
          onClick={onToggleSidebar}
          className={cn(
            "h-7 w-7 rounded-md text-muted-foreground hover:bg-accent/35 hover:text-foreground",
            hideSidebarToggleOnDesktop && "lg:pointer-events-none lg:opacity-0",
          )}
        >
          <Menu className="h-3.5 w-3.5" />
        </Button>
        <div className="flex min-w-0 items-center rounded-md px-1.5 py-1 text-[12px] font-medium text-muted-foreground">
          <span className="max-w-[min(60vw,32rem)] truncate">{title}</span>
        </div>
      </div>

      <div className="flex items-center gap-2">
        {onTogglePanel ? (
          <div className="relative">
            <Button
              variant="ghost"
              size="icon"
              aria-label={t("thread.header.togglePanel")}
              onClick={onTogglePanel}
              className={cn(
                "h-8 w-8 rounded-full hover:bg-accent/40",
                panelOpen
                  ? "text-foreground bg-accent/40"
                  : "text-muted-foreground/85 hover:text-foreground",
              )}
            >
              <PanelRight className="h-4 w-4" />
            </Button>
            {panelHasActiveWork && (
              <span
                className="pointer-events-none absolute right-1 top-1 h-1.5 w-1.5 rounded-full bg-amber-500"
                aria-hidden
              />
            )}
          </div>
        ) : null}
        <ThemeButton theme={theme} onToggleTheme={onToggleTheme} label={t("thread.header.toggleTheme")} />
      </div>

      <div aria-hidden className="pointer-events-none absolute inset-x-0 top-full h-4" />
    </div>
  );
}

function ThemeButton({
  theme,
  onToggleTheme,
  label,
}: {
  theme: "light" | "dark";
  onToggleTheme: () => void;
  label: string;
}) {
  return (
    <Button
      variant="ghost"
      size="icon"
      aria-label={label}
      onClick={onToggleTheme}
      className="h-8 w-8 rounded-full text-muted-foreground/85 hover:bg-accent/40 hover:text-foreground"
    >
      {theme === "dark" ? (
        <Sun className="h-4 w-4" />
      ) : (
        <Moon className="h-4 w-4" />
      )}
    </Button>
  );
}

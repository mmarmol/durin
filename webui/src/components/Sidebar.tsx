import { useMemo, useState } from "react";
import {
  Menu,
  Moon,
  Network,
  Search,
  Settings,
  Sparkles,
  SquarePen,
  Workflow,
} from "lucide-react";
import { useTranslation } from "react-i18next";

import { ChatList } from "@/components/ChatList";
import { ConnectionBadge } from "@/components/ConnectionBadge";
import { Button } from "@/components/ui/button";
import { Separator } from "@/components/ui/separator";
import { cn } from "@/lib/utils";
import type { ChatSummary } from "@/lib/types";

interface SidebarProps {
  sessions: ChatSummary[];
  activeKey: string | null;
  loading: boolean;
  onNewChat: () => void;
  onSelect: (key: string) => void;
  onRequestDelete: (key: string, label: string) => void;
  onRequestRename: (key: string, title: string) => Promise<void>;
  onOpenSettings: () => void;
  onOpenMemoryGraph?: () => void;
  memoryGraphActive?: boolean;
  onOpenSkills?: () => void;
  skillsActive?: boolean;
  onOpenWorkflows?: () => void;
  workflowsActive?: boolean;
  onOpenDream?: () => void;
  dreamActive?: boolean;
  onCollapse: () => void;
}

export function Sidebar(props: SidebarProps) {
  const { t } = useTranslation();
  const [query, setQuery] = useState("");
  const normalizedQuery = query.trim().toLowerCase();
  const filteredSessions = useMemo(() => {
    if (!normalizedQuery) return props.sessions;
    const terms = normalizedQuery.split(/\s+/).filter(Boolean);
    return props.sessions.filter((session) => {
      const haystack = [
        session.title,
        session.preview,
        session.chatId,
        session.channel,
        session.key,
      ]
        .filter(Boolean)
        .join(" ")
        .toLowerCase();
      return terms.every((term) => haystack.includes(term));
    });
  }, [normalizedQuery, props.sessions]);

  return (
    <nav
      aria-label={t("sidebar.navigation")}
      className="flex h-full w-full min-w-0 flex-col border-r border-sidebar-border/60 bg-sidebar text-sidebar-foreground"
    >
      <div className="flex items-center justify-between px-3 pb-2.5 pt-3">
        {/* Wordmark: durin shield logo + lowercase name. Logo asset is
            shipped under /brand/durin-logo.png (see webui/public/brand). */}
        <span
          className="flex select-none items-center gap-2 text-base font-semibold tracking-tight"
          aria-label="durin"
        >
          <img
            src="/brand/durin-logo-64.png"
            alt=""
            aria-hidden="true"
            className="h-6 w-auto"
          />
          <span className="lowercase">durin</span>
        </span>
        <Button
          variant="ghost"
          size="icon"
          aria-label={t("sidebar.collapse")}
          onClick={props.onCollapse}
          className="h-7 w-7 rounded-lg text-muted-foreground/85 hover:bg-sidebar-accent/75 hover:text-sidebar-foreground"
        >
          <Menu className="h-3.5 w-3.5" />
        </Button>
      </div>

      <div className="space-y-1.5 px-2 pb-2">
        <label className="relative block">
          <span className="sr-only">{t("sidebar.searchAria")}</span>
          <Search
            className="pointer-events-none absolute left-3 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-muted-foreground/70"
            aria-hidden
          />
          <input
            value={query}
            onChange={(event) => setQuery(event.target.value)}
            placeholder={t("sidebar.searchPlaceholder")}
            aria-label={t("sidebar.searchAria")}
            className={cn(
              "h-8 w-full rounded-full border border-transparent bg-sidebar-accent/45",
              "pl-8 pr-3 text-[12.5px] text-sidebar-foreground outline-none",
              "placeholder:text-muted-foreground/75",
              "transition-colors hover:bg-sidebar-accent/65",
              "focus:border-sidebar-border/80 focus:bg-sidebar-accent/70",
              "focus:ring-1 focus:ring-sidebar-border/70",
            )}
          />
        </label>
        <Button
          onClick={props.onNewChat}
          className="h-8 w-full justify-start gap-2 rounded-full px-3 text-[12.5px] font-medium text-sidebar-foreground/92 hover:bg-sidebar-accent/75 hover:text-sidebar-foreground"
          variant="ghost"
        >
          <SquarePen className="h-3.5 w-3.5" />
          {t("sidebar.newChat")}
        </Button>
      </div>
      <div className="flex min-h-0 min-w-0 flex-1 flex-col overflow-hidden">
        <ChatList
          sessions={filteredSessions}
          activeKey={props.activeKey}
          loading={props.loading}
          emptyLabel={
            normalizedQuery ? t("sidebar.noSearchResults") : t("chat.noSessions")
          }
          onSelect={props.onSelect}
          onRequestDelete={props.onRequestDelete}
          onRequestRename={props.onRequestRename}
        />
      </div>
      {/* Below the sessions list: entry into the entity-centric memory
          Obsidian-style graph view. Click takes the main pane (sessions
          stay listed in this sidebar so the user can swap back). */}
      {props.onOpenMemoryGraph ? (
        <>
          <Separator className="bg-sidebar-border/50" />
          <div className="px-2.5 py-2">
            <Button
              type="button"
              variant="ghost"
              onClick={props.onOpenMemoryGraph}
              className={cn(
                "h-8 w-full justify-start gap-2 rounded-full px-2.5 text-[12.5px] font-medium",
                props.memoryGraphActive
                  ? "bg-sidebar-accent/80 text-sidebar-foreground"
                  : "text-sidebar-foreground/85 hover:bg-sidebar-accent/75 hover:text-sidebar-foreground",
              )}
              aria-pressed={!!props.memoryGraphActive}
            >
              <Network className="h-3.5 w-3.5" aria-hidden />
              {t("memoryGraph.title")}
            </Button>
          </div>
        </>
      ) : null}
      {props.onOpenSkills ? (
        <div className="px-2.5 pb-2">
          <Button
            type="button"
            variant="ghost"
            onClick={props.onOpenSkills}
            className={cn(
              "h-8 w-full justify-start gap-2 rounded-full px-2.5 text-[12.5px] font-medium",
              props.skillsActive
                ? "bg-sidebar-accent/80 text-sidebar-foreground"
                : "text-sidebar-foreground/85 hover:bg-sidebar-accent/75 hover:text-sidebar-foreground",
            )}
            aria-pressed={!!props.skillsActive}
          >
            <Sparkles className="h-3.5 w-3.5" aria-hidden />
            {t("skills.title")}
          </Button>
        </div>
      ) : null}
      {props.onOpenWorkflows ? (
        <div className="px-2.5 pb-2">
          <Button
            type="button"
            variant="ghost"
            onClick={props.onOpenWorkflows}
            className={cn(
              "h-8 w-full justify-start gap-2 rounded-full px-2.5 text-[12.5px] font-medium",
              props.workflowsActive
                ? "bg-sidebar-accent/80 text-sidebar-foreground"
                : "text-sidebar-foreground/85 hover:bg-sidebar-accent/75 hover:text-sidebar-foreground",
            )}
            aria-pressed={!!props.workflowsActive}
          >
            <Workflow className="h-3.5 w-3.5" aria-hidden />
            {t("workflows.title")}
          </Button>
        </div>
      ) : null}
      {props.onOpenDream ? (
        <div className="px-2.5 pb-2">
          <Button
            type="button"
            variant="ghost"
            onClick={props.onOpenDream}
            className={cn(
              "h-8 w-full justify-start gap-2 rounded-full px-2.5 text-[12.5px] font-medium",
              props.dreamActive
                ? "bg-sidebar-accent/80 text-sidebar-foreground"
                : "text-sidebar-foreground/85 hover:bg-sidebar-accent/75 hover:text-sidebar-foreground",
            )}
            aria-pressed={!!props.dreamActive}
          >
            <Moon className="h-3.5 w-3.5" aria-hidden />
            {t("dream.title")}
          </Button>
        </div>
      ) : null}
      <Separator className="bg-sidebar-border/50" />
      <div className="space-y-1 px-2.5 py-2.5 text-xs">
        <Button
          type="button"
          variant="ghost"
          onClick={props.onOpenSettings}
          className="h-8 w-full justify-start gap-2 rounded-full px-2.5 text-[12.5px] font-medium text-sidebar-foreground/85 hover:bg-sidebar-accent/75 hover:text-sidebar-foreground"
        >
          <Settings className="h-3.5 w-3.5" aria-hidden />
          {t("sidebar.settings")}
        </Button>
        <ConnectionBadge />
      </div>
    </nav>
  );
}

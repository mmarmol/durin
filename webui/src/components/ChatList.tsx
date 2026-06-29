import { useEffect, useRef, useState } from "react";
import { MoreHorizontal, Pencil, Trash2 } from "lucide-react";
import { useTranslation } from "react-i18next";

import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { cn } from "@/lib/utils";
import type { ChatSummary } from "@/lib/types";

interface ChatListProps {
  sessions: ChatSummary[];
  activeKey: string | null;
  onSelect: (key: string) => void;
  onRequestDelete: (key: string, label: string) => void;
  /** P2: persist a user-edited title for a session. Returns when the
   *  server has acknowledged so the caller can revert the optimistic
   *  update on failure. */
  onRequestRename: (key: string, title: string) => Promise<void>;
  loading?: boolean;
  emptyLabel?: string;
}

export function ChatList({
  sessions,
  activeKey,
  onSelect,
  onRequestDelete,
  onRequestRename,
  loading,
  emptyLabel,
}: ChatListProps) {
  const { t } = useTranslation();
  // Which session row is currently being inline-renamed (only one at a
  // time — mirrors the OS file-manager rename UX).
  const [editingKey, setEditingKey] = useState<string | null>(null);
  const [draftTitle, setDraftTitle] = useState("");
  const inputRef = useRef<HTMLInputElement | null>(null);

  // Auto-focus + select when entering rename mode so the user can
  // type-to-replace or arrow-edit immediately.
  useEffect(() => {
    if (editingKey && inputRef.current) {
      inputRef.current.focus();
      inputRef.current.select();
    }
  }, [editingKey]);

  const beginEdit = (key: string, current: string) => {
    setEditingKey(key);
    setDraftTitle(current);
  };
  const cancelEdit = () => {
    setEditingKey(null);
    setDraftTitle("");
  };
  const commitEdit = async (key: string) => {
    const trimmed = draftTitle.trim();
    // Empty or unchanged: silently cancel — don't punish the user.
    if (!trimmed) {
      cancelEdit();
      return;
    }
    try {
      await onRequestRename(key, trimmed);
    } catch {
      // Best-effort: leave the row visible with its previous label;
      // a hard error here is uncommon enough that surfacing a banner
      // would be overkill. The server is authoritative either way.
    } finally {
      cancelEdit();
    }
  };
  if (loading && sessions.length === 0) {
    return (
      <div className="px-3 py-6 text-[12px] text-muted-foreground">
        {t("chat.loading")}
      </div>
    );
  }

  if (sessions.length === 0) {
    return (
      <div className="px-3 py-6 text-[12px] leading-5 text-muted-foreground/80">
        {emptyLabel ?? t("chat.noSessions")}
      </div>
    );
  }

  const groups = groupSessions(sessions, {
    today: t("chat.groups.today"),
    yesterday: t("chat.groups.yesterday"),
    earlier: t("chat.groups.earlier"),
  });

  return (
    <div className="h-full min-h-0 min-w-0 overflow-x-hidden overflow-y-auto overscroll-contain">
      <div className="min-w-0 space-y-3 px-2 py-1.5">
        {groups.map((group) => (
          <section key={group.label} aria-label={group.label}>
            <div className="px-2 pb-1 text-[12px] font-medium text-muted-foreground/65">
              {group.label}
            </div>
            <ul className="space-y-0.5">
              {group.sessions.map((s) => {
                const active = s.key === activeKey;
                const fallbackTitle = t("chat.fallbackTitle", {
                  id: s.chatId.slice(0, 6),
                });
                const rawLabel = (s.title || s.preview)?.trim();
                const title = rawLabel || fallbackTitle;
                const isEditing = editingKey === s.key;
                return (
                  <li key={s.key} className="min-w-0">
                    <div
                      className={cn(
                        "group flex min-h-8 min-w-0 max-w-full items-center gap-2 rounded-xl px-2 text-[13px] transition-colors",
                        active
                          ? "bg-sidebar-accent/70 text-sidebar-accent-foreground shadow-[inset_0_0_0_1px_hsl(var(--sidebar-border)/0.28)]"
                          : "text-sidebar-foreground/82 hover:bg-sidebar-accent/50 hover:text-sidebar-foreground",
                      )}
                    >
                      {isEditing ? (
                        <input
                          ref={inputRef}
                          value={draftTitle}
                          onChange={(e) => setDraftTitle(e.target.value)}
                          onKeyDown={(e) => {
                            if (e.key === "Enter") {
                              e.preventDefault();
                              void commitEdit(s.key);
                            } else if (e.key === "Escape") {
                              e.preventDefault();
                              cancelEdit();
                            }
                          }}
                          onBlur={() => void commitEdit(s.key)}
                          maxLength={60}
                          aria-label={t("chat.renameAria", { defaultValue: "Rename chat" })}
                          className={cn(
                            "min-w-0 flex-1 rounded-md border border-sidebar-border/50",
                            "bg-background/80 px-2 py-1 text-[13px] font-medium",
                            "text-sidebar-foreground outline-none",
                            "focus:border-sidebar-border focus:ring-1 focus:ring-sidebar-border/70",
                          )}
                        />
                      ) : (
                        <button
                          type="button"
                          onClick={() => onSelect(s.key)}
                          onDoubleClick={() => beginEdit(s.key, rawLabel || "")}
                          title={rawLabel || fallbackTitle}
                          className="min-w-0 flex-1 overflow-hidden py-1.5 text-left"
                        >
                          <span className="block w-full truncate font-medium leading-5">{title}</span>
                          {s.channel && s.channel !== "websocket" && (
                            <span className="mt-0.5 inline-block rounded-full bg-emerald-500/15 px-1.5 py-px text-[10px] font-medium capitalize leading-none text-emerald-700 dark:text-emerald-400">
                              {s.channel}
                            </span>
                          )}
                        </button>
                      )}
                      {isEditing ? null : (
                        <DropdownMenu modal={false}>
                          <DropdownMenuTrigger
                            className={cn(
                              "inline-flex h-6 w-6 shrink-0 items-center justify-center rounded-md text-muted-foreground/75 opacity-40 transition-opacity",
                              "hover:bg-sidebar-accent hover:text-sidebar-foreground group-hover:opacity-100",
                              "focus-visible:opacity-100",
                              active && "opacity-100",
                            )}
                            aria-label={t("chat.actions", { title })}
                          >
                            <MoreHorizontal className="h-3.5 w-3.5" />
                          </DropdownMenuTrigger>
                          <DropdownMenuContent
                            align="end"
                            onCloseAutoFocus={(event) => event.preventDefault()}
                          >
                            <DropdownMenuItem
                              onSelect={() => {
                                window.setTimeout(
                                  () => beginEdit(s.key, rawLabel || ""),
                                  0,
                                );
                              }}
                            >
                              <Pencil className="mr-2 h-4 w-4" />
                              {t("chat.rename", { defaultValue: "Rename" })}
                            </DropdownMenuItem>
                            <DropdownMenuItem
                              onSelect={() => {
                                window.setTimeout(() => onRequestDelete(s.key, title), 0);
                              }}
                              className="text-destructive focus:text-destructive"
                            >
                              <Trash2 className="mr-2 h-4 w-4" />
                              {t("chat.delete")}
                            </DropdownMenuItem>
                          </DropdownMenuContent>
                        </DropdownMenu>
                      )}
                    </div>
                  </li>
                );
              })}
            </ul>
          </section>
        ))}
      </div>
    </div>
  );
}

function groupSessions(
  sessions: ChatSummary[],
  labels: { today: string; yesterday: string; earlier: string },
): Array<{ label: string; sessions: ChatSummary[] }> {
  const now = new Date();
  const startOfToday = new Date(now.getFullYear(), now.getMonth(), now.getDate()).getTime();
  const startOfYesterday = startOfToday - 24 * 60 * 60 * 1000;
  const buckets = new Map<string, ChatSummary[]>();

  for (const session of sessions) {
    const timestamp = Date.parse(session.updatedAt ?? session.createdAt ?? "");
    const label = Number.isFinite(timestamp) && timestamp >= startOfToday
      ? labels.today
      : Number.isFinite(timestamp) && timestamp >= startOfYesterday
        ? labels.yesterday
        : labels.earlier;
    const bucket = buckets.get(label) ?? [];
    bucket.push(session);
    buckets.set(label, bucket);
  }

  return [labels.today, labels.yesterday, labels.earlier]
    .map((label) => ({ label, sessions: buckets.get(label) ?? [] }))
    .filter((group) => group.sessions.length > 0);
}

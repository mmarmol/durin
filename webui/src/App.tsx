import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { DeleteConfirm } from "@/components/DeleteConfirm";
import { Sidebar } from "@/components/Sidebar";
import { MemoryGraphView } from "@/components/MemoryGraphView";
import { SkillsView } from "@/components/SkillsView";
import { ToastProvider } from "@/components/ui/toast";
import { SettingsView } from "@/components/settings/SettingsView";
import { ThreadShell } from "@/components/thread/ThreadShell";
import { VoiceDock } from "@/components/voice/VoiceDock";
import { Sheet, SheetContent } from "@/components/ui/sheet";

import { useSessions } from "@/hooks/useSessions";
import { useTheme } from "@/hooks/useTheme";
import { cn } from "@/lib/utils";
import { setApiReauthHandler } from "@/lib/api";
import { deriveWsUrl, fetchBootstrap, signout } from "@/lib/bootstrap";
import { DurinClient } from "@/lib/durin-client";
import { ClientProvider, useClient } from "@/providers/ClientProvider";
import type { ChatSummary } from "@/lib/types";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";

type BootState =
  | { status: "loading" }
  | { status: "error"; message: string }
  | { status: "auth"; failed?: boolean }
  | {
      status: "ready";
      client: DurinClient;
      token: string;
      modelName: string | null;
      modelPreset: string | null;
      // True when this deploy gates bootstrap on a setup secret. The
      // shell uses this to decide whether to surface the Logout
      // affordance — see Shell + SettingsView.
      requiresSecret: boolean;
    };

const SIDEBAR_STORAGE_KEY = "durin-webui.sidebar";
const RESTART_STARTED_KEY = "durin-webui.restartStartedAt";
const SIDEBAR_WIDTH = 272;
type ShellView = "chat" | "settings" | "memory_graph" | "skills";

function AuthForm({
  failed,
  onSecret,
}: {
  failed: boolean;
  onSecret: (secret: string) => void;
}) {
  const { t } = useTranslation();
  const [value, setValue] = useState("");
  const [submitting, setSubmitting] = useState(false);

  const handleSubmit = (e: React.FormEvent) => {
    e.preventDefault();
    const secret = value.trim();
    if (!secret) return;
    setSubmitting(true);
    onSecret(secret);
  };

  return (
    <div className="flex h-full w-full items-center justify-center px-6">
      <form
        onSubmit={handleSubmit}
        className="flex w-full max-w-sm flex-col gap-4"
      >
        <div className="flex flex-col items-center gap-1 text-center">
          <p className="text-lg font-semibold">{t("app.auth.title")}</p>
          <p className="text-sm text-muted-foreground">{t("app.auth.hint")}</p>
        </div>
        {failed && (
          <p className="text-center text-sm text-destructive">
            {t("app.auth.invalid")}
          </p>
        )}
        <Input
          type="password"
          placeholder={t("app.auth.placeholder")}
          value={value}
          onChange={(e) => setValue(e.target.value)}
          disabled={submitting}
          autoFocus
        />
        <Button
          type="submit"
          className="w-full"
          disabled={!value.trim() || submitting}
        >
          {t("app.auth.submit")}
        </Button>
      </form>
    </div>
  );
}

function readSidebarOpen(): boolean {
  if (typeof window === "undefined") return true;
  try {
    const raw = window.localStorage.getItem(SIDEBAR_STORAGE_KEY);
    if (raw === null) return true;
    return raw === "1";
  } catch {
    return true;
  }
}

export default function App() {
  const { t } = useTranslation();
  const [state, setState] = useState<BootState>({ status: "loading" });

  const bootstrapWithSecret = useCallback(
    (secret: string) => {
      let cancelled = false;
      (async () => {
        setState({ status: "loading" });
        try {
          // Secret only on the initial sign-in; the gateway then sets the
          // httpOnly session cookie. Reloads/reconnects pass no secret and are
          // re-authorized by that cookie — nothing is stored client-side.
          const boot = await fetchBootstrap("", secret);
          if (cancelled) return;
          const url = deriveWsUrl(boot.ws_path, boot.token);
          const client = new DurinClient({
            url,
            onReauth: async () => {
              try {
                const refreshed = await fetchBootstrap("", "");
                return deriveWsUrl(refreshed.ws_path, refreshed.token);
              } catch {
                return null;
              }
            },
          });
          client.connect();
          setState({
            status: "ready",
            client,
            token: boot.token,
            modelName: boot.model_name ?? null,
            modelPreset: boot.model_preset ?? null,
            requiresSecret: Boolean(boot.requires_secret),
          });
        } catch (e) {
          if (cancelled) return;
          const msg = (e as Error).message;
          if (msg.includes("HTTP 401") || msg.includes("HTTP 403")) {
            setState({ status: "auth", failed: true });
          } else {
            setState({ status: "error", message: msg });
          }
        }
      })();
      return () => {
        cancelled = true;
      };
    },
    [],
  );

  useEffect(() => {
    // No secret on load: the httpOnly session cookie (if present) re-authorizes,
    // and localhost-only deploys auto-mint. A 401 falls through to the auth form.
    return bootstrapWithSecret("");
  }, [bootstrapWithSecret]);

  // Recover REST calls after a gateway restart: a restart wipes the
  // in-memory token pool, so the cached token 401s. On a 401, `request`
  // calls this to mint a fresh token and retry — no page reload needed.
  useEffect(() => {
    // Dedupe concurrent 401s into a single in-flight bootstrap, and retry it
    // with backoff so a call that 401s *during* a gateway restart recovers once
    // the gateway is back (a few seconds) rather than erroring until a reload.
    let inFlight: Promise<string | null> | null = null;
    setApiReauthHandler(() => {
      if (inFlight) return inFlight;
      inFlight = (async () => {
        for (let attempt = 0; attempt < 5; attempt++) {
          try {
            const boot = await fetchBootstrap("", "");
            setState((s) => (s.status === "ready" ? { ...s, token: boot.token } : s));
            return boot.token;
          } catch {
            if (attempt < 4) {
              await new Promise((r) => setTimeout(r, 400 * (attempt + 1)));
            }
          }
        }
        return null;
      })();
      void inFlight.finally(() => {
        inFlight = null;
      });
      return inFlight;
    });
    return () => setApiReauthHandler(null);
  }, []);

  if (state.status === "loading") {
    return (
      <div className="flex h-full w-full items-center justify-center">
        <div className="flex flex-col items-center gap-3 animate-in fade-in-0 duration-300">
          <div className="flex items-center gap-2 text-sm text-muted-foreground">
            <span className="relative flex h-2 w-2">
              <span className="absolute inline-flex h-full w-full animate-ping rounded-full bg-foreground/40" />
              <span className="relative inline-flex h-2 w-2 rounded-full bg-foreground/60" />
            </span>
            {t("app.loading.connecting")}
          </div>
        </div>
      </div>
    );
  }
  if (state.status === "auth") {
    return (
      <AuthForm
        failed={!!state.failed}
        onSecret={(s) => bootstrapWithSecret(s)}
      />
    );
  }
  if (state.status === "error") {
    return (
      <div className="flex h-full w-full items-center justify-center px-4 text-center">
        <div className="flex max-w-md flex-col items-center gap-3">
          <p className="text-lg font-semibold">{t("app.error.title")}</p>
          <p className="text-sm text-muted-foreground">{state.message}</p>
          <p className="text-xs text-muted-foreground">
            {t("app.error.gatewayHint")}
          </p>
        </div>
      </div>
    );
  }

  const handleModelNameChange = (
    modelName: string | null,
    modelPreset: string | null = null,
  ) => {
    setState((current) =>
      current.status === "ready"
        ? { ...current, modelName, modelPreset }
        : current,
    );
  };

  const handleLogout = () => {
    if (state.status === "ready") {
      state.client.close();
    }
    void signout();
    setState({ status: "auth" });
  };

  return (
    <ClientProvider
      client={state.client}
      token={state.token}
      modelName={state.modelName}
      modelPreset={state.modelPreset}
    >
      <Shell
        onModelNameChange={handleModelNameChange}
        // Only expose Logout when the deploy actually requires a
        // secret to bootstrap. In localhost-only mode, signing out
        // would land the user on an auth form they can't fill (the
        // gateway auto-mints tokens). Hide the affordance entirely.
        onLogout={state.requiresSecret ? handleLogout : undefined}
      />
    </ClientProvider>
  );
}

function Shell({
  onModelNameChange,
  onLogout,
}: {
  onModelNameChange: (
    modelName: string | null,
    modelPreset?: string | null,
  ) => void;
  onLogout?: () => void;
}) {
  const { t, i18n } = useTranslation();
  const { client } = useClient();
  const { theme, toggle, palette, setPalette } = useTheme();
  const { sessions, loading, refresh, createChat, deleteChat, renameChat } = useSessions();
  const [activeKey, setActiveKey] = useState<string | null>(null);
  const [view, setView] = useState<ShellView>("chat");
  const [pendingPrompt, setPendingPrompt] = useState<string | null>(null);
  const [desktopSidebarOpen, setDesktopSidebarOpen] =
    useState<boolean>(readSidebarOpen);
  const [mobileSidebarOpen, setMobileSidebarOpen] = useState(false);
  const [pendingDelete, setPendingDelete] = useState<{
    key: string;
    label: string;
  } | null>(null);
  const restartSawDisconnectRef = useRef(false);
  const [restartToast, setRestartToast] = useState<string | null>(null);
  const [isRestarting, setIsRestarting] = useState(false);

  useEffect(() => {
    try {
      window.localStorage.setItem(
        SIDEBAR_STORAGE_KEY,
        desktopSidebarOpen ? "1" : "0",
      );
    } catch {
      // ignore storage errors (private mode, etc.)
    }
  }, [desktopSidebarOpen]);



  const activeSession = useMemo<ChatSummary | null>(() => {
    if (!activeKey) return null;
    return sessions.find((s) => s.key === activeKey) ?? null;
  }, [sessions, activeKey]);

  const closeDesktopSidebar = useCallback(() => {
    setDesktopSidebarOpen(false);
  }, []);

  const closeMobileSidebar = useCallback(() => {
    setMobileSidebarOpen(false);
  }, []);

  const toggleSidebar = useCallback(() => {
    const isDesktop =
      typeof window !== "undefined" &&
      window.matchMedia("(min-width: 1024px)").matches;
    if (isDesktop) {
      setDesktopSidebarOpen((v) => !v);
    } else {
      setMobileSidebarOpen((v) => !v);
    }
  }, []);

  const onCreateChat = useCallback(async () => {
    try {
      const chatId = await createChat();
      setActiveKey(`websocket:${chatId}`);
      setView("chat");
      setMobileSidebarOpen(false);
      return chatId;
    } catch (e) {
      console.error("Failed to create chat", e);
      return null;
    }
  }, [createChat]);

  // Voice always runs on a real, focused chat: reuse the active one, else
  // create + focus a new chat so the spoken conversation is visible (never a
  // hidden default session).
  const ensureVoiceChat = useCallback(
    async () => (activeSession ? activeSession.chatId : await onCreateChat()),
    [activeSession, onCreateChat],
  );

  const onNewChat = useCallback(() => {
    setActiveKey(null);
    setView("chat");
    setMobileSidebarOpen(false);
  }, []);

  const onSelectChat = useCallback(
    (key: string) => {
      setActiveKey(key);
      setView("chat");
      setMobileSidebarOpen(false);
    },
    [],
  );

  const onOpenSettings = useCallback(() => {
    setView("settings");
    setMobileSidebarOpen(false);
  }, []);

  const onOpenMemoryGraph = useCallback(() => {
    setView("memory_graph");
    setMobileSidebarOpen(false);
  }, []);

  const onOpenSkills = useCallback(() => {
    setView("skills");
    setMobileSidebarOpen(false);
  }, []);

  const onBackToChat = useCallback(() => {
    setView("chat");
    setMobileSidebarOpen(false);
    setActiveKey((current) => {
      if (!current) return null;
      if (sessions.some((session) => session.key === current)) return current;
      return sessions[0]?.key ?? null;
    });
  }, [sessions]);

  const onRestart = useCallback(() => {
    const chatId = activeSession?.chatId ?? client.defaultChatId;
    if (!chatId) return;
    restartSawDisconnectRef.current = false;
    setIsRestarting(true);
    try {
      window.localStorage.setItem(RESTART_STARTED_KEY, String(Date.now()));
    } catch {
      // ignore storage errors
    }
    client.sendMessage(chatId, "/restart");
  }, [activeSession?.chatId, client]);

  useEffect(() => {
    return client.onRuntimeModelUpdate((modelName, modelPreset) => {
      onModelNameChange(modelName, modelPreset ?? null);
    });
  }, [client, onModelNameChange]);

  useEffect(() => {
    return client.onStatus((status) => {
      let startedAt = 0;
      try {
        startedAt = Number(window.localStorage.getItem(RESTART_STARTED_KEY) ?? "0");
      } catch {
        startedAt = 0;
      }
      if (!startedAt) return;
      if (status !== "open") {
        restartSawDisconnectRef.current = true;
        return;
      }
      const elapsedMs = Date.now() - startedAt;
      if (!restartSawDisconnectRef.current && elapsedMs < 1500) return;
      try {
        window.localStorage.removeItem(RESTART_STARTED_KEY);
      } catch {
        // ignore storage errors
      }
      setIsRestarting(false);
      setRestartToast(t("app.restart.completed", { seconds: (elapsedMs / 1000).toFixed(1) }));
      window.setTimeout(() => setRestartToast(null), 3_500);
    });
  }, [client, t]);

  const onTurnEnd = useCallback(() => {
    void refresh();
  }, [refresh]);

  const onConfirmDelete = useCallback(async () => {
    if (!pendingDelete) return;
    const key = pendingDelete.key;
    const deletingActive = activeKey === key;
    const currentIndex = sessions.findIndex((s) => s.key === key);
    const fallbackKey = deletingActive
      ? (sessions[currentIndex + 1]?.key ?? sessions[currentIndex - 1]?.key ?? null)
      : activeKey;
    setPendingDelete(null);
    if (deletingActive) setActiveKey(fallbackKey);
    try {
      await deleteChat(key);
    } catch (e) {
      if (deletingActive) setActiveKey(key);
      console.error("Failed to delete session", e);
    }
  }, [pendingDelete, deleteChat, activeKey, sessions]);

  const headerTitle = activeSession
    ? activeSession.title ||
      activeSession.preview ||
      t("chat.fallbackTitle", { id: activeSession.chatId.slice(0, 6) })
    : t("app.brand");

  useEffect(() => {
    if (view === "settings") {
      document.title = t("app.documentTitle.chat", {
        title: t("settings.sidebar.title"),
      });
      return;
    }
    document.title = activeSession
      ? t("app.documentTitle.chat", { title: headerTitle })
      : t("app.documentTitle.base");
  }, [activeSession, headerTitle, i18n.resolvedLanguage, t, view]);

  const sidebarProps = {
    sessions,
    activeKey,
    loading,
    onNewChat,
    onSelect: onSelectChat,
    onRequestDelete: (key: string, label: string) =>
      setPendingDelete({ key, label }),
    onRequestRename: renameChat,
    onOpenSettings,
    onOpenMemoryGraph,
    memoryGraphActive: view === "memory_graph",
    onOpenSkills,
    skillsActive: view === "skills",
  };
  const showMainSidebar = view !== "settings";

  return (
    <ToastProvider>
    <div className="relative flex h-full w-full overflow-hidden">
      {/* Desktop sidebar: in normal flow, so the thread area width stays honest. */}
      {showMainSidebar ? (
        <aside
          className={cn(
            "relative z-20 hidden shrink-0 overflow-hidden lg:block",
            "transition-[width] duration-300 ease-out",
          )}
          style={{ width: desktopSidebarOpen ? SIDEBAR_WIDTH : 0 }}
        >
          <div
            className={cn(
              "absolute inset-y-0 left-0 h-full overflow-hidden bg-sidebar shadow-inner-right",
              "transition-transform duration-300 ease-out",
              desktopSidebarOpen ? "translate-x-0" : "-translate-x-full",
            )}
            style={{ width: SIDEBAR_WIDTH }}
          >
            <Sidebar {...sidebarProps} onCollapse={closeDesktopSidebar} />
          </div>
        </aside>
      ) : null}

      {showMainSidebar ? (
        <Sheet
          open={mobileSidebarOpen}
          onOpenChange={(open) => setMobileSidebarOpen(open)}
        >
          <SheetContent
            side="left"
            showCloseButton={false}
            className="p-0 lg:hidden"
            style={{ width: SIDEBAR_WIDTH, maxWidth: SIDEBAR_WIDTH }}
          >
            <Sidebar {...sidebarProps} onCollapse={closeMobileSidebar} />
          </SheetContent>
        </Sheet>
      ) : null}

      <main className="relative flex h-full min-w-0 flex-1 flex-col">
        <div
          className={cn(
            "absolute inset-0 flex flex-col",
            view !== "chat" && "invisible pointer-events-none",
          )}
        >
          <ThreadShell
            session={activeSession}
            title={headerTitle}
            onToggleSidebar={toggleSidebar}
            onNewChat={onNewChat}
            onCreateChat={onCreateChat}
            onTurnEnd={onTurnEnd}
            theme={theme}
            onToggleTheme={toggle}
            hideSidebarToggleOnDesktop={desktopSidebarOpen}
            pendingPrompt={pendingPrompt}
            onPromptConsumed={() => setPendingPrompt(null)}
          />
        </div>
        {view === "settings" && (
          <div className="absolute inset-0 flex flex-col">
            <SettingsView
              theme={theme}
              onToggleTheme={toggle}
              palette={palette}
              onSelectPalette={setPalette}
              onBackToChat={onBackToChat}
              onModelNameChange={onModelNameChange}
              onLogout={onLogout}
              onRestart={onRestart}
              isRestarting={isRestarting}
              onOpenSession={(key) => {
                setActiveKey(key);
                setView("chat");
              }}
            />
          </div>
        )}
        {view === "memory_graph" && (
          <div className="absolute inset-0 flex flex-col">
            <MemoryGraphView
              active={view === "memory_graph"}
              onToggleSidebar={toggleSidebar}
              hideSidebarToggleOnDesktop={desktopSidebarOpen}
            />
          </div>
        )}
        {view === "skills" && (
          <div className="absolute inset-0 flex flex-col">
            <SkillsView onAskDurin={(binName) => {
              setView("chat");
              setPendingPrompt(`Ayúdame a instalar ${binName}`);
            }} />
          </div>
        )}
      </main>

      <VoiceDock
        chatId={activeSession?.chatId ?? null}
        chatTitle={activeSession?.title ?? activeSession?.preview ?? null}
        onEnsureChat={ensureVoiceChat}
      />

      <DeleteConfirm
        open={!!pendingDelete}
        title={pendingDelete?.label ?? ""}
        onCancel={() => setPendingDelete(null)}
        onConfirm={onConfirmDelete}
      />
      {restartToast ? (
        <div
          role="status"
          className="fixed left-1/2 top-4 z-50 -translate-x-1/2 rounded-full border border-border/70 bg-popover px-4 py-2 text-sm font-medium text-popover-foreground shadow-lg"
        >
          {restartToast}
        </div>
      ) : null}
    </div>
    </ToastProvider>
  );
}

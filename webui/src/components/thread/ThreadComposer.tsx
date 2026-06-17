import {
  useCallback,
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
  type KeyboardEvent as ReactKeyboardEvent,
} from "react";

import { MarkdownText, preloadMarkdownText } from "@/components/MarkdownText";
import {
  Activity,
  ArrowUp,
  BookOpen,
  ChevronDown,
  ChevronUp,
  CircleHelp,
  History,
  ImageIcon,
  Loader2,
  Paperclip,
  RotateCw,
  Sparkles,
  Square,
  SquarePen,
  Target,
  Undo2,
  X,
  Compass,
  type LucideIcon,
} from "lucide-react";
import { useTranslation } from "react-i18next";

import { Button } from "@/components/ui/button";
import {
  useAttachedImages,
  type AttachedImage,
  type AttachmentError,
  MAX_IMAGES_PER_MESSAGE,
} from "@/hooks/useAttachedImages";
import { useClipboardAndDrop } from "@/hooks/useClipboardAndDrop";
import { usePromptHistory } from "@/hooks/usePromptHistory";
import { ModelPickerPopover } from "@/components/thread/ModelPickerPopover";
import { ReasoningEffortPicker } from "@/components/thread/ReasoningEffortPicker";
import type { SendImage } from "@/hooks/useDurinStream";
import type { SlashCommand, GoalStateWsPayload } from "@/lib/types";
import { cn } from "@/lib/utils";

/** ``<input accept>``: aligned with the server's MIME whitelist. SVG is
 * deliberately excluded to avoid an embedded-script XSS surface. */
const ACCEPT_ATTR = "image/png,image/jpeg,image/webp,image/gif";

function formatBytes(n: number): string {
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  return `${(n / (1024 * 1024)).toFixed(1)} MB`;
}

interface ThreadComposerProps {
  onSend: (content: string, images?: SendImage[]) => void;
  disabled?: boolean;
  placeholder?: string;
  isStreaming?: boolean;
  modelLabel?: string | null;
  variant?: "thread" | "hero";
  slashCommands?: SlashCommand[];
  onStop?: () => void;
  /** Unix seconds from server; turn elapsed timer above input while set. */
  runStartedAt?: number | null;
  /** Sustained objective for this chat (WebSocket ``goal_state``). */
  goalState?: GoalStateWsPayload;
  /** Called when user picks a model; receives the exact `/model` argument. */
  onModelPick?: (ref: string) => void;
  /** Called when user picks a reasoning effort level. */
  onEffortPick?: (effort: string) => void;
  /** Active reasoning effort, if known. */
  activeEffort?: string | null;
  /** Whether the active model supports reasoning. */
  canReason?: boolean;
  /** Pre-fill the composer with this prompt (from skills "durin can help"). */
  pendingPrompt?: string | null;
  /** Called when pendingPrompt has been loaded into the textarea. */
  onPromptConsumed?: () => void;
}

const COMMAND_ICONS: Record<string, LucideIcon> = {
  activity: Activity,
  "book-open": BookOpen,
  "circle-help": CircleHelp,
  history: History,
  "rotate-cw": RotateCw,
  sparkles: Sparkles,
  square: Square,
  "square-pen": SquarePen,
  "undo-2": Undo2,
};

const SLASH_PALETTE_GAP_PX = 8;
const SLASH_PALETTE_MAX_HEIGHT_PX = 288;
const SLASH_PALETTE_MIN_HEIGHT_PX = 144;
const SLASH_PALETTE_CHROME_PX = 64;

type SlashPalettePlacement = "above" | "below";

interface SlashPaletteLayout {
  placement: SlashPalettePlacement;
  maxHeight: number;
}

function slashCommandI18nKey(command: string): string {
  return command.replace(/^\//, "").replace(/-/g, "_");
}

function getVisibleBounds(el: HTMLElement): { top: number; bottom: number } {
  let top = 0;
  let bottom = window.innerHeight;
  let parent = el.parentElement;

  while (parent) {
    const style = window.getComputedStyle(parent);
    if (/(auto|scroll|hidden|clip)/.test(style.overflowY)) {
      const rect = parent.getBoundingClientRect();
      top = Math.max(top, rect.top);
      bottom = Math.min(bottom, rect.bottom);
    }
    parent = parent.parentElement;
  }

  return { top, bottom };
}

function goalStateStripPreview(
  goal: GoalStateWsPayload | undefined,
  t: (key: string) => string,
): string | null {
  if (!goal?.active) return null;
  const summary = goal.ui_summary?.trim();
  if (summary) return summary;
  const obj = goal.objective?.trim();
  if (obj) return obj.length > 72 ? `${obj.slice(0, 72)}…` : obj;
  return t("thread.composer.goalStateFallback");
}

const GOAL_PANEL_VIEWPORT_TOP_PAD = 20;
const GOAL_PANEL_GAP_ABOVE_STRIP_PX = 10;
const GOAL_PANEL_MIN_HEIGHT_PX = 112;
const GOAL_PANEL_MAX_VIEWPORT_RATIO = 0.62;

function measureGoalPanelMaxCssHeight(stripTopY: number): number {
  const spaceAboveStrip =
    stripTopY - GOAL_PANEL_VIEWPORT_TOP_PAD - GOAL_PANEL_GAP_ABOVE_STRIP_PX;
  return Math.min(
    Math.max(spaceAboveStrip, GOAL_PANEL_MIN_HEIGHT_PX),
    Math.floor(window.innerHeight * GOAL_PANEL_MAX_VIEWPORT_RATIO),
  );
}

function buildGoalMarkdownBody(summary: string, objective: string): string {
  const s = summary.trim();
  const o = objective.trim();
  if (s && o) return `${s}\n\n---\n\n${o}`;
  return o || s;
}

function RunElapsedStrip({
  startedAt,
  goalState,
}: {
  startedAt: number | null;
  goalState?: GoalStateWsPayload;
}) {
  const { t } = useTranslation();
  const [goalPanelOpen, setGoalPanelOpen] = useState(false);
  const [, setTick] = useState(0);
  const stripWrapperRef = useRef<HTMLDivElement>(null);
  const panelRef = useRef<HTMLDivElement>(null);
  const expandToggleRef = useRef<HTMLButtonElement>(null);
  const [panelMaxPx, setPanelMaxPx] = useState(280);

  useEffect(() => {
    if (startedAt == null) return;
    const id = window.setInterval(() => setTick((n) => n + 1), 1000);
    return () => window.clearInterval(id);
  }, [startedAt]);

  const showTimer = startedAt != null;
  const stripLabel = goalStateStripPreview(goalState, t);
  const showGoal = !!stripLabel?.trim();
  // Non-default agent mode (plan = read-only) and a pending ask_user
  // question ride the same goal_state frame (durin/session/goal_state.py).
  const mode = goalState?.mode?.trim() ?? "";
  const showMode = mode.length > 0;
  // Hide the awaiting-answer strip while a turn is running — the answer
  // (or the question's turn itself) is already being processed.
  const pendingQuestion = goalState?.pending_question?.question?.trim() ?? "";
  const showPendingQuestion = pendingQuestion.length > 0 && !showTimer;
  if (!showTimer && !showGoal && !showMode && !showPendingQuestion) return null;

  const objectiveFull = goalState?.objective?.trim() ?? "";
  const summaryFull = goalState?.ui_summary?.trim() ?? "";
  const canExpandGoal = !!(goalState?.active && (objectiveFull || summaryFull));

  const markdownBody =
    objectiveFull || summaryFull
      ? buildGoalMarkdownBody(summaryFull, objectiveFull)
      : "";

  useLayoutEffect(() => {
    if (!goalPanelOpen) return;

    function relayout(): void {
      const el = stripWrapperRef.current;
      if (!el) return;
      const top = el.getBoundingClientRect().top;
      setPanelMaxPx(measureGoalPanelMaxCssHeight(top));
    }

    relayout();

    preloadMarkdownText();
    const ro =
      typeof ResizeObserver !== "undefined"
        ? new ResizeObserver(() => relayout())
        : null;
    if (stripWrapperRef.current && ro) {
      ro.observe(stripWrapperRef.current);
    }
    window.addEventListener("resize", relayout);
    window.addEventListener("scroll", relayout, true);
    return () => {
      ro?.disconnect();
      window.removeEventListener("resize", relayout);
      window.removeEventListener("scroll", relayout, true);
    };
  }, [goalPanelOpen]);

  useEffect(() => {
    if (!goalPanelOpen) return;

    function onPointerDown(ev: MouseEvent): void {
      const target = ev.target as Node | null;
      if (!target) return;
      if (panelRef.current?.contains(target)) return;
      if (expandToggleRef.current?.contains(target)) return;
      setGoalPanelOpen(false);
    }

    function onKey(ev: KeyboardEvent): void {
      if (ev.key === "Escape") setGoalPanelOpen(false);
    }

    window.addEventListener("mousedown", onPointerDown);
    window.addEventListener("keydown", onKey);
    return () => {
      window.removeEventListener("mousedown", onPointerDown);
      window.removeEventListener("keydown", onKey);
    };
  }, [goalPanelOpen]);

  const elapsed =
    startedAt != null ? Math.max(0, Math.floor(Date.now() / 1000 - startedAt)) : 0;
  const m = Math.floor(elapsed / 60);
  const sec = elapsed % 60;
  const shortElapsed = m > 0 ? `${m}:${sec.toString().padStart(2, "0")}` : `${sec}s`;
  const timerTitle = showTimer
    ? t("thread.composer.runRuntimeTitle", { elapsed: shortElapsed })
    : null;

  const ariaParts = [timerTitle, showGoal ? stripLabel : null].filter(Boolean);
  const ariaLabel = ariaParts.join(" · ");

  return (
    <div ref={stripWrapperRef} className="relative z-30">
      {goalPanelOpen && canExpandGoal && markdownBody ? (
        <div
          ref={panelRef}
          id="durin-goal-panel-root"
          role="dialog"
          aria-modal="false"
          aria-labelledby="durin-goal-panel-title"
          tabIndex={-1}
          className={cn(
            "absolute bottom-[calc(100%+8px)] left-3 right-3 z-[50] flex max-w-none flex-col overflow-hidden",
            "rounded-2xl border border-black/[0.08] bg-card shadow-[0_12px_40px_rgba(15,23,42,0.14)]",
            "backdrop-blur-sm dark:border-white/[0.1] dark:shadow-[0_16px_48px_rgba(0,0,0,0.45)]",
          )}
          style={{ maxHeight: `${Math.round(panelMaxPx)}px` }}
        >
          <div className="flex shrink-0 items-center justify-between gap-2 border-b border-black/[0.06] px-3 py-2 dark:border-white/[0.08]">
            <h2
              id="durin-goal-panel-title"
              className="min-w-0 truncate text-[13px] font-semibold tracking-tight text-foreground"
            >
              {t("thread.composer.goalStateSheetTitle")}
            </h2>
            <button
              type="button"
              className={cn(
                "inline-flex h-8 w-8 shrink-0 items-center justify-center rounded-full",
                "text-muted-foreground transition-colors hover:bg-muted/65 hover:text-foreground",
                "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring",
              )}
              aria-label={t("thread.composer.goalStateCloseAria")}
              onClick={() => setGoalPanelOpen(false)}
            >
              <X className="h-4 w-4" aria-hidden />
            </button>
          </div>
          <div
            id="durin-goal-panel-scroll"
            className="min-h-0 flex-1 overflow-y-auto scrollbar-thin px-3 pb-3 pt-2"
          >
            <MarkdownText className="max-w-none text-[13.5px] leading-relaxed text-foreground/90">
              {markdownBody}
            </MarkdownText>
          </div>
        </div>
      ) : null}
      <div
        className="flex min-h-[36px] items-center gap-2 border-b border-black/[0.04] px-3 py-2 dark:border-white/[0.06]"
        role="status"
        aria-label={ariaLabel}
      >
        {showTimer ? (
          <Activity className="h-4 w-4 shrink-0 text-primary/80" aria-hidden />
        ) : (
          <Target className="h-4 w-4 shrink-0 text-primary/75" aria-hidden />
        )}
        <span className="flex min-w-0 flex-1 items-center gap-1.5 text-[12px] font-medium text-foreground/75">
          {showMode ? (
            <span
              className={cn(
                "inline-flex shrink-0 items-center gap-1 rounded-full border border-primary/30",
                "bg-primary/10 px-2 py-0.5 text-[11px] font-medium text-primary",
              )}
            >
              📐 {mode === "plan" ? t("thread.composer.planMode") : mode}
            </span>
          ) : null}
          {timerTitle ? <span className="shrink-0">{timerTitle}</span> : null}
          {timerTitle && showGoal ? (
            <span className="shrink-0 text-muted-foreground/45" aria-hidden>
              ·
            </span>
          ) : null}
          {showGoal ? (
            <span className="truncate">
              {t("thread.composer.goalStateStrip", { label: stripLabel })}
            </span>
          ) : null}
          {showPendingQuestion ? (
            <span className="truncate text-foreground/85">
              ❓ {t("thread.composer.awaitingAnswer")} ·{" "}
              {pendingQuestion.length > 80
                ? `${pendingQuestion.slice(0, 80)}…`
                : pendingQuestion}
            </span>
          ) : null}
        </span>
        {canExpandGoal ? (
          <button
            ref={expandToggleRef}
            type="button"
            className={cn(
              "inline-flex h-8 w-8 shrink-0 items-center justify-center rounded-full",
              "text-muted-foreground transition-colors hover:bg-muted/55 hover:text-foreground",
              "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring",
            )}
            aria-expanded={goalPanelOpen}
            aria-controls={goalPanelOpen ? "durin-goal-panel-root" : undefined}
            aria-label={t("thread.composer.goalStateExpandAria")}
            title={t("thread.composer.goalStateExpandAria")}
            onClick={() => setGoalPanelOpen((o) => !o)}
          >
            {goalPanelOpen ? (
              <ChevronDown className="h-4 w-4" aria-hidden />
            ) : (
              <ChevronUp className="h-4 w-4" aria-hidden />
            )}
          </button>
        ) : null}
      </div>
    </div>
  );
}

export function ThreadComposer({
  onSend,
  disabled,
  placeholder,
  isStreaming = false,
  modelLabel = null,
  variant = "thread",
  slashCommands = [],
  onStop,
  runStartedAt = null,
  goalState,
  onModelPick,
  onEffortPick,
  activeEffort = null,
  canReason = true,
  pendingPrompt = null,
  onPromptConsumed,
}: ThreadComposerProps) {
  const { t } = useTranslation();
  const [value, setValue] = useState("");
  const [inlineError, setInlineError] = useState<string | null>(null);
  const [slashMenuDismissed, setSlashMenuDismissed] = useState(false);
  const [selectedCommandIndex, setSelectedCommandIndex] = useState(0);
  const [modelPickerOpen, setModelPickerOpen] = useState(false);
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const formRef = useRef<HTMLFormElement>(null);
  const fileInputRef = useRef<HTMLInputElement>(null);
  const chipRefs = useRef(new Map<string, HTMLButtonElement>());
  const isHero = variant === "hero";
  const promptHistory = usePromptHistory();

  useEffect(() => {
    if (pendingPrompt) {
      setValue(pendingPrompt);
      onPromptConsumed?.();
      requestAnimationFrame(() => textareaRef.current?.focus());
    }
  }, [pendingPrompt, onPromptConsumed]);

  const resolvedPlaceholder = isStreaming
    ? t("thread.composer.placeholderStreaming")
    : placeholder ?? t("thread.composer.placeholderThread");

  const { images, enqueue, remove, clear, encoding, full } =
    useAttachedImages();

  const formatRejection = useCallback(
    (reason: AttachmentError): string => {
      const key = `thread.composer.imageRejected.${reason}`;
      return t(key, { max: MAX_IMAGES_PER_MESSAGE });
    },
    [t],
  );

  const addFiles = useCallback(
    (files: File[]) => {
      if (files.length === 0) return;
      const { rejected } = enqueue(files);
      if (rejected.length > 0) {
        setInlineError(formatRejection(rejected[0].reason));
      } else {
        setInlineError(null);
      }
    },
    [enqueue, formatRejection],
  );

  const {
    isDragging,
    onPaste,
    onDragEnter,
    onDragOver,
    onDragLeave,
    onDrop,
  } = useClipboardAndDrop(addFiles);

  useEffect(() => {
    if (disabled) return;
    const el = textareaRef.current;
    if (!el) return;
    const id = requestAnimationFrame(() => el.focus());
    return () => cancelAnimationFrame(id);
  }, [disabled]);

  const readyImages = useMemo(
    () => images.filter((img): img is AttachedImage & { dataUrl: string } =>
      img.status === "ready" && typeof img.dataUrl === "string",
    ),
    [images],
  );
  const hasErrors = images.some((img) => img.status === "error");

  const canSend =
    !disabled
    && !encoding
    && !hasErrors
    && (value.trim().length > 0 || readyImages.length > 0);

  const slashQuery = useMemo(() => {
    if (disabled || slashMenuDismissed || !value.startsWith("/")) return null;
    const commandToken = value.slice(1);
    if (/\s/.test(commandToken)) return null;
    return commandToken.toLowerCase();
  }, [disabled, slashMenuDismissed, value]);

  const filteredSlashCommands = useMemo(() => {
    if (slashQuery === null) return [];
    // No hard cap — the palette container already scrolls (overflow-y-auto)
    // and the per-item ref + scrollIntoView keeps keyboard selection visible.
    // The previous `.slice(0, 8)` truncated the list to 8 even though the
    // backend exposes ~25 commands; the user couldn't reach anything past
    // /dream-log and ↑/↓ wrapped around at 8 making it look like the picker
    // had nothing more.
    return slashCommands.filter((command) => {
      const haystack = [
        command.command,
        command.title,
        command.description,
        command.argHint ?? "",
        t(`thread.composer.slash.commands.${slashCommandI18nKey(command.command)}.title`, {
          defaultValue: "",
        }),
        t(`thread.composer.slash.commands.${slashCommandI18nKey(command.command)}.description`, {
          defaultValue: "",
        }),
      ].join(" ").toLowerCase();
      return haystack.includes(slashQuery);
    });
  }, [slashCommands, slashQuery, t]);

  const showSlashMenu = filteredSlashCommands.length > 0;
  const [slashPaletteLayout, setSlashPaletteLayout] = useState<SlashPaletteLayout>({
    placement: "above",
    maxHeight: SLASH_PALETTE_MAX_HEIGHT_PX,
  });

  useEffect(() => {
    setSelectedCommandIndex(0);
  }, [slashQuery]);

  useEffect(() => {
    if (selectedCommandIndex >= filteredSlashCommands.length) {
      setSelectedCommandIndex(0);
    }
  }, [filteredSlashCommands.length, selectedCommandIndex]);

  useEffect(() => {
    if (!showSlashMenu) return;

    const dismissOnPointerDown = (event: PointerEvent) => {
      const target = event.target;
      if (target instanceof Node && formRef.current?.contains(target)) return;
      setSlashMenuDismissed(true);
    };

    document.addEventListener("pointerdown", dismissOnPointerDown, true);
    return () => {
      document.removeEventListener("pointerdown", dismissOnPointerDown, true);
    };
  }, [showSlashMenu]);

  useLayoutEffect(() => {
    if (!showSlashMenu) return;

    const updateLayout = () => {
      const form = formRef.current;
      if (!form) return;
      const rect = form.getBoundingClientRect();
      if (rect.width === 0 && rect.height === 0) return;

      const bounds = getVisibleBounds(form);
      const spaceAbove = Math.max(0, rect.top - bounds.top - SLASH_PALETTE_GAP_PX);
      const spaceBelow = Math.max(0, bounds.bottom - rect.bottom - SLASH_PALETTE_GAP_PX);
      const placement: SlashPalettePlacement =
        spaceAbove >= SLASH_PALETTE_MIN_HEIGHT_PX || spaceAbove >= spaceBelow
          ? "above"
          : "below";
      const available = placement === "above" ? spaceAbove : spaceBelow;
      const maxHeight = Math.min(SLASH_PALETTE_MAX_HEIGHT_PX, available);

      setSlashPaletteLayout((current) =>
        current.placement === placement && current.maxHeight === maxHeight
          ? current
          : { placement, maxHeight },
      );
    };

    updateLayout();
    window.addEventListener("resize", updateLayout);
    document.addEventListener("scroll", updateLayout, true);
    return () => {
      window.removeEventListener("resize", updateLayout);
      document.removeEventListener("scroll", updateLayout, true);
    };
  }, [filteredSlashCommands.length, showSlashMenu]);

  const resizeTextarea = useCallback(() => {
    requestAnimationFrame(() => {
      const el = textareaRef.current;
      if (!el) return;
      el.style.height = "auto";
      el.style.height = `${Math.min(el.scrollHeight, 260)}px`;
      el.focus();
    });
  }, []);

  const chooseSlashCommand = useCallback(
    (command: SlashCommand) => {
      setValue(command.argHint ? `${command.command} ` : command.command);
      setSlashMenuDismissed(true);
      setInlineError(null);
      resizeTextarea();
    },
    [resizeTextarea],
  );

  const onKeyDown = (e: ReactKeyboardEvent<HTMLTextAreaElement>) => {
    if (showSlashMenu) {
      if (e.key === "ArrowDown") {
        e.preventDefault();
        setSelectedCommandIndex((idx) => (idx + 1) % filteredSlashCommands.length);
        return;
      }
      if (e.key === "ArrowUp") {
        e.preventDefault();
        setSelectedCommandIndex(
          (idx) => (idx - 1 + filteredSlashCommands.length) % filteredSlashCommands.length,
        );
        return;
      }
      if (e.key === "Tab" || (e.key === "Enter" && !e.shiftKey)) {
        e.preventDefault();
        chooseSlashCommand(filteredSlashCommands[selectedCommandIndex]);
        return;
      }
      if (e.key === "Escape") {
        e.preventDefault();
        setSlashMenuDismissed(true);
        return;
      }
    }
    if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) {
      e.preventDefault();
      submit();
    }
    // Prompt history navigation (only when slash menu is closed)
    if (!showSlashMenu && e.key === "ArrowUp" && e.currentTarget.selectionStart === 0) {
      const prev = promptHistory.navigateUp(value);
      if (prev !== null) {
        e.preventDefault();
        setValue(prev);
      }
    }
    if (!showSlashMenu && e.key === "ArrowDown" && e.currentTarget.selectionStart === value.length) {
      const next = promptHistory.navigateDown();
      if (next !== null) {
        e.preventDefault();
        setValue(next);
      }
    }
  };

  const onInput: React.FormEventHandler<HTMLTextAreaElement> = (e) => {
    const el = e.currentTarget;
    el.style.height = "auto";
    el.style.height = `${Math.min(el.scrollHeight, 260)}px`;
  };

  const onFilePick: React.ChangeEventHandler<HTMLInputElement> = (e) => {
    const files = Array.from(e.target.files ?? []);
    e.target.value = "";
    addFiles(files);
  };

  const removeChip = useCallback(
    (id: string) => {
      const { nextFocusId } = remove(id);
      setInlineError(null);
      requestAnimationFrame(() => {
        const el = nextFocusId ? chipRefs.current.get(nextFocusId) : null;
        if (el) {
          el.focus();
        } else {
          textareaRef.current?.focus();
        }
      });
    },
    [remove],
  );

  const onChipKey = useCallback(
    (id: string) => (e: ReactKeyboardEvent<HTMLButtonElement>) => {
      if (
        e.key === "Delete" ||
        e.key === "Backspace" ||
        e.key === "Enter" ||
        e.key === " "
      ) {
        e.preventDefault();
        removeChip(id);
      }
    },
    [removeChip],
  );

  const attachButtonDisabled = disabled || full;
  const showStopButton = isStreaming && !!onStop;
  const [queuedFlash, setQueuedFlash] = useState(false);

  const submit = useCallback(() => {
    if (!canSend) return;
    const trimmed = value.trim();
    // Bare `/model` opens the picker (parity with the TUI and the model button)
    // rather than sending a status-text command.
    if (trimmed === "/model" && onModelPick) {
      setValue("");
      setSlashMenuDismissed(false);
      setModelPickerOpen(true);
      resizeTextarea();
      return;
    }
    const payload: SendImage[] | undefined =
      readyImages.length > 0
        ? readyImages.map((img) => ({
            media: {
              data_url: img.dataUrl,
              name: img.file.name,
            },
            preview: { url: img.dataUrl, name: img.file.name },
          }))
        : undefined;
    onSend(trimmed, payload);
    promptHistory.addEntry(trimmed);
    promptHistory.reset();
    setValue("");
    setInlineError(null);
    clear();
    setSlashMenuDismissed(false);
    resizeTextarea();
    if (isStreaming) {
      setQueuedFlash(true);
      window.setTimeout(() => setQueuedFlash(false), 2500);
    }
  }, [canSend, clear, isStreaming, onSend, promptHistory, readyImages, resizeTextarea, value]);

  const steer = useCallback(() => {
    const trimmed = value.trim();
    if (!trimmed) return;
    onSend(`[steer] ${trimmed}`);
    setValue("");
    setQueuedFlash(true);
    window.setTimeout(() => setQueuedFlash(false), 2500);
  }, [onSend, value]);

  return (
    <form
      ref={formRef}
      onSubmit={(e) => {
        e.preventDefault();
        submit();
      }}
      onDragEnter={onDragEnter}
      onDragOver={onDragOver}
      onDragLeave={onDragLeave}
      onDrop={onDrop}
      className={cn("relative w-full", isHero ? "px-0" : "px-1 pb-1.5 pt-1 sm:px-0")}
    >
      {showSlashMenu ? (
        <SlashCommandPalette
          commands={filteredSlashCommands}
          selectedIndex={selectedCommandIndex}
          layout={slashPaletteLayout}
          isHero={isHero}
          onHover={setSelectedCommandIndex}
          onChoose={chooseSlashCommand}
        />
      ) : null}
      <div
        className={cn(
          "relative mx-auto flex w-full flex-col overflow-visible transition-all duration-200",
          isHero
            ? "max-w-[58rem] rounded-[28px] border border-black/[0.035] bg-card shadow-[0_20px_55px_rgba(15,23,42,0.08)] dark:border-white/[0.06] dark:shadow-[0_24px_55px_rgba(0,0,0,0.34)]"
            : "max-w-[49.5rem] rounded-[22px] border border-black/[0.035] bg-card shadow-[0_12px_30px_rgba(15,23,42,0.07)] dark:border-white/[0.06] dark:shadow-[0_16px_34px_rgba(0,0,0,0.28)]",
          "focus-within:ring-1 focus-within:ring-foreground/8",
          disabled && "opacity-60",
          isDragging && "ring-2 ring-primary/40 motion-reduce:ring-0 motion-reduce:border-primary",
          goalState?.active &&
            "goal-shell-glow ring-1 ring-sky-400/35 motion-reduce:ring-sky-400/25 dark:ring-sky-400/45",
        )}
      >
        {images.length > 0 ? (
          <div
            className="flex flex-wrap gap-2 px-3 pt-3"
            aria-label={t("thread.composer.attachImage")}
          >
            {images.map((img) => (
              <AttachmentChip
                key={img.id}
                image={img}
                labelRemove={t("thread.composer.remove")}
                labelEncoding={t("thread.composer.encoding")}
                normalizedHint={(orig, current) =>
                  t("thread.composer.normalizedSizeHint", {
                    orig: formatBytes(orig),
                    current: formatBytes(current),
                  })
                }
                formatError={formatRejection}
                onRemove={() => removeChip(img.id)}
                onKeyDown={onChipKey(img.id)}
                registerRef={(el) => {
                  if (el) chipRefs.current.set(img.id, el);
                  else chipRefs.current.delete(img.id);
                }}
              />
            ))}
          </div>
        ) : null}
        {runStartedAt != null
        || goalState?.active
        || goalState?.mode
        || goalState?.pending_question ? (
          <RunElapsedStrip startedAt={runStartedAt} goalState={goalState} />
        ) : null}
        <textarea
          ref={textareaRef}
          value={value}
          onChange={(e) => {
            setValue(e.target.value);
            setSlashMenuDismissed(false);
          }}
          onInput={onInput}
          onKeyDown={onKeyDown}
          onPaste={onPaste}
          rows={1}
          placeholder={resolvedPlaceholder}
          disabled={disabled}
          aria-label={t("thread.composer.inputAria")}
          className={cn(
            "w-full resize-none bg-transparent",
            isHero
              ? "min-h-[78px] px-5 pb-2 pt-5 text-[15px] leading-6"
              : "min-h-[50px] px-4 pb-1.5 pt-3 text-[13.5px] leading-5",
            "placeholder:text-muted-foreground/70",
            "focus:outline-none focus-visible:outline-none",
            "disabled:cursor-not-allowed",
          )}
        />
        {inlineError ? (
          <div
            role="alert"
            className={cn(
              "mx-3 mb-1 rounded-md border border-destructive/40 bg-destructive/8 px-2.5 py-1",
              "text-[11.5px] font-medium text-destructive",
            )}
          >
            {inlineError}
          </div>
        ) : null}
        <div
          className={cn(
            "flex items-center justify-between gap-2",
            isHero ? "px-4 pb-4" : "px-3 pb-2",
          )}
        >
          <div className="flex min-w-0 items-center gap-2">
            <input
              ref={fileInputRef}
              type="file"
              accept={ACCEPT_ATTR}
              multiple
              hidden
              onChange={onFilePick}
            />
            <Button
              type="button"
              size="icon"
              variant="ghost"
              disabled={attachButtonDisabled}
              aria-label={t("thread.composer.attachImage")}
              onClick={() => fileInputRef.current?.click()}
              className={cn(
                "rounded-full text-muted-foreground hover:text-foreground",
                isHero
                  ? "h-9 w-9 border border-border/55 bg-card shadow-[0_2px_8px_rgba(15,23,42,0.05)] hover:bg-card"
                  : "h-7.5 w-7.5 border border-border/55 bg-card shadow-[0_2px_8px_rgba(15,23,42,0.05)] hover:bg-card",
              )}
            >
              <Paperclip className={cn(isHero ? "h-5 w-5" : "h-4 w-4")} />
            </Button>
            {modelLabel ? (
              <div className="relative">
                <button
                  type="button"
                  disabled={!onModelPick}
                  onClick={() => onModelPick && setModelPickerOpen((v) => !v)}
                  title={modelLabel}
                  className={cn(
                    "inline-flex min-w-0 items-center gap-1.5 rounded-full border px-2.5 py-1",
                    "border-foreground/10 bg-foreground/[0.035] font-medium text-foreground/80",
                    isHero
                      ? "max-w-[13rem] text-[12px] shadow-[0_2px_8px_rgba(15,23,42,0.04)]"
                      : "max-w-[10rem] text-[10.5px] shadow-[0_2px_8px_rgba(15,23,42,0.035)]",
                    onModelPick && "cursor-pointer hover:bg-foreground/[0.06] transition-colors",
                  )}
                >
                  <span
                    aria-hidden
                    className="h-1.5 w-1.5 flex-none rounded-full bg-emerald-500/80"
                  />
                  <span className="truncate">{modelLabel}</span>
                </button>
                {onModelPick ? (
                  <ModelPickerPopover
                    open={modelPickerOpen}
                    onClose={() => setModelPickerOpen(false)}
                    onSelect={onModelPick}
                    activeModel={modelLabel}
                  />
                ) : null}
              </div>
            ) : null}
            {onEffortPick && canReason ? (
              <ReasoningEffortPicker
                activeEffort={activeEffort}
                onSelect={onEffortPick}
                disabled={disabled}
              />
            ) : null}
            <span className="hidden select-none text-[10.5px] text-muted-foreground/60 sm:inline">
              {t("thread.composer.sendHint")}
            </span>
          </div>
          <span className={cn(isHero ? "hidden" : "sm:hidden")} aria-hidden />
          {queuedFlash ? (
            <span className="mr-2 inline-flex items-center gap-1 text-[11px] text-muted-foreground">
              ⏳ {t("thread.composer.queued")}
            </span>
          ) : null}
          {showStopButton && canSend ? (
            <Button
              type="button"
              size="icon"
              disabled={disabled}
              aria-label={t("thread.composer.stop")}
              onClick={onStop}
              className="mr-1 rounded-full border border-border/70 bg-card text-foreground/85 shadow-[0_3px_10px_rgba(15,23,42,0.08)] hover:bg-muted/65 hover:text-foreground"
            >
              <Square className="h-2.5 w-2.5 fill-current stroke-current" />
            </Button>
          ) : null}
          {isStreaming && canSend ? (
            <Button
              type="button"
              size="icon"
              disabled={disabled}
              aria-label={t("thread.composer.steer")}
              title={t("thread.composer.steer")}
              onClick={steer}
              className="mr-1 rounded-full border border-border/70 bg-card text-foreground/85 shadow-[0_3px_10px_rgba(15,23,42,0.08)] hover:bg-muted/65 hover:text-foreground"
            >
              <Compass className="h-3.5 w-3.5" />
            </Button>
          ) : null}
          <Button
            type={showStopButton && !canSend ? "button" : "submit"}
            size="icon"
            disabled={showStopButton && !canSend ? disabled : !canSend}
            aria-label={showStopButton && !canSend ? t("thread.composer.stop") : t("thread.composer.send")}
            onClick={showStopButton && !canSend ? onStop : undefined}
            className={cn(
              "rounded-full transition-transform",
              showStopButton && !canSend
                ? "border border-border/70 bg-card text-foreground/85 shadow-[0_3px_10px_rgba(15,23,42,0.08)] hover:bg-muted/65 hover:text-foreground disabled:text-muted-foreground/50"
                : isHero
                  ? "border border-foreground bg-foreground text-background shadow-[0_4px_12px_rgba(15,23,42,0.20)] hover:bg-foreground/90 disabled:border-foreground/35 disabled:bg-foreground/35 disabled:text-background/80"
                  : "border border-foreground bg-foreground text-background shadow-[0_3px_10px_rgba(15,23,42,0.18)] hover:bg-foreground/90 disabled:border-foreground/35 disabled:bg-foreground/35 disabled:text-background/80",
              isHero ? "" : "h-7.5 w-7.5",
              (canSend || (showStopButton && !canSend)) && "hover:scale-[1.03] active:scale-95",
            )}
          >
            {showStopButton && !canSend ? (
              <Square className={cn("fill-current stroke-current", isHero ? "h-3 w-3" : "h-2.5 w-2.5")} />
            ) : (
              <ArrowUp className={cn(isHero ? "h-4.5 w-4.5" : "h-4 w-4")} />
            )}
          </Button>
        </div>
      </div>
    </form>
  );
}

interface SlashCommandPaletteProps {
  commands: SlashCommand[];
  selectedIndex: number;
  layout: SlashPaletteLayout;
  isHero: boolean;
  onHover: (index: number) => void;
  onChoose: (command: SlashCommand) => void;
}

function SlashCommandPalette({
  commands,
  selectedIndex,
  layout,
  isHero,
  onHover,
  onChoose,
}: SlashCommandPaletteProps) {
  const { t } = useTranslation();
  const listMaxHeight = Math.max(
    0,
    layout.maxHeight - SLASH_PALETTE_CHROME_PX,
  );
  // Per-row refs let the listbox keep the keyboard-selected row
  // visible. Mouse hover doesn't need this — the user is already
  // looking at the row they're over — but ↑/↓ keys would otherwise
  // walk off the visible window and the user can't see the cursor.
  const itemRefs = useRef<Array<HTMLButtonElement | null>>([]);
  useEffect(() => {
    const target = itemRefs.current[selectedIndex];
    if (target) {
      target.scrollIntoView({ block: "nearest", inline: "nearest" });
    }
  }, [selectedIndex]);
  return (
    <div
      role="listbox"
      aria-label={t("thread.composer.slash.ariaLabel")}
      style={{ maxHeight: layout.maxHeight }}
      className={cn(
        "absolute left-1/2 z-30 w-[calc(100%-0.5rem)] -translate-x-1/2 overflow-hidden rounded-[18px] border",
        layout.placement === "above" ? "bottom-full mb-2" : "top-full mt-2",
        "border-border/65 bg-popover p-1.5 text-popover-foreground shadow-[0_18px_55px_rgba(15,23,42,0.18)]",
        "dark:border-white/10 dark:shadow-[0_22px_55px_rgba(0,0,0,0.45)]",
        isHero ? "max-w-[58rem]" : "max-w-[49.5rem]",
      )}
    >
      <div className="px-2 pb-1 pt-1 text-[11px] font-medium tracking-[0.08em] text-muted-foreground/70">
        {t("thread.composer.slash.label")}
      </div>
      <div className="overflow-y-auto pr-0.5" style={{ maxHeight: listMaxHeight }}>
        {commands.map((command, index) => {
          const Icon = COMMAND_ICONS[command.icon] ?? CircleHelp;
          const selected = index === selectedIndex;
          const commandKey = slashCommandI18nKey(command.command);
          const title = t(`thread.composer.slash.commands.${commandKey}.title`, {
            defaultValue: command.title,
          });
          const description = t(`thread.composer.slash.commands.${commandKey}.description`, {
            defaultValue: command.description,
          });
          return (
            <button
              key={command.command}
              ref={(el) => {
                itemRefs.current[index] = el;
              }}
              type="button"
              role="option"
              aria-selected={selected}
              onMouseEnter={() => onHover(index)}
              onMouseDown={(e) => {
                e.preventDefault();
                onChoose(command);
              }}
              className={cn(
                "flex w-full items-center gap-3 rounded-[13px] px-3 py-2.5 text-left transition-colors",
                selected
                  ? "bg-primary/10 text-foreground"
                  : "text-foreground/86 hover:bg-accent/55",
              )}
            >
              <span
                className={cn(
                  "flex h-8 w-8 shrink-0 items-center justify-center rounded-[10px] border",
                  selected
                    ? "border-primary/25 bg-primary/12 text-primary"
                    : "border-border/65 bg-muted/45 text-muted-foreground",
                )}
              >
                <Icon className="h-4 w-4" />
              </span>
              <span className="min-w-0 flex-1">
                <span className="flex min-w-0 items-baseline gap-2">
                  <span className="font-mono text-[13px] font-semibold text-foreground">
                    {command.command}
                  </span>
                  {command.argHint ? (
                    <span className="font-mono text-[12px] text-muted-foreground">
                      {command.argHint}
                    </span>
                  ) : null}
                  <span className="truncate text-[13px] font-medium">
                    {title}
                  </span>
                </span>
                <span className="mt-0.5 block truncate text-[12px] text-muted-foreground">
                  {description}
                </span>
              </span>
            </button>
          );
        })}
      </div>
      <div className="flex items-center gap-2 px-2 pt-1.5 text-[10.5px] text-muted-foreground/70">
        <span>{t("thread.composer.slash.navigateHint")}</span>
        <span>{t("thread.composer.slash.selectHint")}</span>
        <span>{t("thread.composer.slash.closeHint")}</span>
      </div>
    </div>
  );
}

interface AttachmentChipProps {
  image: AttachedImage;
  labelRemove: string;
  labelEncoding: string;
  normalizedHint: (origBytes: number, currentBytes: number) => string;
  formatError: (reason: AttachmentError) => string;
  onRemove: () => void;
  onKeyDown: (e: ReactKeyboardEvent<HTMLButtonElement>) => void;
  registerRef: (el: HTMLButtonElement | null) => void;
}

function AttachmentChip({
  image,
  labelRemove,
  labelEncoding,
  normalizedHint,
  formatError,
  onRemove,
  onKeyDown,
  registerRef,
}: AttachmentChipProps) {
  const sizeLabel =
    image.status === "ready" && image.normalized && image.encodedBytes
      ? normalizedHint(image.file.size, image.encodedBytes)
      : formatBytes(image.file.size);
  const tone =
    image.status === "error"
      ? "border-destructive/40 bg-destructive/5 text-destructive"
      : "border-border/70 bg-muted/60";

  return (
    <div
      className={cn(
        "group relative flex items-center gap-2 rounded-[12px] border px-2 py-1.5",
        "transition-colors motion-reduce:transition-none",
        tone,
      )}
      data-testid="composer-chip"
    >
      <div className="relative h-10 w-10 overflow-hidden rounded-md bg-background">
        {image.previewUrl ? (
          <img
            src={image.previewUrl}
            alt=""
            aria-hidden
            loading="eager"
            draggable={false}
            className="h-full w-full object-cover"
          />
        ) : (
          <div className="flex h-full w-full items-center justify-center">
            <ImageIcon className="h-4 w-4 text-muted-foreground" aria-hidden />
          </div>
        )}
        {image.status === "encoding" ? (
          <div
            className="absolute inset-0 flex items-center justify-center bg-background/60"
            aria-label={labelEncoding}
          >
            <Loader2 className="h-4 w-4 animate-spin motion-reduce:animate-none" aria-hidden />
          </div>
        ) : null}
      </div>
      <div className="flex min-w-0 flex-col text-[11.5px] leading-4">
        <span className="truncate max-w-[14rem] font-medium" title={image.file.name}>
          {image.file.name}
        </span>
        <span className="truncate text-muted-foreground">
          {image.status === "error" && image.error
            ? formatError(image.error)
            : sizeLabel}
        </span>
      </div>
      <button
        type="button"
        ref={registerRef}
        onClick={onRemove}
        onKeyDown={onKeyDown}
        aria-label={labelRemove}
        className={cn(
          "ml-1 grid h-5 w-5 flex-none place-items-center rounded-full",
          "text-muted-foreground/80 hover:bg-foreground/8 hover:text-foreground",
          "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-foreground/30",
        )}
      >
        <X className="h-3.5 w-3.5" aria-hidden />
      </button>
    </div>
  );
}

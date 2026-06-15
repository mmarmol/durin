import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useRef,
  useState,
  type ReactNode,
} from "react";

import { CheckCircle2, AlertCircle, Info, AlertTriangle, X } from "lucide-react";

import { cn } from "@/lib/utils";

type ToastLevel = "info" | "success" | "warning" | "error";

interface ToastEntry {
  id: number;
  level: ToastLevel;
  message: string;
  duration: number;
}

interface ToastContextValue {
  toast: (message: string, level?: ToastLevel, duration?: number) => void;
}

const ToastContext = createContext<ToastContextValue | null>(null);

const ICONS: Record<ToastLevel, typeof Info> = {
  info: Info,
  success: CheckCircle2,
  warning: AlertTriangle,
  error: AlertCircle,
};

const COLORS: Record<ToastLevel, string> = {
  info: "border-border/60 text-foreground",
  success: "border-emerald-500/40 text-emerald-600 dark:text-emerald-400",
  warning: "border-amber-500/40 text-amber-600 dark:text-amber-400",
  error: "border-red-500/40 text-red-600 dark:text-red-400",
};

let _nextId = 1;

export function ToastProvider({ children }: { children: ReactNode }) {
  const [toasts, setToasts] = useState<ToastEntry[]>([]);
  const timers = useRef(new Map<number, ReturnType<typeof setTimeout>>());

  const dismiss = useCallback((id: number) => {
    setToasts((prev) => prev.filter((t) => t.id !== id));
    const timer = timers.current.get(id);
    if (timer) {
      clearTimeout(timer);
      timers.current.delete(id);
    }
  }, []);

  const toast = useCallback(
    (message: string, level: ToastLevel = "info", duration = 4000) => {
      const id = _nextId++;
      setToasts((prev) => [...prev, { id, level, message, duration }]);
      const timer = setTimeout(() => dismiss(id), duration);
      timers.current.set(id, timer);
    },
    [dismiss],
  );

  useEffect(() => {
    const map = timers.current;
    return () => {
      map.forEach((t) => clearTimeout(t));
      map.clear();
    };
  }, []);

  return (
    <ToastContext.Provider value={{ toast }}>
      {children}
      <div className="pointer-events-none fixed bottom-4 right-4 z-[100] flex flex-col gap-2">
        {toasts.map((t) => {
          const Icon = ICONS[t.level];
          return (
            <div
              key={t.id}
              role="status"
              className={cn(
                "pointer-events-auto flex items-start gap-2.5 rounded-lg border bg-popover px-3.5 py-2.5 shadow-lg",
                "animate-in fade-in-0 slide-in-from-bottom-2 duration-300",
                COLORS[t.level],
              )}
            >
              <Icon className="mt-0.5 h-4 w-4 flex-none" aria-hidden />
              <p className="min-w-0 flex-1 text-[13px] leading-snug text-popover-foreground">
                {t.message}
              </p>
              <button
                type="button"
                onClick={() => dismiss(t.id)}
                className="flex-none rounded p-0.5 text-muted-foreground/60 hover:text-foreground"
                aria-label="Dismiss"
              >
                <X className="h-3.5 w-3.5" />
              </button>
            </div>
          );
        })}
      </div>
    </ToastContext.Provider>
  );
}

export function useToast(): ToastContextValue {
  const ctx = useContext(ToastContext);
  if (!ctx) return { toast: () => {} };
  return ctx;
}

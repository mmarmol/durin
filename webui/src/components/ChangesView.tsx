import { useCallback, useEffect, useState } from "react";
import { ArrowLeft, RefreshCw, FileText, Loader2 } from "lucide-react";
import { useTranslation } from "react-i18next";

import { Button } from "@/components/ui/button";
import { getChanges, getDiff, type ChangeFile } from "@/lib/api";
import { useClient } from "@/providers/ClientProvider";
import { cn } from "@/lib/utils";

export function ChangesView({
  onBack,
  onToggleSidebar,
  hideSidebarToggleOnDesktop,
}: {
  onBack: () => void;
  onToggleSidebar: () => void;
  hideSidebarToggleOnDesktop?: boolean;
}) {
  const { t } = useTranslation();
  const { token } = useClient();
  const [files, setFiles] = useState<ChangeFile[]>([]);
  const [selected, setSelected] = useState<string | null>(null);
  const [diff, setDiff] = useState<string>("");
  const [loading, setLoading] = useState(true);
  const [diffLoading, setDiffLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const res = await getChanges(token);
      setFiles(res.files ?? []);
      if (res.files?.length > 0 && !selected) {
        setSelected(res.files[0].path);
      }
    } catch {
      setError(t("changes.error"));
    } finally {
      setLoading(false);
    }
  }, [token, t, selected]);

  useEffect(() => {
    refresh();
  }, [refresh]);

  useEffect(() => {
    if (!selected) {
      setDiff("");
      return;
    }
    setDiffLoading(true);
    getDiff(token, selected)
      .then((res) => setDiff(res.diff ?? ""))
      .catch(() => setDiff(""))
      .finally(() => setDiffLoading(false));
  }, [selected, token]);

  return (
    <div className="flex h-full flex-col">
      <header className="flex items-center gap-2 border-b border-border/60 px-3 py-2.5">
        {!hideSidebarToggleOnDesktop ? (
          <Button variant="ghost" size="icon" className="lg:hidden" onClick={onToggleSidebar}>
            <svg viewBox="0 0 24 24" className="h-4 w-4" fill="none" stroke="currentColor" strokeWidth="2">
              <line x1="3" y1="6" x2="21" y2="6" />
              <line x1="3" y1="12" x2="21" y2="12" />
              <line x1="3" y1="18" x2="21" y2="18" />
            </svg>
          </Button>
        ) : null}
        <Button variant="ghost" size="icon" onClick={onBack}>
          <ArrowLeft className="h-4 w-4" />
        </Button>
        <h1 className="flex-1 text-[15px] font-semibold text-foreground">
          {t("changes.title")}
        </h1>
        <Button variant="ghost" size="icon" onClick={refresh} disabled={loading} title={t("changes.refresh")}>
          {loading ? <Loader2 className="h-4 w-4 animate-spin" /> : <RefreshCw className="h-4 w-4" />}
        </Button>
      </header>

      <div className="flex min-h-0 flex-1">
        {/* File list */}
        <div className="w-[240px] shrink-0 overflow-y-auto border-r border-border/40">
          {loading ? (
            <div className="flex items-center justify-center py-8 text-sm text-muted-foreground">
              <Loader2 className="h-4 w-4 animate-spin" />
            </div>
          ) : error ? (
            <div className="px-3 py-4 text-center text-sm text-red-500">{error}</div>
          ) : files.length === 0 ? (
            <div className="px-3 py-4 text-center text-sm text-muted-foreground">
              {t("changes.empty")}
            </div>
          ) : (
            <div className="py-1">
              {files.map((f) => (
                <button
                  key={f.path}
                  type="button"
                  onClick={() => setSelected(f.path)}
                  className={cn(
                    "flex w-full items-center gap-2 px-3 py-1.5 text-left text-[12px] transition-colors",
                    selected === f.path
                      ? "bg-accent/60 font-medium text-accent-foreground"
                      : "text-foreground/80 hover:bg-muted/50",
                  )}
                >
                  <span
                    className={cn(
                      "w-4 flex-none text-center font-mono text-[10px]",
                      f.marker === "M" ? "text-amber-500" : f.marker === "?" ? "text-emerald-500" : "text-muted-foreground",
                    )}
                  >
                    {f.marker === "??" ? "?" : f.marker || "·"}
                  </span>
                  <FileText className="h-3 w-3 flex-none opacity-50" aria-hidden />
                  <span className="min-w-0 flex-1 truncate">{f.path}</span>
                </button>
              ))}
            </div>
          )}
        </div>

        {/* Diff view */}
        <div className="min-w-0 flex-1 overflow-auto bg-card/30">
          {diffLoading ? (
            <div className="flex items-center justify-center py-8 text-sm text-muted-foreground">
              <Loader2 className="h-4 w-4 animate-spin" />
            </div>
          ) : diff ? (
            <pre className="p-3 text-[12px] leading-relaxed">
              <code className="font-mono">
                {diff.split("\n").map((line, i) => (
                  <div
                    key={i}
                    className={cn(
                      line.startsWith("+") && !line.startsWith("+++") && "bg-emerald-500/10 text-emerald-600 dark:text-emerald-400",
                      line.startsWith("-") && !line.startsWith("---") && "bg-red-500/10 text-red-600 dark:text-red-400",
                      line.startsWith("@@") && "text-cyan-600 dark:text-cyan-400",
                      line.startsWith("diff ") && "font-bold text-foreground",
                      line.startsWith("index ") && "text-muted-foreground",
                    )}
                  >
                    {line || " "}
                  </div>
                ))}
              </code>
            </pre>
          ) : (
            <div className="flex h-full items-center justify-center text-sm text-muted-foreground">
              {selected ? t("changes.noDiff") : t("changes.selectFile")}
            </div>
          )}
        </div>
      </div>
    </div>
  );
}

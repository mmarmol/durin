// webui/src/components/rich/RichBlock.tsx
import { Suspense, lazy, useCallback, useRef, useState } from "react";
import * as Dialog from "@radix-ui/react-dialog";
import { Check, Code2, Copy, Download, Eye, Maximize2, X } from "lucide-react";
import { useTranslation } from "react-i18next";

import { CodeBlock } from "@/components/CodeBlock";
import { SandboxFrame } from "@/components/rich/SandboxFrame";
import { ZoomInspector } from "@/components/rich/ZoomInspector";
import { downloadBlob } from "@/components/rich/download";
import { richKind } from "@/components/rich/rich-languages";
import { cn } from "@/lib/utils";

const MermaidPreview = lazy(() => import("@/components/rich/MermaidPreview"));
const ChartPreview = lazy(() => import("@/components/rich/ChartPreview"));

const FALLBACK = <div className="p-4 text-sm text-muted-foreground">…</div>;

export function RichBlock({ language, code }: { language: string; code: string }) {
  const { t } = useTranslation();
  const kind = richKind(language);
  const [mode, setMode] = useState<"preview" | "code">("preview");
  const [copied, setCopied] = useState(false);
  const [expanded, setExpanded] = useState(false);
  const svgRef = useRef<string | null>(null);
  const rememberSvg = useCallback((s: string) => { svgRef.current = s; }, []);

  const onCopy = useCallback(() => {
    if (!navigator.clipboard) return;
    navigator.clipboard.writeText(code).then(() => {
      setCopied(true);
      setTimeout(() => setCopied(false), 1_500);
    });
  }, [code]);

  if (!kind) return <CodeBlock language={language} code={code} className="my-3" />;

  const isSvgKind = kind === "mermaid" || kind === "chart";

  const onDownload = () => {
    if (isSvgKind) {
      if (svgRef.current) {
        downloadBlob(kind === "mermaid" ? "diagram.svg" : "chart.svg", "image/svg+xml", svgRef.current);
      }
    } else {
      const svg = kind === "svg";
      downloadBlob(svg ? "snippet.svg" : "snippet.html", svg ? "image/svg+xml" : "text/html", code);
    }
  };

  const previewNode =
    kind === "html" ? (
      <SandboxFrame html={code} title="rich-html" />
    ) : kind === "svg" ? (
      <SandboxFrame html={code} title="rich-svg" />
    ) : kind === "mermaid" ? (
      <Suspense fallback={FALLBACK}>
        <MermaidPreview code={code} onRendered={rememberSvg} />
      </Suspense>
    ) : (
      <Suspense fallback={FALLBACK}>
        <ChartPreview code={code} onRendered={rememberSvg} />
      </Suspense>
    );

  const iconBtn = "inline-flex items-center gap-1 rounded px-1.5 py-0.5 hover:bg-muted";

  return (
    <Dialog.Root open={expanded} onOpenChange={setExpanded}>
      <div className="my-3 overflow-hidden rounded-lg border border-border/60">
        <div className="flex items-center justify-between bg-muted/40 px-3 py-1.5 text-xs">
          <span className="font-mono lowercase text-muted-foreground">{kind}</span>
          <div className="flex items-center gap-1">
            <button
              type="button"
              onClick={() => setMode(mode === "preview" ? "code" : "preview")}
              className={iconBtn}
              aria-label={mode === "preview" ? t("rich.code") : t("rich.preview")}
            >
              {mode === "preview" ? <Code2 className="h-3.5 w-3.5" /> : <Eye className="h-3.5 w-3.5" />}
              {mode === "preview" ? t("rich.code") : t("rich.preview")}
            </button>
            <button type="button" onClick={onDownload} className={iconBtn} aria-label={t("rich.download")}>
              <Download className="h-3.5 w-3.5" />
            </button>
            <Dialog.Trigger asChild>
              <button type="button" className={iconBtn} aria-label={t("rich.expand")}>
                <Maximize2 className="h-3.5 w-3.5" />
              </button>
            </Dialog.Trigger>
            <button type="button" onClick={onCopy} className={iconBtn} aria-label={t("rich.copy")}>
              {copied ? <Check className="h-3.5 w-3.5" /> : <Copy className="h-3.5 w-3.5" />}
            </button>
          </div>
        </div>
        {mode === "preview" ? (
          isSvgKind ? (
            <div className="overflow-auto bg-white" style={{ maxHeight: 240 }}>
              {previewNode}
            </div>
          ) : (
            previewNode
          )
        ) : (
          <CodeBlock language={language} code={code} bare className={cn("rounded-none border-0")} />
        )}
      </div>
      <Dialog.Portal>
        <Dialog.Overlay className="fixed inset-0 z-50 bg-black/60" />
        <Dialog.Content className="fixed inset-0 z-50 bg-background">
          <Dialog.Title className="sr-only">{t("rich.expand")}</Dialog.Title>
          {isSvgKind ? (
            <ZoomInspector onDownload={onDownload} onClose={() => setExpanded(false)}>
              <div className="bg-white">{previewNode}</div>
            </ZoomInspector>
          ) : (
            <div className="relative h-full w-full bg-white">
              <div className="h-full w-full overflow-auto">
                <SandboxFrame html={code} title={kind === "svg" ? "rich-svg" : "rich-html"} fill />
              </div>
              <div className="absolute right-3 top-3 flex items-center gap-1">
                <button
                  type="button"
                  onClick={onDownload}
                  className="inline-flex h-9 w-9 items-center justify-center rounded-full border border-border bg-background shadow-lg hover:bg-muted"
                  aria-label={t("rich.download")}
                >
                  <Download className="h-4 w-4" />
                </button>
                <button
                  type="button"
                  onClick={() => setExpanded(false)}
                  className="inline-flex h-9 w-9 items-center justify-center rounded-full border border-border bg-background shadow-lg hover:bg-muted"
                  aria-label={t("rich.close")}
                >
                  <X className="h-4 w-4" />
                </button>
              </div>
            </div>
          )}
        </Dialog.Content>
      </Dialog.Portal>
    </Dialog.Root>
  );
}

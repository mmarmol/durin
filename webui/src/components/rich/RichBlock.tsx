// webui/src/components/rich/RichBlock.tsx
import { Suspense, lazy, useCallback, useState } from "react";
import { Check, Code2, Copy, Eye } from "lucide-react";
import { useTranslation } from "react-i18next";

import { CodeBlock } from "@/components/CodeBlock";
import { SandboxFrame } from "@/components/rich/SandboxFrame";
import { richKind } from "@/components/rich/rich-languages";
import { cn } from "@/lib/utils";

const MermaidPreview = lazy(() => import("@/components/rich/MermaidPreview"));
const ChartPreview = lazy(() => import("@/components/rich/ChartPreview"));

export function RichBlock({ language, code }: { language: string; code: string }) {
  const { t } = useTranslation();
  const kind = richKind(language);
  const [mode, setMode] = useState<"preview" | "code">("preview");
  const [copied, setCopied] = useState(false);

  const onCopy = useCallback(() => {
    if (!navigator.clipboard) return;
    navigator.clipboard.writeText(code).then(() => {
      setCopied(true);
      setTimeout(() => setCopied(false), 1_500);
    });
  }, [code]);

  if (!kind) return <CodeBlock language={language} code={code} className="my-3" />;

  const preview =
    kind === "html" ? (
      <SandboxFrame html={code} title="rich-html" />
    ) : kind === "svg" ? (
      <SandboxFrame html={code} title="rich-svg" />
    ) : kind === "mermaid" ? (
      <Suspense fallback={<div className="p-4 text-sm text-muted-foreground">…</div>}>
        <MermaidPreview code={code} />
      </Suspense>
    ) : (
      <Suspense fallback={<div className="p-4 text-sm text-muted-foreground">…</div>}>
        <ChartPreview code={code} />
      </Suspense>
    );

  return (
    <div className="my-3 overflow-hidden rounded-lg border border-border/60">
      <div className="flex items-center justify-between bg-muted/40 px-3 py-1.5 text-xs">
        <span className="font-mono lowercase text-muted-foreground">{kind}</span>
        <div className="flex items-center gap-1">
          <button
            type="button"
            onClick={() => setMode(mode === "preview" ? "code" : "preview")}
            className="inline-flex items-center gap-1 rounded px-1.5 py-0.5 hover:bg-muted"
            aria-label={mode === "preview" ? t("rich.code") : t("rich.preview")}
          >
            {mode === "preview" ? <Code2 className="h-3.5 w-3.5" /> : <Eye className="h-3.5 w-3.5" />}
            {mode === "preview" ? t("rich.code") : t("rich.preview")}
          </button>
          <button
            type="button"
            onClick={onCopy}
            className="inline-flex items-center gap-1 rounded px-1.5 py-0.5 hover:bg-muted"
            aria-label={t("rich.copy")}
          >
            {copied ? <Check className="h-3.5 w-3.5" /> : <Copy className="h-3.5 w-3.5" />}
          </button>
        </div>
      </div>
      {mode === "preview" ? (
        preview
      ) : (
        <CodeBlock language={language} code={code} className={cn("rounded-none border-0")} />
      )}
    </div>
  );
}

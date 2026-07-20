// webui/src/components/rich/ZoomInspector.tsx
import type { ReactNode } from "react";
import { useCallback, useEffect, useRef, useState } from "react";
import { Download, Maximize, Minus, Plus, X } from "lucide-react";
import { useTranslation } from "react-i18next";

import { type Transform, fitTransform, zoomToward } from "@/components/rich/zoom-math";

export function ZoomInspector({
  children,
  onDownload,
  onClose,
}: {
  children: ReactNode;
  onDownload: () => void;
  onClose: () => void;
}) {
  const { t } = useTranslation();
  const viewRef = useRef<HTMLDivElement>(null);
  const layerRef = useRef<HTMLDivElement>(null);
  const drag = useRef<{ x: number; y: number } | null>(null);
  const [tr, setTr] = useState<Transform>({ scale: 1, tx: 0, ty: 0 });

  const fit = useCallback(() => {
    const view = viewRef.current;
    const svg = layerRef.current?.querySelector("svg");
    if (!view || !svg) return;
    const vr = view.getBoundingClientRect();
    const sr = svg.getBoundingClientRect();
    setTr((cur) => fitTransform(sr.width / cur.scale, sr.height / cur.scale, vr.width, vr.height));
  }, []);

  const zoomCenter = useCallback((factor: number) => {
    const vr = viewRef.current?.getBoundingClientRect();
    if (!vr) return;
    setTr((cur) => zoomToward(cur, vr.width / 2, vr.height / 2, factor));
  }, []);

  // Fit to the viewport once, after first paint.
  useEffect(() => {
    const id = requestAnimationFrame(fit);
    return () => cancelAnimationFrame(id);
  }, [fit]);

  // Non-passive wheel listener so we can preventDefault and zoom toward the cursor.
  useEffect(() => {
    const view = viewRef.current;
    if (!view) return;
    const onWheel = (e: WheelEvent) => {
      e.preventDefault();
      const vr = view.getBoundingClientRect();
      setTr((cur) => zoomToward(cur, e.clientX - vr.left, e.clientY - vr.top, e.deltaY < 0 ? 1.1 : 0.9));
    };
    view.addEventListener("wheel", onWheel, { passive: false });
    return () => view.removeEventListener("wheel", onWheel);
  }, []);

  // Keyboard: +/- zoom, 0 fit. (Esc is handled by the enclosing Radix Dialog.)
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "+" || e.key === "=") { e.preventDefault(); zoomCenter(1.25); }
      else if (e.key === "-") { e.preventDefault(); zoomCenter(0.8); }
      else if (e.key === "0") { e.preventDefault(); fit(); }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [zoomCenter, fit]);

  const onPointerDown = (e: React.PointerEvent) => {
    drag.current = { x: e.clientX, y: e.clientY };
    (e.currentTarget as Element).setPointerCapture?.(e.pointerId);
  };
  const onPointerMove = (e: React.PointerEvent) => {
    if (!drag.current) return;
    const dx = e.clientX - drag.current.x;
    const dy = e.clientY - drag.current.y;
    drag.current = { x: e.clientX, y: e.clientY };
    setTr((cur) => ({ ...cur, tx: cur.tx + dx, ty: cur.ty + dy }));
  };
  const onPointerUp = () => { drag.current = null; };

  const btn = "inline-flex h-8 w-8 items-center justify-center rounded-full text-foreground hover:bg-muted";

  return (
    <div className="relative h-full w-full">
      <div
        ref={viewRef}
        className="absolute inset-0 cursor-grab overflow-hidden"
        style={{ touchAction: "none" }}
        onPointerDown={onPointerDown}
        onPointerMove={onPointerMove}
        onPointerUp={onPointerUp}
        onPointerCancel={onPointerUp}
      >
        <div
          ref={layerRef}
          style={{ transform: `translate(${tr.tx}px, ${tr.ty}px) scale(${tr.scale})`, transformOrigin: "0 0", width: "max-content" }}
        >
          {children}
        </div>
      </div>

      <div className="absolute left-1/2 top-3 flex -translate-x-1/2 items-center gap-1 rounded-full border border-border bg-background p-1 shadow-lg">
        <button type="button" className={btn} aria-label={t("rich.zoomOut")} onClick={() => zoomCenter(0.8)}>
          <Minus className="h-4 w-4" />
        </button>
        <span className="min-w-[3rem] text-center text-xs text-muted-foreground">{Math.round(tr.scale * 100)}%</span>
        <button type="button" className={btn} aria-label={t("rich.zoomIn")} onClick={() => zoomCenter(1.25)}>
          <Plus className="h-4 w-4" />
        </button>
        <span className="mx-1 h-5 w-px bg-border" />
        <button type="button" className={btn} aria-label={t("rich.fit")} onClick={fit}>
          <Maximize className="h-4 w-4" />
        </button>
        <button type="button" className={btn} aria-label={t("rich.download")} onClick={onDownload}>
          <Download className="h-4 w-4" />
        </button>
      </div>

      <button
        type="button"
        className="absolute right-3 top-3 inline-flex h-9 w-9 items-center justify-center rounded-full border border-border bg-background shadow-lg hover:bg-muted"
        aria-label={t("rich.close")}
        onClick={onClose}
      >
        <X className="h-4 w-4" />
      </button>

      <div className="pointer-events-none absolute bottom-3 left-1/2 -translate-x-1/2 rounded-full border border-border bg-background px-3 py-1 text-xs text-muted-foreground">
        {t("rich.panZoomHint")}
      </div>
    </div>
  );
}

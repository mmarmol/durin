export const MIN_SCALE = 0.15;
export const MAX_SCALE = 8;

export interface Transform {
  scale: number;
  tx: number;
  ty: number;
}

export function clampScale(s: number): number {
  return Math.max(MIN_SCALE, Math.min(MAX_SCALE, s));
}

/** Scale to contain content in the viewport (with padding) and center it. */
export function fitTransform(cw: number, ch: number, vw: number, vh: number, pad = 0.92): Transform {
  if (cw <= 0 || ch <= 0 || vw <= 0 || vh <= 0) return { scale: 1, tx: 0, ty: 0 };
  const scale = clampScale(Math.min(vw / cw, vh / ch) * pad);
  return { scale, tx: (vw - cw * scale) / 2, ty: (vh - ch * scale) / 2 };
}

/** Zoom by `factor` around viewport point (cx, cy), keeping that point fixed. */
export function zoomToward(tr: Transform, cx: number, cy: number, factor: number): Transform {
  const scale = clampScale(tr.scale * factor);
  return {
    scale,
    tx: cx - ((cx - tr.tx) / tr.scale) * scale,
    ty: cy - ((cy - tr.ty) / tr.scale) * scale,
  };
}

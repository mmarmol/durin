// Stub: the Deliberation feature was pruned from durin (see
// post_prune_state May 2026 — smart layer refuted V3-V8). The webui
// still references the panel in a few places; this no-op component
// keeps the bundle compiling without resurrecting the smart layer.
// Safe to delete once those callsites are cleaned up.

import * as React from "react";

// `any` on purpose: the original `DeliberationResultData` type lives in
// `lib/types` with a different shape and we don't want to maintain two
// copies. Since this component renders nothing, a permissive prop type
// is the safest bridge.
export interface DeliberationPanelProps {
  data: any; // eslint-disable-line @typescript-eslint/no-explicit-any
}

export const DeliberationPanel: React.FC<DeliberationPanelProps> = () => null;

export default DeliberationPanel;

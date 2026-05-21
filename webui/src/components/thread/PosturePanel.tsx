// Stub: the Posture feature was pruned from durin (see
// post_prune_state May 2026 — smart layer refuted V3-V8). Renders
// nothing so the existing callsites compile without resurrecting
// the smart layer. Safe to delete once those callsites are removed.

import * as React from "react";

// `any` on purpose: the real `PostureUpdateData` lives in `lib/types`
// and we don't maintain two copies. Renders nothing.
export interface PosturePanelProps {
  data: any; // eslint-disable-line @typescript-eslint/no-explicit-any
}

export const PosturePanel: React.FC<PosturePanelProps> = () => null;

export default PosturePanel;

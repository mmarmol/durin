import { useEffect, useState } from "react";

import { useClient } from "@/providers/ClientProvider";
import { listModes, type ModeInfo } from "@/lib/api";

/** Fetch the registered agent modes once (built-ins plus any custom modes).
 *  On failure the list stays empty, which hides the picker — the conversation
 *  still works, it just falls back to the default mode. */
export function useModes(): ModeInfo[] {
  const { token } = useClient();
  const [modes, setModes] = useState<ModeInfo[]>([]);
  useEffect(() => {
    let cancelled = false;
    void listModes(token)
      .then((m) => {
        if (!cancelled) setModes(m);
      })
      .catch(() => {
        /* leave empty */
      });
    return () => {
      cancelled = true;
    };
  }, [token]);
  return modes;
}

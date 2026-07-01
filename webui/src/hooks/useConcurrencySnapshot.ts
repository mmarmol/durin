import { useEffect, useState } from "react";
import { useClient } from "@/providers/ClientProvider";
import type { ConcurrencySnapshot } from "@/lib/types";

/** Latest gateway-wide concurrency snapshot, or null until the first frame.
 * The gateway pushes a coalesced snapshot on turn/subagent boundaries and
 * hydrates one on (re)subscribe, so this settles within one turn boundary of
 * mounting. */
export function useConcurrencySnapshot(): ConcurrencySnapshot | null {
  const { client } = useClient();
  const [snapshot, setSnapshot] = useState<ConcurrencySnapshot | null>(null);

  useEffect(() => {
    const unsub = client.onConcurrencySnapshot((ev) => {
      const { event: _event, ...snap } = ev;
      setSnapshot(snap);
    });
    return unsub;
  }, [client]);

  return snapshot;
}

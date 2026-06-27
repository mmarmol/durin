import { createContext, useContext } from "react";

/** Actions a deeply-nested message component can take on the active
 *  thread without prop-drilling through the viewport → list → bubble
 *  chain: answering an `ask_user_question`, satisfying a
 *  `request_secret`, opening the work panel. */
export interface ThreadActions {
  /** Submit `text` as the user's next message in the current thread. */
  sendUserMessage: (text: string) => void;
  /** Store a credential the agent requested. The chat is bound to this
   *  thread; the value never enters the conversation. Resolves on the
   *  server ack. */
  storeSecret: (input: {
    name: string;
    service: string;
    value: string;
    scope?: string[];
  }) => Promise<void>;
  /** Open the side work panel (workflow / sub-agent detail). */
  openWorkPanel?: () => void;
}

const ThreadActionsContext = createContext<ThreadActions | null>(null);

export const ThreadActionsProvider = ThreadActionsContext.Provider;

/** Thread actions, or null when rendered outside a thread (e.g. tests). */
export function useThreadActions(): ThreadActions | null {
  return useContext(ThreadActionsContext);
}

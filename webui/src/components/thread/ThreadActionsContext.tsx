import { createContext, useContext } from "react";

/** Actions a deeply-nested message component can take on the active
 *  thread without prop-drilling through the viewport → list → bubble
 *  chain. Currently just answering an `ask_user_question` inline. */
export interface ThreadActions {
  /** Submit `text` as the user's next message in the current thread. */
  sendUserMessage: (text: string) => void;
}

const ThreadActionsContext = createContext<ThreadActions | null>(null);

export const ThreadActionsProvider = ThreadActionsContext.Provider;

/** Thread actions, or null when rendered outside a thread (e.g. tests). */
export function useThreadActions(): ThreadActions | null {
  return useContext(ThreadActionsContext);
}

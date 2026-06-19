import { createContext, useContext, type ReactNode } from "react";

import type { DurinClient } from "@/lib/durin-client";

interface ClientContextValue {
  client: DurinClient;
  token: string;
  modelName: string | null;
  // Active preset name (e.g. "default", "glm-5.2", "default:high"). The effort
  // suffix is what the composer's reasoning-effort picker reflects.
  modelPreset: string | null;
}

const ClientContext = createContext<ClientContextValue | null>(null);

export function ClientProvider({
  client,
  token,
  modelName = null,
  modelPreset = null,
  children,
}: {
  client: DurinClient;
  token: string;
  modelName?: string | null;
  modelPreset?: string | null;
  children: ReactNode;
}) {
  return (
    <ClientContext.Provider value={{ client, token, modelName, modelPreset }}>
      {children}
    </ClientContext.Provider>
  );
}

export function useClient(): ClientContextValue {
  const ctx = useContext(ClientContext);
  if (!ctx) {
    throw new Error("useClient must be used within a ClientProvider");
  }
  return ctx;
}

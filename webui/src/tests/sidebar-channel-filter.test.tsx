import { fireEvent, render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { Sidebar } from "@/components/Sidebar";
import type { ChatSummary } from "@/lib/types";

vi.mock("@/components/ConnectionBadge", () => ({
  ConnectionBadge: () => null,
}));

const SESSIONS: ChatSummary[] = [
  {
    key: "websocket:web-1",
    channel: "websocket",
    chatId: "web-1",
    createdAt: "2026-08-03T10:00:00Z",
    updatedAt: "2026-08-03T10:00:00Z",
    title: "",
    preview: "chat desde la webui",
  },
  {
    key: "slack:C0AKE2P92F7",
    channel: "slack",
    chatId: "C0AKE2P92F7",
    createdAt: "2026-07-27T13:45:46Z",
    updatedAt: "2026-08-03T17:18:53Z",
    title: "",
    preview: "analiza el ticket 23087",
  },
];

function renderSidebar() {
  return render(
    <Sidebar
      sessions={SESSIONS}
      activeKey={null}
      loading={false}
      onNewChat={() => {}}
      onSelect={() => {}}
      onRequestDelete={() => {}}
      onRequestRename={async () => {}}
      onOpenSettings={() => {}}
      onCollapse={() => {}}
    />,
  );
}

describe("sidebar channel filter", () => {
  beforeEach(() => {
    localStorage.clear();
  });

  it("hides non-web chats by default", () => {
    renderSidebar();
    expect(screen.getByText("chat desde la webui")).toBeTruthy();
    expect(screen.queryByText("analiza el ticket 23087")).toBeNull();
  });

  it("remembers the filter across a remount", () => {
    const first = renderSidebar();
    // The two filter chips are "web" and "all"; pick the one that is not active.
    fireEvent.click(screen.getByText(/all/i));
    expect(screen.getByText("analiza el ticket 23087")).toBeTruthy();
    first.unmount();

    // A fresh mount stands in for a page reload: the Slack row must survive it,
    // otherwise every channel conversation silently disappears again.
    renderSidebar();
    expect(screen.getByText("analiza el ticket 23087")).toBeTruthy();
  });
});

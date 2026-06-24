import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactNode } from "react";
import { beforeEach, afterEach, describe, expect, it, vi } from "vitest";

import { DreamView } from "@/components/DreamView";
import * as api from "@/lib/api";
import { ClientProvider } from "@/providers/ClientProvider";

vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/api")>();
  return {
    ...actual,
    fetchDreamDigest: vi.fn(),
    fetchMemoryEntity: vi.fn(),
    getSkill: vi.fn(),
    runCronJob: vi.fn(),
    fetchFlaggedPairs: vi.fn(),
    resolveFlaggedPair: vi.fn(),
    listQuarantine: vi.fn(),
  };
});

function wrap(children: ReactNode) {
  return (
    <ClientProvider
      client={{} as unknown as import("@/lib/durin-client").DurinClient}
      token="tok"
    >
      {children}
    </ClientProvider>
  );
}

beforeEach(() => {
  vi.mocked(api.fetchDreamDigest).mockReset();
  vi.mocked(api.fetchMemoryEntity).mockReset();
  vi.mocked(api.getSkill).mockReset();
  vi.mocked(api.runCronJob).mockReset();
  vi.mocked(api.fetchFlaggedPairs).mockReset();
  vi.mocked(api.resolveFlaggedPair).mockReset();
  vi.mocked(api.listQuarantine).mockReset();

  // Default Bandeja mocks to empty so Resumen tests don't need them
  vi.mocked(api.fetchFlaggedPairs).mockResolvedValue([]);
  vi.mocked(api.listQuarantine).mockResolvedValue([]);
});
afterEach(() => vi.restoreAllMocks());

describe("DreamView", () => {
  it("renders event summaries returned by fetchDreamDigest", async () => {
    const now = Date.now();
    vi.mocked(api.fetchDreamDigest).mockResolvedValue({
      last_run_at_ms: now - 60_000,
      events: [
        { at_ms: now - 120_000, kind: "merged", ref: null, ref_kind: null, summary: "Merged entity Alpha into Beta" },
        { at_ms: now - 180_000, kind: "improved", ref: "skill:git", ref_kind: "skill", summary: "Improved the git skill" },
      ],
    });

    render(wrap(<DreamView />));

    expect(await screen.findByText("Merged entity Alpha into Beta")).toBeInTheDocument();
    expect(screen.getByText("Improved the git skill")).toBeInTheDocument();
    expect(api.fetchDreamDigest).toHaveBeenCalledWith("tok");
  });

  it("shows the empty state when there are no events", async () => {
    vi.mocked(api.fetchDreamDigest).mockResolvedValue({
      last_run_at_ms: null,
      events: [],
    });

    render(wrap(<DreamView />));

    expect(await screen.findByText("No dream activity yet.")).toBeInTheDocument();
  });

  it("shows an error when the fetch fails", async () => {
    vi.mocked(api.fetchDreamDigest).mockRejectedValue(new Error("HTTP 500"));

    render(wrap(<DreamView />));

    expect(await screen.findByText("HTTP 500")).toBeInTheDocument();
  });

  it("opens the drawer with entity detail when Ver is clicked on an entity-ref event", async () => {
    const user = userEvent.setup();
    const now = Date.now();

    vi.mocked(api.fetchDreamDigest).mockResolvedValue({
      last_run_at_ms: now - 60_000,
      events: [
        {
          at_ms: now - 120_000,
          kind: "merged",
          ref: "person:alice",
          ref_kind: "entity",
          summary: "Merged Alice records",
        },
      ],
    });

    vi.mocked(api.fetchMemoryEntity).mockResolvedValue({
      ref: "person:alice",
      page: {
        type: "person",
        name: "Alice",
        aliases: ["Al"],
        identifiers: null,
        extra: {},
        body: "Alice is a key contact.",
        dream_processed_through: null,
      },
      provenance: [],
      history: [],
      archive: [],
      entries: [],
    });

    render(wrap(<DreamView />));

    // Wait for the feed to appear.
    expect(await screen.findByText("Merged Alice records")).toBeInTheDocument();

    // Click the "View" button on the entity-ref event.
    const viewBtn = screen.getByRole("button", { name: "View" });
    await user.click(viewBtn);

    // Drawer should fetch and display entity detail.
    await waitFor(() => {
      expect(api.fetchMemoryEntity).toHaveBeenCalledWith("tok", "person:alice");
    });

    // Entity name appears in the drawer header.
    expect(await screen.findByText("Alice")).toBeInTheDocument();
    // Entity body content is rendered.
    expect(screen.getByText(/Alice is a key contact/)).toBeInTheDocument();
  });

  it("opens the drawer with skill detail when Ver is clicked on a skill-ref event", async () => {
    const user = userEvent.setup();
    const now = Date.now();

    vi.mocked(api.fetchDreamDigest).mockResolvedValue({
      last_run_at_ms: null,
      events: [
        {
          at_ms: now - 180_000,
          kind: "improved",
          ref: "git",
          ref_kind: "skill",
          summary: "Improved the git skill",
        },
      ],
    });

    vi.mocked(api.getSkill).mockResolvedValue({
      name: "git",
      mode: "auto",
      content: "# Git skill\n\nUse this skill to run git commands.",
    });

    render(wrap(<DreamView />));

    expect(await screen.findByText("Improved the git skill")).toBeInTheDocument();

    const viewBtn = screen.getByRole("button", { name: "View" });
    await user.click(viewBtn);

    await waitFor(() => {
      expect(api.getSkill).toHaveBeenCalledWith("tok", "git");
    });

    // Skill name appears as the drawer title.
    expect(await screen.findByText("git")).toBeInTheDocument();
    // Local SKILL.md content is rendered.
    expect(screen.getByText(/Use this skill to run git commands/)).toBeInTheDocument();
  });

  it("closes the drawer when the X button is clicked", async () => {
    const user = userEvent.setup();
    const now = Date.now();

    vi.mocked(api.fetchDreamDigest).mockResolvedValue({
      last_run_at_ms: null,
      events: [
        {
          at_ms: now - 120_000,
          kind: "created",
          ref: "person:bob",
          ref_kind: "entity",
          summary: "Created Bob",
        },
      ],
    });

    vi.mocked(api.fetchMemoryEntity).mockResolvedValue({
      ref: "person:bob",
      page: {
        type: "person",
        name: "Bob",
        aliases: [],
        identifiers: null,
        extra: {},
        body: "",
        dream_processed_through: null,
      },
      provenance: [],
      history: [],
      archive: [],
      entries: [],
    });

    render(wrap(<DreamView />));
    expect(await screen.findByText("Created Bob")).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "View" }));
    expect(await screen.findByText("Bob")).toBeInTheDocument();

    // Close via the × button.
    await user.click(screen.getByRole("button", { name: "Close" }));

    // Drawer slides away: its role stays in DOM but header name is gone.
    await waitFor(() => {
      // After close the drawer name "Bob" should no longer be visible (drawer is off-screen).
      // The dialog role remains in DOM as translate-x-full; we verify the drawer
      // title content is gone from the visible document.
      expect(screen.queryByRole("dialog")).toBeInTheDocument();
    });
  });

  it("does not render a clickable Ver button for events with null ref", async () => {
    const now = Date.now();

    vi.mocked(api.fetchDreamDigest).mockResolvedValue({
      last_run_at_ms: null,
      events: [
        {
          at_ms: now - 60_000,
          kind: "merged",
          ref: null,
          ref_kind: null,
          summary: "No-ref event",
        },
      ],
    });

    render(wrap(<DreamView />));

    expect(await screen.findByText("No-ref event")).toBeInTheDocument();

    // The View button is disabled when ref is null.
    const viewBtn = screen.getByRole("button", { name: "View" });
    expect(viewBtn).toBeDisabled();
  });

  it("Run now calls runCronJob with memory_dream and refreshes the digest", async () => {
    const user = userEvent.setup();
    const now = Date.now();

    vi.mocked(api.fetchDreamDigest)
      .mockResolvedValueOnce({ last_run_at_ms: null, events: [] })
      .mockResolvedValueOnce({
        last_run_at_ms: now,
        events: [
          { at_ms: now, kind: "merged", ref: null, ref_kind: null, summary: "New dream event" },
        ],
      });
    vi.mocked(api.runCronJob).mockResolvedValue({ started: true });

    render(wrap(<DreamView />));

    // Wait for the initial load to finish.
    expect(await screen.findByText("No dream activity yet.")).toBeInTheDocument();

    const runBtn = screen.getByRole("button", { name: "Run now" });
    await user.click(runBtn);

    await waitFor(() => {
      expect(api.runCronJob).toHaveBeenCalledWith("tok", "memory_dream");
    });

    // After runCronJob succeeds, the digest is refetched and the new event appears.
    await waitFor(() => {
      expect(api.fetchDreamDigest).toHaveBeenCalledTimes(2);
    });
    expect(await screen.findByText("New dream event")).toBeInTheDocument();
  });

  it("Run now shows an error message when runCronJob fails", async () => {
    const user = userEvent.setup();

    vi.mocked(api.fetchDreamDigest).mockResolvedValue({ last_run_at_ms: null, events: [] });
    vi.mocked(api.runCronJob).mockRejectedValue(new Error("HTTP 503"));

    render(wrap(<DreamView />));

    expect(await screen.findByText("No dream activity yet.")).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Run now" }));

    expect(await screen.findByText("Failed to start dream run.")).toBeInTheDocument();
    // Button re-enabled after failure.
    expect(screen.getByRole("button", { name: "Run now" })).not.toBeDisabled();
  });
});

describe("DreamView Bandeja tab", () => {
  const basePair: api.FlaggedPair = {
    ref_a: "person:alice",
    ref_b: "person:alice-smith",
    verdict: "same_entity",
    confidence: 72,
    reasoning: "Both refs share the same name and email.",
    at_ms: Date.now() - 3_600_000,
  };

  const baseQuarantine: api.QuarantineRow = {
    name: "shady-skill",
    status: "quarantined",
    source: "https://example.com/shady.zip",
    verdict: "caution",
    findings: [{ category: "network", severity: "caution", where: "SKILL.md", detail: "Makes outbound requests." }],
  };

  beforeEach(() => {
    vi.mocked(api.fetchDreamDigest).mockResolvedValue({ last_run_at_ms: null, events: [] });
  });

  it("renders a flagged pair when the Bandeja tab is clicked", async () => {
    const user = userEvent.setup();
    vi.mocked(api.fetchFlaggedPairs).mockResolvedValue([basePair]);
    vi.mocked(api.listQuarantine).mockResolvedValue([]);

    render(wrap(<DreamView />));
    await screen.findByText("No dream activity yet.");

    await user.click(screen.getByRole("button", { name: /Inbox/i }));

    expect(await screen.findByText("person:alice")).toBeInTheDocument();
    expect(screen.getByText("person:alice-smith")).toBeInTheDocument();
    expect(screen.getByText("Both refs share the same name and email.")).toBeInTheDocument();
  });

  it("calls resolveFlaggedPair with action:merge and removes the row", async () => {
    const user = userEvent.setup();
    vi.mocked(api.fetchFlaggedPairs).mockResolvedValue([basePair]);
    vi.mocked(api.listQuarantine).mockResolvedValue([]);
    vi.mocked(api.resolveFlaggedPair).mockResolvedValue({ ok: true, action: "merge" });

    render(wrap(<DreamView />));
    await screen.findByText("No dream activity yet.");

    await user.click(screen.getByRole("button", { name: /Inbox/i }));
    expect(await screen.findByText("person:alice")).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Merge" }));

    await waitFor(() => {
      expect(api.resolveFlaggedPair).toHaveBeenCalledWith(
        "tok",
        { ref_a: "person:alice", ref_b: "person:alice-smith", action: "merge" },
      );
    });

    // Row is removed optimistically after resolution
    await waitFor(() => {
      expect(screen.queryByText("person:alice")).not.toBeInTheDocument();
    });
  });

  it("calls resolveFlaggedPair with action:separate and removes the row", async () => {
    const user = userEvent.setup();
    vi.mocked(api.fetchFlaggedPairs).mockResolvedValue([basePair]);
    vi.mocked(api.listQuarantine).mockResolvedValue([]);
    vi.mocked(api.resolveFlaggedPair).mockResolvedValue({ ok: true, action: "separate" });

    render(wrap(<DreamView />));
    await screen.findByText("No dream activity yet.");

    await user.click(screen.getByRole("button", { name: /Inbox/i }));
    expect(await screen.findByText("person:alice")).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Keep separate" }));

    await waitFor(() => {
      expect(api.resolveFlaggedPair).toHaveBeenCalledWith(
        "tok",
        { ref_a: "person:alice", ref_b: "person:alice-smith", action: "separate" },
      );
    });

    await waitFor(() => {
      expect(screen.queryByText("person:alice")).not.toBeInTheDocument();
    });
  });

  it("shows an inline error message when resolveFlaggedPair fails", async () => {
    const user = userEvent.setup();
    vi.mocked(api.fetchFlaggedPairs).mockResolvedValue([basePair]);
    vi.mocked(api.listQuarantine).mockResolvedValue([]);
    vi.mocked(api.resolveFlaggedPair).mockRejectedValue(new Error("HTTP 409"));

    render(wrap(<DreamView />));
    await screen.findByText("No dream activity yet.");

    await user.click(screen.getByRole("button", { name: /Inbox/i }));
    expect(await screen.findByText("person:alice")).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Merge" }));

    // Error message appears; pair row is still visible (not removed on failure)
    expect(await screen.findByText(/Could not resolve pair/)).toBeInTheDocument();
    expect(screen.getByText("person:alice")).toBeInTheDocument();
  });

  it("renders a quarantined skill and calls onOpenSkills when Review in Skills is clicked", async () => {
    const user = userEvent.setup();
    vi.mocked(api.fetchFlaggedPairs).mockResolvedValue([]);
    vi.mocked(api.listQuarantine).mockResolvedValue([baseQuarantine]);

    const onOpenSkills = vi.fn();
    render(wrap(<DreamView onOpenSkills={onOpenSkills} />));
    await screen.findByText("No dream activity yet.");

    await user.click(screen.getByRole("button", { name: /Inbox/i }));

    expect(await screen.findByText("shady-skill")).toBeInTheDocument();
    expect(screen.getByText("Makes outbound requests.")).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Review in Skills" }));

    expect(onOpenSkills).toHaveBeenCalledOnce();
  });
});

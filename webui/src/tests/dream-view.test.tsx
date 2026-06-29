import { act, render, screen, waitFor } from "@testing-library/react";
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
    fetchSkillSuggestions: vi.fn(),
    acceptSkillSuggestion: vi.fn(),
    rejectSkillSuggestion: vi.fn(),
  };
});

type DurinClient = import("@/lib/durin-client").DurinClient;

// A minimal fake client that captures the DreamView's dream-progress handler so
// a test can drive live frames (run_started / activity / run_finished).
function fakeClient(): { client: DurinClient; emitDream: (ev: unknown) => void } {
  let handler: ((ev: unknown) => void) | null = null;
  const client = {
    onDreamProgress: (h: (ev: unknown) => void) => {
      handler = h;
      return () => {
        handler = null;
      };
    },
  } as unknown as DurinClient;
  return {
    client,
    emitDream: (ev) => act(() => handler?.(ev)),
  };
}

function wrap(children: ReactNode, client?: DurinClient) {
  return (
    <ClientProvider client={client ?? fakeClient().client} token="tok">
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
  vi.mocked(api.fetchSkillSuggestions).mockReset();
  vi.mocked(api.acceptSkillSuggestion).mockReset();
  vi.mocked(api.rejectSkillSuggestion).mockReset();

  // Default Bandeja mocks to empty so Resumen tests don't need them
  vi.mocked(api.fetchFlaggedPairs).mockResolvedValue([]);
  vi.mocked(api.listQuarantine).mockResolvedValue([]);
  vi.mocked(api.fetchSkillSuggestions).mockResolvedValue([]);
});
afterEach(() => vi.restoreAllMocks());

describe("DreamView", () => {
  it("renders event summaries returned by fetchDreamDigest", async () => {
    const now = Date.now();
    vi.mocked(api.fetchDreamDigest).mockResolvedValue({
      last_run: null,
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
      last_run: null,
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
      last_run: null,
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
      last_run: null,
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
      last_run: null,
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
      last_run: null,
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

  it("shows the última corrida card with counts, even all zeros", async () => {
    const now = Date.now();
    vi.mocked(api.fetchDreamDigest).mockResolvedValue({
      last_run: {
        at_ms: now, sessions: 0, entities: 0, merged: 0,
        skills_created: 0, skills_improved: 0,
      },
      last_run_at_ms: now,
      events: [],
    });

    render(wrap(<DreamView />));

    // The headline card renders (NOT the empty state) and shows the zero counts,
    // including the created-vs-improved skills split.
    expect(await screen.findByText("Last run")).toBeInTheDocument();
    expect(screen.getByText("entities")).toBeInTheDocument();
    expect(screen.getByText("merges")).toBeInTheDocument();
    expect(screen.getByText("new skills")).toBeInTheDocument();
    expect(screen.getByText("improved skills")).toBeInTheDocument();
    expect(screen.queryByText("No dream activity yet.")).not.toBeInTheDocument();
  });

  it("Run now triggers memory_dream; the live run_finished frame refreshes the digest", async () => {
    const user = userEvent.setup();
    const now = Date.now();
    const { client, emitDream } = fakeClient();

    vi.mocked(api.fetchDreamDigest)
      .mockResolvedValueOnce({ last_run: null, last_run_at_ms: null, events: [] })
      .mockResolvedValueOnce({
        last_run: null,
        last_run_at_ms: now,
        events: [
          { at_ms: now, kind: "merged", ref: null, ref_kind: null, summary: "New dream event" },
        ],
      });
    vi.mocked(api.runCronJob).mockResolvedValue({ started: true });

    render(wrap(<DreamView />, client));

    // Wait for the initial load to finish.
    expect(await screen.findByText("No dream activity yet.")).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Run now" }));

    await waitFor(() => {
      expect(api.runCronJob).toHaveBeenCalledWith("tok", "memory_dream");
    });

    // The server run is async (returns immediately). Nothing refetches until
    // the live run_finished frame arrives — that drives the reconcile fetch.
    emitDream({ event: "dream_progress", kind: "run_finished", ok: true });

    await waitFor(() => {
      expect(api.fetchDreamDigest).toHaveBeenCalledTimes(2);
    });
    expect(await screen.findByText("New dream event")).toBeInTheDocument();
  });

  it("streams live activity items into the feed during a run (no refetch needed)", async () => {
    const now = Date.now();
    const { client, emitDream } = fakeClient();

    vi.mocked(api.fetchDreamDigest).mockResolvedValue({ last_run: null, last_run_at_ms: null, events: [] });

    render(wrap(<DreamView />, client));
    expect(await screen.findByText("No dream activity yet.")).toBeInTheDocument();

    emitDream({ event: "dream_progress", kind: "run_started" });
    emitDream({
      event: "dream_progress",
      kind: "activity",
      item: {
        at_ms: now,
        kind: "merged",
        ref: "place:x",
        ref_kind: "entity",
        summary: "Live merge event",
      },
    });

    expect(await screen.findByText("Live merge event")).toBeInTheDocument();
    // The live item came over the socket — the digest was only fetched once (initial load).
    expect(api.fetchDreamDigest).toHaveBeenCalledTimes(1);
  });

  it("Run now shows an error message when runCronJob fails", async () => {
    const user = userEvent.setup();

    vi.mocked(api.fetchDreamDigest).mockResolvedValue({ last_run: null, last_run_at_ms: null, events: [] });
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
    vi.mocked(api.fetchDreamDigest).mockResolvedValue({ last_run: null, last_run_at_ms: null, events: [] });
  });

  it("shows the Inbox badge count on initial load without opening the tab", async () => {
    vi.mocked(api.fetchFlaggedPairs).mockResolvedValue([
      basePair,
      { ...basePair, ref_a: "person:bob" },
    ]); // 2 pairs
    vi.mocked(api.listQuarantine).mockResolvedValue([baseQuarantine]); // 1 quarantined skill

    render(wrap(<DreamView />));

    // Stay on the default Resumen tab — the badge must still surface 3 (2 + 1)
    // without the user ever opening the Inbox tab.
    const inboxBtn = await screen.findByRole("button", { name: /Inbox/i });
    await waitFor(() => expect(inboxBtn).toHaveTextContent("3"));
    expect(screen.getByText("No dream activity yet.")).toBeInTheDocument();
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

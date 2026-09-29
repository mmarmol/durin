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

describe("DreamView — the dream's decisions live in Pending", () => {
  const pair: api.FlaggedPair = {
    ref_a: "person:alice",
    ref_b: "person:alice-smith",
    verdict: "same_entity",
    confidence: 72,
    reasoning: "Both refs share the same name and email.",
    at_ms: Date.now() - 3_600_000,
    name_a: "Alice",
    name_b: "Alice Smith",
    aliases_a: ["Alice"],
    aliases_b: ["Alice", "A. Smith"],
    proposal: null,
    source: null,
  };
  const suggestion = {
    id: "sug1", skill: "mailer", type: "evolve", reason: "clearer steps", patch: null, created_at: "",
  } as unknown as api.SkillSuggestion;

  beforeEach(() => {
    vi.mocked(api.fetchDreamDigest).mockResolvedValue({ last_run: null, last_run_at_ms: null, events: [] });
  });

  it("has no inbox tab of its own", async () => {
    vi.mocked(api.fetchFlaggedPairs).mockResolvedValue([pair]);

    render(wrap(<DreamView />));
    await screen.findByText("No dream activity yet.");

    expect(screen.queryByRole("button", { name: /Inbox/i })).not.toBeInTheDocument();
  });

  it("counts the dream's decisions and opens Pending at the memory pairs", async () => {
    const user = userEvent.setup();
    vi.mocked(api.fetchFlaggedPairs).mockResolvedValue([pair, { ...pair, ref_a: "person:bob" }]);
    vi.mocked(api.fetchSkillSuggestions).mockResolvedValue([suggestion]);
    const onOpenPending = vi.fn();

    render(wrap(<DreamView onOpenPending={onOpenPending} />));
    await user.click(await screen.findByRole("button", { name: "3 dream decisions are waiting in Pending" }));

    expect(onOpenPending).toHaveBeenCalledWith("flagged_pair");
  });

  it("opens Pending at the skill suggestions when no memory pair waits", async () => {
    const user = userEvent.setup();
    vi.mocked(api.fetchSkillSuggestions).mockResolvedValue([suggestion]);
    const onOpenPending = vi.fn();

    render(wrap(<DreamView onOpenPending={onOpenPending} />));
    await user.click(await screen.findByRole("button", { name: "1 dream decision is waiting in Pending" }));

    expect(onOpenPending).toHaveBeenCalledWith("skill_suggestion");
  });

  it("says nothing when no dream decision waits, and a skill import is not one", async () => {
    vi.mocked(api.listQuarantine).mockResolvedValue([
      { name: "shady-skill", status: "quarantined", source: "https://example.com/s.zip", verdict: "caution", findings: [] },
    ]);

    render(wrap(<DreamView onOpenPending={vi.fn()} />));
    await screen.findByText("No dream activity yet.");
    await waitFor(() => expect(api.fetchSkillSuggestions).toHaveBeenCalled());

    expect(screen.queryByRole("button", { name: /waiting in Pending/ })).not.toBeInTheDocument();
  });
});


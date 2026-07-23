import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import * as api from "@/lib/api";
import { SkillSuggestionsSection } from "./DreamView";

describe("SkillSuggestionsSection", () => {
  beforeEach(() => {
    vi.spyOn(api, "fetchSkillSuggestions").mockResolvedValue([
      { id: "a", skill: "commit-helper", type: "evolve", reason: "run tests first",
        patch: "--- a/SKILL.md\n+++ b/SKILL.md\n@@ -1 +1 @@\n-old\n+new\n", created_at: "" },
    ] as never);
    vi.spyOn(api, "acceptSkillSuggestion").mockResolvedValue({ ok: true });
  });

  it("lists a suggestion and accepts it", async () => {
    render(<SkillSuggestionsSection token="tok" onCountChange={() => {}} />);
    expect(await screen.findByText("commit-helper")).toBeInTheDocument();
    expect(screen.getByText(/run tests first/)).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: /accept/i }));
    await waitFor(() => expect(api.acceptSkillSuggestion).toHaveBeenCalledWith("tok", "a"));
  });

  it("shows inline error and keeps item when accept fails", async () => {
    vi.spyOn(api, "acceptSkillSuggestion").mockRejectedValue(new Error("409"));
    render(<SkillSuggestionsSection token="tok" onCountChange={() => {}} />);
    expect(await screen.findByText("commit-helper")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: /accept/i }));
    await waitFor(() =>
      expect(screen.getByText(/could not apply the suggestion/i)).toBeInTheDocument()
    );
    // Item must still be in the list — not removed on failure
    expect(screen.getByText("commit-helper")).toBeInTheDocument();
  });

  it("shows the server's detail when the ApiError carries one", async () => {
    vi.spyOn(api, "acceptSkillSuggestion").mockRejectedValue(
      new api.ApiError(409, "HTTP 409", "old text not found"),
    );
    render(<SkillSuggestionsSection token="tok" onCountChange={() => {}} />);
    expect(await screen.findByText("commit-helper")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: /accept/i }));
    await waitFor(() =>
      expect(screen.getByText("old text not found")).toBeInTheDocument(),
    );
  });

  it("explains the quarantine block with a localized message", async () => {
    vi.spyOn(api, "acceptSkillSuggestion").mockRejectedValue(
      new api.ApiError(409, "HTTP 409", "skill 'commit-helper' is awaiting review", {
        reason: "skill_quarantined",
        skill: "commit-helper",
      }),
    );
    render(<SkillSuggestionsSection token="tok" onCountChange={() => {}} />);
    expect(await screen.findByText("commit-helper")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: /accept/i }));
    await waitFor(() =>
      expect(
        screen.getByText(/awaiting review in the import quarantine/i),
      ).toBeInTheDocument(),
    );
  });
});

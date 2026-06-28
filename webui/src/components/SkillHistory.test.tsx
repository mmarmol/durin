// webui/src/components/SkillHistory.test.tsx
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import * as api from "@/lib/api";
import { SkillHistory } from "./SkillHistory";

const data = {
  provenance: { source: "workspace", created_at: "", verdict: "" },
  commits: [
    { sha: "abc1234", subject: "edit alpha", actor: "curation", timestamp: "2026-06-28 10:00", session: null, agent: null },
  ],
} as never;

describe("SkillHistory diff expand", () => {
  it("fetches and renders the commit diff on expand", async () => {
    vi.spyOn(api, "fetchSkillCommitDiff").mockResolvedValue({
      sha: "abc1234",
      patch: "diff --git a/alpha/SKILL.md b/alpha/SKILL.md\n--- a/alpha/SKILL.md\n+++ b/alpha/SKILL.md\n@@ -1 +1 @@\n-v1\n+v2\n",
    });
    render(<SkillHistory data={data} skillName="alpha" token="tok" />);
    await userEvent.click(screen.getByText("edit alpha"));
    expect(await screen.findByText(/v2/)).toBeInTheDocument();
    expect(api.fetchSkillCommitDiff).toHaveBeenCalledWith("tok", "alpha", "abc1234");
  });
});

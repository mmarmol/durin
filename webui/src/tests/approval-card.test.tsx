import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { ApprovalCard } from "@/components/thread/ApprovalCard";
import en from "@/i18n/locales/en/common.json";
import es from "@/i18n/locales/es/common.json";
import type { PendingApproval } from "@/lib/types";

const EXEC: PendingApproval = {
  approval_id: "a1b2c3d4e5f6",
  kind: "exec_command",
  summary: "run `rm -rf build`",
  detail: { command: "rm -rf build", rule: "\\brm\\s+-[rf]{1,2}\\b" },
};

const EDIT: PendingApproval = {
  approval_id: "b1b2c3d4e5f6",
  kind: "skill_edit",
  summary: "edit skill 'mailer'",
  detail: {
    verdict: "caution",
    findings: [{ severity: "medium", message: "downloads a script at runtime" }, "reads ~/.ssh"],
    diff: "--- a/SKILL.md\n+++ b/SKILL.md\n-old line\n+new line",
  },
};

// Real skill-scan findings are objects `{category, severity, where, detail}`
// (durin/agent/skills_store.py), not the `{severity, message}` shape above.
const SKILL_SCAN: PendingApproval = {
  approval_id: "c1c2c3c4c5c6",
  kind: "skill_edit",
  summary: "edit SKILL.md of skill 'mailer'",
  detail: {
    verdict: "caution",
    findings: [
      {
        category: "network",
        severity: "medium",
        where: "SKILL.md:12",
        detail: "downloads a script at runtime",
      },
    ],
    new_findings: [
      {
        category: "filesystem",
        severity: "low",
        where: "SKILL.md:20",
        detail: "reads ~/.ssh",
      },
    ],
  },
};

const MCP: PendingApproval = {
  approval_id: "d1d2d3d4d5d6",
  kind: "mcp_change",
  summary: "add MCP server 'weather' → stdio",
  detail: {
    server: "weather → stdio",
    target: "stdio",
    source: "npm:weather-mcp",
    config: { command: "npx", args: ["weather-mcp"] },
    env: "API_KEY=${secret:WEATHER_API_KEY}",
    security: "network=true, sampling.enabled=true",
  },
};

const keysDeep = (o: object, p = ""): string[] =>
  Object.entries(o).flatMap(([k, v]) =>
    v && typeof v === "object" ? keysDeep(v, `${p}${k}.`) : [`${p}${k}`]);

describe("ApprovalCard", () => {
  it("shows what would run and what was reviewed", () => {
    render(<ApprovalCard approval={EXEC} onDecide={vi.fn()} />);
    expect(screen.getByText("Approval needed")).toBeInTheDocument();
    expect(screen.getByText("Run a command")).toBeInTheDocument();
    expect(screen.getByText("run `rm -rf build`")).toBeInTheDocument();
    expect(screen.getByText("$ rm -rf build")).toBeInTheDocument();
    expect(screen.getByText("rule")).toBeInTheDocument();
  });

  it("renders the scan verdict, findings and diff of a skill change", () => {
    render(<ApprovalCard approval={EDIT} onDecide={vi.fn()} />);
    expect(screen.getByText("Caution")).toBeInTheDocument();
    expect(screen.getByText("[medium] downloads a script at runtime")).toBeInTheDocument();
    expect(screen.getByText("reads ~/.ssh")).toBeInTheDocument();
    expect(screen.getByText("-old line")).toBeInTheDocument();
    expect(screen.getByText("+new line")).toBeInTheDocument();
  });

  it("renders a skill-shaped finding's category, detail and where, and no raw JSON", () => {
    const { container } = render(<ApprovalCard approval={SKILL_SCAN} onDecide={vi.fn()} />);
    expect(
      screen.getByText("[medium] network: downloads a script at runtime (SKILL.md:12)"),
    ).toBeInTheDocument();
    expect(container.textContent).not.toContain("{");
  });

  it("renders new_findings as a second list under its own label", () => {
    render(<ApprovalCard approval={SKILL_SCAN} onDecide={vi.fn()} />);
    expect(screen.getByText("New findings")).toBeInTheDocument();
    expect(
      screen.getByText("[low] filesystem: reads ~/.ssh (SKILL.md:20)"),
    ).toBeInTheDocument();
  });

  it("shows an MCP approval's env and security detail rows", () => {
    render(<ApprovalCard approval={MCP} onDecide={vi.fn()} />);
    expect(screen.getByText("env")).toBeInTheDocument();
    expect(screen.getByText("API_KEY=${secret:WEATHER_API_KEY}")).toBeInTheDocument();
    expect(screen.getByText("security")).toBeInTheDocument();
    expect(screen.getByText("network=true, sampling.enabled=true")).toBeInTheDocument();
  });

  it("sends the verdict through onDecide and reports the hand-off", async () => {
    const onDecide = vi
      .fn()
      .mockResolvedValue({ status: "pending", message: "Handed to the waiting turn." });
    render(<ApprovalCard approval={EXEC} onDecide={onDecide} />);
    fireEvent.click(screen.getByRole("button", { name: "Approve" }));
    expect(onDecide).toHaveBeenCalledWith("approve");
    await waitFor(() =>
      expect(screen.getByText(/Sent — the agent continues\./)).toBeInTheDocument());
    expect(screen.getByRole("button", { name: "Approve" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Reject" })).toBeDisabled();
  });

  it("shows the server's refusal and lets the person try again", async () => {
    const onDecide = vi
      .fn()
      .mockRejectedValue(new Error("Approval a1b2c3d4e5f6 was already decided (applied)."));
    render(<ApprovalCard approval={EXEC} onDecide={onDecide} />);
    fireEvent.click(screen.getByRole("button", { name: "Reject" }));
    expect(onDecide).toHaveBeenCalledWith("reject");
    await waitFor(() => expect(screen.getByText(/already decided/)).toBeInTheDocument());
    expect(screen.getByRole("button", { name: "Reject" })).not.toBeDisabled();
  });
});

describe("approval i18n parity", () => {
  it("es mirrors every en message.approval key", () => {
    const enApproval = (en as { message: { approval?: object } }).message.approval ?? {};
    const esApproval = (es as { message: { approval?: object } }).message.approval ?? {};
    expect(keysDeep(enApproval).length).toBeGreaterThan(0);
    expect(keysDeep(esApproval)).toEqual(keysDeep(enApproval));
  });
});

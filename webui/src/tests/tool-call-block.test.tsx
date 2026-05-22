import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { ToolCallBlock } from "@/components/thread/ToolCallBlock";
import type { ToolProgressEvent } from "@/lib/types";

/**
 * ToolCallBlock gives the interactive tools (ask_user_question,
 * request_secret) a render of their own — built from the call
 * arguments, never leaking the internal "YIELD TO USER" instruction the
 * raw tool result carries.
 */
describe("ToolCallBlock — interactive tools", () => {
  it("ask_user_question shows the question and numbered options", () => {
    const event: ToolProgressEvent = {
      phase: "end",
      call_id: "aq1",
      name: "ask_user_question",
      arguments: { question: "Which database?", options: ["Postgres", "SQLite"] },
      result: "Question registered (id=abc). YIELD TO USER. Present this...",
    };
    render(<ToolCallBlock event={event} />);
    // The ❓ prefix is unique to the body line (the header summary also
    // echoes the bare question text).
    expect(screen.getByText(/❓ Which database\?/)).toBeInTheDocument();
    expect(screen.getByText(/1\. Postgres/)).toBeInTheDocument();
    expect(screen.getByText(/2\. SQLite/)).toBeInTheDocument();
    expect(screen.queryByText(/YIELD TO USER/)).not.toBeInTheDocument();
  });

  it("request_secret shows the durin secret set command", () => {
    const event: ToolProgressEvent = {
      phase: "end",
      call_id: "rs1",
      name: "request_secret",
      arguments: { name: "GH_TOKEN", service: "github", purpose: "open PRs" },
      result: "Secret 'GH_TOKEN' is not stored. YIELD TO USER. Present...",
    };
    render(<ToolCallBlock event={event} />);
    expect(screen.getByText(/open PRs/)).toBeInTheDocument();
    expect(
      screen.getByText(/durin secret set GH_TOKEN --service github --scope exec/),
    ).toBeInTheDocument();
    expect(screen.queryByText(/YIELD TO USER/)).not.toBeInTheDocument();
  });

  it("request_secret reports an already-stored credential", () => {
    const event: ToolProgressEvent = {
      phase: "end",
      call_id: "rs2",
      name: "request_secret",
      arguments: { name: "GH_TOKEN", service: "github" },
      result: "Secret 'GH_TOKEN' already exists (service=github, scope=exec).",
    };
    render(<ToolCallBlock event={event} />);
    expect(screen.getByText(/already stored/)).toBeInTheDocument();
    expect(screen.queryByText(/durin secret set/)).not.toBeInTheDocument();
  });
});

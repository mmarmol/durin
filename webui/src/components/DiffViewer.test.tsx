import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { DiffViewer } from "./DiffViewer";

const PATCH = `--- a/SKILL.md
+++ b/SKILL.md
@@ -1,2 +1,2 @@
 context line
-old line
+new line
`;

const MULTI_FILE_PATCH = `--- a/SKILL.md
+++ b/SKILL.md
@@ -1,2 +1,2 @@
 context line
-old line
+new line
--- a/scripts/check.sh
+++ b/scripts/check.sh
@@ -1,2 +1,2 @@
 #!/bin/sh
-exit 0
+exit 1
`;

describe("DiffViewer", () => {
  it("renders the file header and changed lines", () => {
    render(<DiffViewer patch={PATCH} />);
    expect(screen.getByText("SKILL.md")).toBeInTheDocument();
    expect(screen.getByText(/new line/)).toBeInTheDocument();
    expect(screen.getByText(/old line/)).toBeInTheDocument();
  });

  it("renders nothing for an empty patch", () => {
    const { container } = render(<DiffViewer patch="" />);
    expect(container.textContent).toBe("");
  });

  it("renders both file headers for a multi-file plain unified diff", () => {
    render(<DiffViewer patch={MULTI_FILE_PATCH} />);
    expect(screen.getByText("SKILL.md")).toBeInTheDocument();
    expect(screen.getByText("scripts/check.sh")).toBeInTheDocument();
  });
});

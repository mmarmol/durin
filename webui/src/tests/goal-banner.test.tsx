import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { GoalBanner } from "@/components/thread/GoalBanner";

describe("GoalBanner", () => {
  it("renders the objective when a goal is active", () => {
    render(<GoalBanner goal={{ active: true, objective: "Migrate auth to OAuth", ui_summary: "2/5 steps" }} />);
    expect(screen.getByText("Migrate auth to OAuth")).toBeInTheDocument();
    expect(screen.getByText("2/5 steps")).toBeInTheDocument();
  });

  it("renders nothing when there is no active goal", () => {
    const { container } = render(<GoalBanner goal={{ active: false }} />);
    expect(container).toBeEmptyDOMElement();
  });

  it("renders nothing when goal is undefined", () => {
    const { container } = render(<GoalBanner goal={undefined} />);
    expect(container).toBeEmptyDOMElement();
  });
});

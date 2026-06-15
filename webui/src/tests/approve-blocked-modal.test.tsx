import { describe, it, expect, vi } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import { ApproveBlockedModal } from "../components/ApproveBlockedModal";

describe("ApproveBlockedModal", () => {
  it("renders skill name and non-installable deps", () => {
    render(
      <ApproveBlockedModal
        skillName="my-skill"
        nonInstallableBins={["custom-tool"]}
        onApprove={() => {}}
        onCancel={() => {}}
      />,
    );
    // h3 uses i18n interpolation, so match partial
    expect(screen.getByText(/my-skill/)).toBeInTheDocument();
    // li has bullet prefix, so match partial
    expect(screen.getByText(/custom-tool/)).toBeInTheDocument();
  });

  it("calls onApprove when approve button clicked", () => {
    const onApprove = vi.fn();
    render(
      <ApproveBlockedModal
        skillName="s"
        nonInstallableBins={["x"]}
        onApprove={onApprove}
        onCancel={() => {}}
      />,
    );
    fireEvent.click(screen.getByTestId("approve-btn"));
    expect(onApprove).toHaveBeenCalled();
  });

  it("calls onCancel when cancel button clicked", () => {
    const onCancel = vi.fn();
    render(
      <ApproveBlockedModal
        skillName="s"
        nonInstallableBins={["x"]}
        onApprove={() => {}}
        onCancel={onCancel}
      />,
    );
    fireEvent.click(screen.getByTestId("cancel-btn"));
    expect(onCancel).toHaveBeenCalled();
  });
});

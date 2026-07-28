import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { expect, it, vi } from "vitest";

import { MaskedSecret } from "@/components/settings/secrets/MaskedSecret";

const storeSecret = vi.fn().mockResolvedValue(undefined);

vi.mock("@/providers/ClientProvider", () => ({
  useClient: () => ({ client: { storeSecret } }),
}));

it("rotates through the rotate path so the stored metadata survives", async () => {
  // A plain store REPLACES the entry: scope, account and description are
  // whatever the caller sent. This dialog only ever collects a new value, so
  // without `rotate` it silently strips an exec-scoped credential of the very
  // scope that lets shell commands read it.
  storeSecret.mockClear();
  render(
    <MaskedSecret secretName="ATLASSIAN_API_TOKEN" busy={false} onDisconnect={() => {}} />,
  );
  const [openRotate] = screen.getAllByRole("button");
  await userEvent.click(openRotate);
  const input = await screen.findByPlaceholderText(/.+/);
  await userEvent.type(input, "new-token-value{Enter}");

  expect(storeSecret).toHaveBeenCalledWith(
    expect.objectContaining({ name: "ATLASSIAN_API_TOKEN", value: "new-token-value", rotate: true }),
  );
  expect(storeSecret.mock.calls[0][0]).not.toHaveProperty("scope");
});

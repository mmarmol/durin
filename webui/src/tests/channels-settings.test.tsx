import { render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { ChannelsSettings } from "@/components/settings/ChannelsSettings";
import type { ChannelInfo } from "@/lib/api";

const listChannels = vi.fn();
const getConfig = vi.fn();

vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/api")>();
  return {
    ...actual,
    listChannels: (...a: unknown[]) => listChannels(...a),
    getConfig: (...a: unknown[]) => getConfig(...a),
  };
});

vi.mock("@/providers/ClientProvider", () => ({
  useClient: () => ({ client: {} }),
}));

function channel(over: Partial<ChannelInfo>): ChannelInfo {
  return {
    name: "x",
    display_name: "X",
    enabled: false,
    always_on: false,
    description: "",
    credential_field: null,
    fields: [],
    available: true,
    install_extra: null,
    ...over,
  };
}

describe("ChannelsSettings availability hint", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    getConfig.mockResolvedValue({ config: { channels: {} } });
  });

  it("hints only channels reporting available === false (old gateways omit the field)", async () => {
    listChannels.mockResolvedValue([
      channel({ name: "matrix", display_name: "Matrix", available: false, install_extra: "matrix" }),
      channel({ name: "qq", display_name: "QQ", enabled: true, available: false, install_extra: "qq" }),
      // Old gateway payload: no available/install_extra at all → no hint.
      channel({ name: "telegram", display_name: "Telegram", available: undefined }),
    ]);
    render(<ChannelsSettings token="t" />);
    await screen.findByText("Matrix");
    // Exactly ONE hint per variant — a `!available` gate would also hint the
    // old-gateway telegram row (undefined), which getByText rejects as ambiguous.
    expect(
      screen.getByText(/Optional dependency required.*durin-ai\[matrix\]/),
    ).toBeInTheDocument();
    expect(
      screen.getByText(/Will install on next gateway restart.*durin-ai\[qq\]/),
    ).toBeInTheDocument();
    expect(screen.getAllByText(/Optional dependency required/)).toHaveLength(1);
    expect(screen.getAllByText(/Will install on next gateway restart/)).toHaveLength(1);
  });
});

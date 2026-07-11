import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { ChannelsSettings } from "@/components/settings/ChannelsSettings";
import { setConfigValue, type ChannelInfo } from "@/lib/api";

vi.mock("@/lib/api", async (importOriginal) => ({
  ...(await importOriginal<object>()),
  listChannels: vi.fn(() => Promise.resolve(CHANNELS)),
  getConfig: vi.fn(() =>
    Promise.resolve({
      config: { channels: { discord: { enabled: true }, email: {}, websocket: {} } },
    }),
  ),
  setConfigValue: vi.fn(() => Promise.resolve()),
  listPersonas: vi.fn(() =>
    Promise.resolve({ personas: [{ name: "ops", description: "" }], default: "" }),
  ),
}));
vi.mock("@/providers/ClientProvider", () => ({
  useClient: () => ({ client: {} }),
}));

const CHANNELS: ChannelInfo[] = [
  {
    name: "discord",
    display_name: "Discord",
    enabled: true,
    always_on: false,
    description: "Discord bot",
    credential_field: "token",
    fields: [],
  },
  {
    name: "websocket",
    display_name: "Web dashboard",
    enabled: true,
    always_on: true,
    description: "Built-in web chat",
    credential_field: null,
    fields: [],
  },
  {
    name: "email",
    display_name: "Email",
    enabled: false,
    always_on: false,
    description: "IMAP/SMTP",
    credential_field: null,
    fields: [
      {
        name: "persona",
        type: "string",
        secret: false,
        group: "behavior",
        required: false,
        default: "",
      },
      {
        name: "signature",
        type: "string",
        secret: false,
        group: "behavior",
        required: false,
        default: "",
      },
    ],
  },
];

async function openChannel(displayName: string) {
  render(<ChannelsSettings token="t" />);
  const header = await screen.findByText(displayName);
  fireEvent.click(header);
}

describe("universal channel persona row", () => {
  it("renders a persona select for a schema-less channel and writes the config key", async () => {
    await openChannel("Discord");
    const select = await screen.findByRole("combobox");
    await waitFor(() => expect(select).toBeEnabled());
    fireEvent.change(select, { target: { value: "ops" } });
    await waitFor(() =>
      expect(setConfigValue).toHaveBeenCalledWith("t", "channels.discord.persona", "ops"),
    );
  });

  it("does not render a persona select for websocket", async () => {
    await openChannel("Web dashboard");
    // Accordion is open (localized description visible) but no select rendered.
    expect(
      await screen.findByText(/Transport for the web dashboard/),
    ).toBeInTheDocument();
    expect(screen.queryByRole("combobox")).not.toBeInTheDocument();
  });

  it("renders the persona select exactly once for typed channels that declare the field", async () => {
    await openChannel("Email");
    await screen.findByRole("combobox");
    // Open the Advanced section, where the behavior-group fields render.
    fireEvent.click(screen.getByRole("button", { name: /advanced/i }));
    // signature (behavior group) shows, persona is filtered out of the form.
    expect(await screen.findByText("signature")).toBeInTheDocument();
    expect(screen.getAllByRole("combobox")).toHaveLength(1);
  });
});

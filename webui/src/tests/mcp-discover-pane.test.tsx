import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, expect, it, vi } from "vitest";

import { McpDiscoverPane } from "@/components/settings/McpDiscoverPane";
import type { McpRegistryHit, McpRegistryServerDetail } from "@/lib/types";

const searchMcpRegistry = vi.fn();
const describeMcpRegistryServer = vi.fn();
const installMcpFromRegistry = vi.fn();

vi.mock("@/lib/api", () => ({
  searchMcpRegistry: (...a: unknown[]) => searchMcpRegistry(...a),
  describeMcpRegistryServer: (...a: unknown[]) => describeMcpRegistryServer(...a),
  installMcpFromRegistry: (...a: unknown[]) => installMcpFromRegistry(...a),
}));

const OFFICIAL_HIT: McpRegistryHit = {
  name: "github-mcp",
  ref: "registry/github-mcp",
  registry: "official",
  kind: "remote",
  description: "The official GitHub MCP server",
  signals: {
    stars: 4200,
    owner_login: "github",
    owner_url: "https://github.com/github",
    owner_avatar: "https://avatars.githubusercontent.com/u/9919",
    topics: ["github", "api"],
    language: "TypeScript",
    license: "MIT",
    official: true,
    repo_url: "https://github.com/github/github-mcp",
  },
};

const PLAIN_HIT: McpRegistryHit = {
  name: "postgres-mcp",
  ref: "registry/postgres-mcp",
  registry: "public",
  kind: "local",
  description: "PostgreSQL MCP server",
  signals: {
    stars: 150,
    owner_login: "acme",
    owner_url: "https://github.com/acme",
    owner_avatar: "",
    topics: [],
    language: "Python",
    official: false,
    repo_url: "https://github.com/acme/postgres-mcp",
  },
};

const DETAIL: McpRegistryServerDetail = {
  name: "github-mcp",
  ref: "registry/github-mcp",
  description: "Detailed description of the GitHub MCP server",
  version: "1.2.3",
  repository: "https://github.com/github/github-mcp",
  packages: [],
  remotes: [{ transport_type: "sse", url: "https://mcp.github.com/sse", headers: [] }],
};

beforeEach(() => {
  searchMcpRegistry.mockReset();
  describeMcpRegistryServer.mockReset();
  installMcpFromRegistry.mockReset();
});
afterEach(() => vi.restoreAllMocks());

it("renders the Official badge for a hit with signals.official=true", async () => {
  const user = userEvent.setup();
  searchMcpRegistry.mockResolvedValue([OFFICIAL_HIT, PLAIN_HIT]);

  render(<McpDiscoverPane token="tok" onClose={vi.fn()} />);

  await user.type(screen.getByRole("textbox"), "github");
  await user.click(screen.getByRole("button", { name: /search/i }));

  // Official badge must appear for the official hit
  expect(await screen.findByText("github-mcp")).toBeInTheDocument();
  const badges = screen.getAllByText("Official");
  expect(badges.length).toBeGreaterThanOrEqual(1);

  // The non-official hit must NOT get a badge
  expect(screen.getByText("postgres-mcp")).toBeInTheDocument();
  // Only one Official badge — the plain hit has none
  expect(screen.getAllByText("Official")).toHaveLength(1);
});

it("owner link has target=_blank and rel=noopener on listing rows", async () => {
  const user = userEvent.setup();
  searchMcpRegistry.mockResolvedValue([OFFICIAL_HIT]);

  render(<McpDiscoverPane token="tok" onClose={vi.fn()} />);

  await user.type(screen.getByRole("textbox"), "github");
  await user.click(screen.getByRole("button", { name: /search/i }));

  await screen.findByText("github-mcp");

  const ownerLink = screen.getByRole("link", { name: /@github/ });
  expect(ownerLink).toHaveAttribute("target", "_blank");
  expect(ownerLink).toHaveAttribute("rel", expect.stringContaining("noopener"));
  expect(ownerLink).toHaveAttribute("href", "https://github.com/github");
});

it("renders star count and language on listing rows", async () => {
  const user = userEvent.setup();
  searchMcpRegistry.mockResolvedValue([OFFICIAL_HIT]);

  render(<McpDiscoverPane token="tok" onClose={vi.fn()} />);

  await user.type(screen.getByRole("textbox"), "github");
  await user.click(screen.getByRole("button", { name: /search/i }));

  await screen.findByText("github-mcp");
  expect(screen.getByText(/★.*4[,.]?200/)).toBeInTheDocument();
  expect(screen.getByText("TypeScript")).toBeInTheDocument();
});

it("renders topic chips when topics are present", async () => {
  const user = userEvent.setup();
  searchMcpRegistry.mockResolvedValue([OFFICIAL_HIT]);

  render(<McpDiscoverPane token="tok" onClose={vi.fn()} />);

  await user.type(screen.getByRole("textbox"), "github");
  await user.click(screen.getByRole("button", { name: /search/i }));

  await screen.findByText("github-mcp");
  expect(screen.getByText("github")).toBeInTheDocument();
  expect(screen.getByText("api")).toBeInTheDocument();
});

it("detail view shows Official badge, owner link, stars, View on GitHub link", async () => {
  const user = userEvent.setup();
  searchMcpRegistry.mockResolvedValue([OFFICIAL_HIT]);
  describeMcpRegistryServer.mockResolvedValue(DETAIL);

  render(<McpDiscoverPane token="tok" onClose={vi.fn()} />);

  await user.type(screen.getByRole("textbox"), "github");
  await user.click(screen.getByRole("button", { name: /search/i }));

  const row = await screen.findByText("github-mcp");
  await user.click(row.closest("button")!);

  // Version shown
  await screen.findByText(/v1\.2\.3/);

  // Official badge
  expect(screen.getAllByText("Official").length).toBeGreaterThanOrEqual(1);

  // Owner link with target=_blank
  const ownerLink = screen.getByRole("link", { name: /by.*@github/i });
  expect(ownerLink).toHaveAttribute("target", "_blank");
  expect(ownerLink).toHaveAttribute("rel", expect.stringContaining("noopener"));

  // Stars
  expect(screen.getByText(/★.*4[,.]?200/)).toBeInTheDocument();

  // View on GitHub link
  const ghLink = screen.getByRole("link", { name: /view on github/i });
  expect(ghLink).toHaveAttribute("href", "https://github.com/github/github-mcp");
  expect(ghLink).toHaveAttribute("target", "_blank");
  expect(ghLink).toHaveAttribute("rel", expect.stringContaining("noopener"));
});

it("detail view keeps install button functional", async () => {
  const user = userEvent.setup();
  searchMcpRegistry.mockResolvedValue([OFFICIAL_HIT]);
  describeMcpRegistryServer.mockResolvedValue(DETAIL);
  installMcpFromRegistry.mockResolvedValue({});
  const onClose = vi.fn();

  render(<McpDiscoverPane token="tok" onClose={onClose} />);

  await user.type(screen.getByRole("textbox"), "github");
  await user.click(screen.getByRole("button", { name: /search/i }));

  const row = await screen.findByText("github-mcp");
  await user.click(row.closest("button")!);

  await screen.findByText(/v1\.2\.3/);

  await user.click(screen.getByRole("button", { name: /connect/i }));

  await waitFor(() => {
    expect(installMcpFromRegistry).toHaveBeenCalledWith(
      "tok",
      "registry/github-mcp",
      "remote",
      {},
    );
    expect(onClose).toHaveBeenCalledWith(true);
  });
});

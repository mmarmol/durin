import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, expect, it, vi } from "vitest";

import { McpDiscoverPane } from "@/components/settings/McpDiscoverPane";
import type { McpRegistryHit, McpRegistryServerDetail } from "@/lib/types";

const searchMcpRegistry = vi.fn();
const describeMcpRegistryServer = vi.fn();
const installMcpFromRegistry = vi.fn();
const mcpRegistryRuntime = vi.fn();

vi.mock("@/lib/api", () => ({
  searchMcpRegistry: (...a: unknown[]) => searchMcpRegistry(...a),
  describeMcpRegistryServer: (...a: unknown[]) => describeMcpRegistryServer(...a),
  installMcpFromRegistry: (...a: unknown[]) => installMcpFromRegistry(...a),
  mcpRegistryRuntime: (...a: unknown[]) => mcpRegistryRuntime(...a),
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

const LOCAL_OCI_DETAIL: McpRegistryServerDetail = {
  name: "github-mcp-server",
  ref: "io.github.github/github-mcp-server",
  description: "GitHub MCP server",
  version: "1.4.0",
  repository: "https://github.com/github/github-mcp-server",
  packages: [
    {
      registry_type: "oci",
      identifier: "ghcr.io/github/github-mcp-server:1.4.0",
      version: "",
      runtime_hint: "",
      transport_type: "stdio",
      runtime_arguments: [],
      package_arguments: [],
      env: [
        {
          name: "GITHUB_PERSONAL_ACCESS_TOKEN",
          description: "",
          is_required: true,
          is_secret: true,
          default: null,
        },
      ],
    },
  ],
  remotes: [],
};

beforeEach(() => {
  searchMcpRegistry.mockReset();
  describeMcpRegistryServer.mockReset();
  installMcpFromRegistry.mockReset();
  mcpRegistryRuntime.mockReset();
  // Default: remote model needs no local runtime (keeps existing detail tests green).
  mcpRegistryRuntime.mockResolvedValue({
    kind: "remote",
    runtime: "",
    present: true,
    auto_installable: false,
    install_command: "",
  });
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

it("owner with no owner_url renders plain text, not a broken anchor", async () => {
  const user = userEvent.setup();
  const NO_URL_HIT: McpRegistryHit = {
    name: "no-url-mcp",
    ref: "registry/no-url-mcp",
    registry: "public",
    kind: "local",
    description: "A server whose owner has no URL",
    signals: {
      owner_login: "orphan",
      // owner_url intentionally absent
    },
  };
  searchMcpRegistry.mockResolvedValue([NO_URL_HIT]);

  render(<McpDiscoverPane token="tok" onClose={vi.fn()} />);

  await user.type(screen.getByRole("textbox"), "orphan");
  await user.click(screen.getByRole("button", { name: /search/i }));

  await screen.findByText("no-url-mcp");

  // @orphan text must appear
  expect(screen.getByText("@orphan")).toBeInTheDocument();

  // No anchor whose href is the string "undefined"
  const links = screen.queryAllByRole("link");
  const broken = links.filter((l) => l.getAttribute("href") === "undefined");
  expect(broken).toHaveLength(0);
});

it("show-all toggle re-searches with includeAll=true and flips back", async () => {
  const user = userEvent.setup();
  // First search returns only official; second call (include_all) returns both
  searchMcpRegistry
    .mockResolvedValueOnce([OFFICIAL_HIT])
    .mockResolvedValueOnce([OFFICIAL_HIT, PLAIN_HIT]);

  render(<McpDiscoverPane token="tok" onClose={vi.fn()} />);

  // Initial search
  await user.type(screen.getByRole("textbox"), "jira");
  await user.click(screen.getByRole("button", { name: /search/i }));
  await screen.findByText("github-mcp");

  // Toggle label should read "Showing official only"
  expect(screen.getByText(/showing official only/i)).toBeInTheDocument();
  const showAllBtn = screen.getByRole("button", { name: /show all/i });
  expect(showAllBtn).toBeInTheDocument();

  // Click "Show all" — should trigger a second search with includeAll=true
  await user.click(showAllBtn);
  await screen.findByText("postgres-mcp");

  // Verify the second call passed include_all=true (5th arg)
  expect(searchMcpRegistry).toHaveBeenCalledTimes(2);
  expect(searchMcpRegistry.mock.calls[1][4]).toBe(true);

  // Toggle now shows "Showing all" + "Official only" button
  expect(screen.getByText(/showing all/i)).toBeInTheDocument();
  expect(screen.getByRole("button", { name: /official only/i })).toBeInTheDocument();
});

it("local detail with missing Docker surfaces an install-Docker banner", async () => {
  const user = userEvent.setup();
  searchMcpRegistry.mockResolvedValue([OFFICIAL_HIT]);
  describeMcpRegistryServer.mockResolvedValue(LOCAL_OCI_DETAIL);
  mcpRegistryRuntime.mockResolvedValue({
    kind: "local",
    runtime: "docker",
    present: false,
    auto_installable: false,
    install_command: "",
  });

  render(<McpDiscoverPane token="tok" onClose={vi.fn()} />);

  await user.type(screen.getByRole("textbox"), "github");
  await user.click(screen.getByRole("button", { name: /search/i }));

  const row = await screen.findByText("github-mcp");
  await user.click(row.closest("button")!);

  await screen.findByText(/v1\.4\.0/);

  // Runtime status was queried for the local model
  await waitFor(() =>
    expect(mcpRegistryRuntime).toHaveBeenCalledWith(
      "tok",
      "io.github.github/github-mcp-server",
      "local",
    ),
  );

  // A "Get Docker Desktop" link to docker.com opens in a new tab
  const dockerLink = await screen.findByRole("link", { name: /docker/i });
  expect(dockerLink).toHaveAttribute(
    "href",
    expect.stringContaining("docker.com"),
  );
  expect(dockerLink).toHaveAttribute("target", "_blank");
  expect(dockerLink).toHaveAttribute("rel", expect.stringContaining("noopener"));
});

it("local detail with missing npx shows a copy-paste install command", async () => {
  const user = userEvent.setup();
  searchMcpRegistry.mockResolvedValue([OFFICIAL_HIT]);
  describeMcpRegistryServer.mockResolvedValue({
    ...LOCAL_OCI_DETAIL,
    packages: [
      { ...LOCAL_OCI_DETAIL.packages[0], registry_type: "npm", runtime_hint: "npx" },
    ],
  });
  mcpRegistryRuntime.mockResolvedValue({
    kind: "local",
    runtime: "npx",
    present: false,
    auto_installable: true,
    install_command: "brew install node",
  });

  render(<McpDiscoverPane token="tok" onClose={vi.fn()} />);

  await user.type(screen.getByRole("textbox"), "github");
  await user.click(screen.getByRole("button", { name: /search/i }));
  const row = await screen.findByText("github-mcp");
  await user.click(row.closest("button")!);
  await screen.findByText(/v1\.4\.0/);

  expect(await screen.findByText("brew install node")).toBeInTheDocument();
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

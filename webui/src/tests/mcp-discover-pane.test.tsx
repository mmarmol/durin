import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, expect, it, vi } from "vitest";

import { McpDiscoverPane } from "@/components/settings/McpDiscoverPane";
import type { McpRegistryHit, McpRegistryServerDetail } from "@/lib/types";

const searchMcpRegistry = vi.fn();
const describeMcpRegistryServer = vi.fn();
const installMcpFromRegistry = vi.fn();
const mcpRegistryRuntime = vi.fn();
const mcpRegistryOauthCapability = vi.fn();

vi.mock("@/lib/api", () => ({
  searchMcpRegistry: (...a: unknown[]) => searchMcpRegistry(...a),
  describeMcpRegistryServer: (...a: unknown[]) => describeMcpRegistryServer(...a),
  installMcpFromRegistry: (...a: unknown[]) => installMcpFromRegistry(...a),
  mcpRegistryRuntime: (...a: unknown[]) => mcpRegistryRuntime(...a),
  mcpRegistryOauthCapability: (...a: unknown[]) => mcpRegistryOauthCapability(...a),
}));

// searchMcpRegistry now resolves to { hits, more } (curated+popular, then the less-popular reveal).
const result = (hits: McpRegistryHit[], more: McpRegistryHit[] = []) => ({ hits, more });

const VERIFIED_HIT: McpRegistryHit = {
  name: "github-mcp",
  ref: "registry/github-mcp",
  registry: "github",
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
    verified: true,
    repo_url: "https://github.com/github/github-mcp",
  },
};

const PLAIN_HIT: McpRegistryHit = {
  name: "postgres-mcp",
  ref: "registry/postgres-mcp",
  registry: "official",
  kind: "local",
  description: "PostgreSQL MCP server",
  signals: {
    stars: 150, // popular (over the floor), not verified → no badge
    owner_login: "acme",
    owner_url: "https://github.com/acme",
    owner_avatar: "",
    topics: [],
    language: "Python",
    verified: false,
    repo_url: "https://github.com/acme/postgres-mcp",
  },
};

const LOW_STAR_HIT: McpRegistryHit = {
  name: "low-star-mcp",
  ref: "registry/low-star-mcp",
  registry: "official",
  kind: "local",
  description: "A niche, low-star server (below the floor)",
  signals: { stars: 5, owner_login: "nobody", verified: false },
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
  mcpRegistryOauthCapability.mockReset();
  // Default: remote model needs no local runtime (keeps detail tests green).
  mcpRegistryRuntime.mockResolvedValue({
    kind: "remote",
    runtime: "",
    present: true,
    auto_installable: false,
    install_command: "",
  });
  // Default: not oauth-capable (keeps existing remote-detail tests green).
  mcpRegistryOauthCapability.mockResolvedValue({ oauth_capable: false });
});
afterEach(() => vi.restoreAllMocks());

it("shows the Verified badge for a verified hit, none for a non-verified popular one", async () => {
  const user = userEvent.setup();
  searchMcpRegistry.mockResolvedValue(result([VERIFIED_HIT, PLAIN_HIT]));

  render(<McpDiscoverPane token="tok" onClose={vi.fn()} />);
  await user.type(screen.getByRole("textbox"), "github");
  await user.click(screen.getByRole("button", { name: /search/i }));

  expect(await screen.findByText("github-mcp")).toBeInTheDocument();
  expect(screen.getByText("postgres-mcp")).toBeInTheDocument();
  // Exactly one "Verified" badge (the verified hit); the popular non-verified one has none.
  expect(screen.getAllByText("Verified")).toHaveLength(1);
});

it("owner link has target=_blank and rel=noopener on listing rows", async () => {
  const user = userEvent.setup();
  searchMcpRegistry.mockResolvedValue(result([VERIFIED_HIT]));

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
  searchMcpRegistry.mockResolvedValue(result([VERIFIED_HIT]));

  render(<McpDiscoverPane token="tok" onClose={vi.fn()} />);
  await user.type(screen.getByRole("textbox"), "github");
  await user.click(screen.getByRole("button", { name: /search/i }));
  await screen.findByText("github-mcp");

  expect(screen.getByText(/★.*4[,.]?200/)).toBeInTheDocument();
  expect(screen.getByText("TypeScript")).toBeInTheDocument();
});

it("renders topic chips when topics are present", async () => {
  const user = userEvent.setup();
  searchMcpRegistry.mockResolvedValue(result([VERIFIED_HIT]));

  render(<McpDiscoverPane token="tok" onClose={vi.fn()} />);
  await user.type(screen.getByRole("textbox"), "github");
  await user.click(screen.getByRole("button", { name: /search/i }));
  await screen.findByText("github-mcp");

  expect(screen.getByText("github")).toBeInTheDocument();
  expect(screen.getByText("api")).toBeInTheDocument();
});

it("detail view shows Verified badge, owner link, stars, View on GitHub link", async () => {
  const user = userEvent.setup();
  searchMcpRegistry.mockResolvedValue(result([VERIFIED_HIT]));
  describeMcpRegistryServer.mockResolvedValue(DETAIL);

  render(<McpDiscoverPane token="tok" onClose={vi.fn()} />);
  await user.type(screen.getByRole("textbox"), "github");
  await user.click(screen.getByRole("button", { name: /search/i }));

  const row = await screen.findByText("github-mcp");
  await user.click(row.closest("button")!);

  await screen.findByText(/v1\.2\.3/);
  expect(screen.getAllByText("Verified").length).toBeGreaterThanOrEqual(1);

  const ownerLink = screen.getByRole("link", { name: /by.*@github/i });
  expect(ownerLink).toHaveAttribute("target", "_blank");
  expect(ownerLink).toHaveAttribute("rel", expect.stringContaining("noopener"));

  expect(screen.getByText(/★.*4[,.]?200/)).toBeInTheDocument();

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
    signals: { owner_login: "orphan", stars: 200 },
  };
  searchMcpRegistry.mockResolvedValue(result([NO_URL_HIT]));

  render(<McpDiscoverPane token="tok" onClose={vi.fn()} />);
  await user.type(screen.getByRole("textbox"), "orphan");
  await user.click(screen.getByRole("button", { name: /search/i }));
  await screen.findByText("no-url-mcp");

  expect(screen.getByText("@orphan")).toBeInTheDocument();
  const links = screen.queryAllByRole("link");
  expect(links.filter((l) => l.getAttribute("href") === "undefined")).toHaveLength(0);
});

it("reveals less-popular results progressively in one call (no 'show all' mode)", async () => {
  const user = userEvent.setup();
  searchMcpRegistry.mockResolvedValue(result([VERIFIED_HIT], [LOW_STAR_HIT]));

  render(<McpDiscoverPane token="tok" onClose={vi.fn()} />);
  await user.type(screen.getByRole("textbox"), "jira");
  await user.click(screen.getByRole("button", { name: /search/i }));
  await screen.findByText("github-mcp");

  // The less-popular hit is hidden until requested
  expect(screen.queryByText("low-star-mcp")).not.toBeInTheDocument();

  // A single "show N less-popular" affordance reveals them inline — no second search call
  await user.click(screen.getByRole("button", { name: /less-popular/i }));
  expect(await screen.findByText("low-star-mcp")).toBeInTheDocument();
  expect(searchMcpRegistry).toHaveBeenCalledTimes(1);
});

it("shows the less-popular results inline when there are no curated/popular hits", async () => {
  const user = userEvent.setup();
  searchMcpRegistry.mockResolvedValue(result([], [LOW_STAR_HIT]));

  render(<McpDiscoverPane token="tok" onClose={vi.fn()} />);
  await user.type(screen.getByRole("textbox"), "obscure");
  await user.click(screen.getByRole("button", { name: /search/i }));

  // No "hits" to hide them behind → shown directly (not behind a click)
  expect(await screen.findByText("low-star-mcp")).toBeInTheDocument();
});

it("local detail with missing Docker surfaces an install-Docker banner", async () => {
  const user = userEvent.setup();
  searchMcpRegistry.mockResolvedValue(result([VERIFIED_HIT]));
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

  await waitFor(() =>
    expect(mcpRegistryRuntime).toHaveBeenCalledWith(
      "tok",
      "io.github.github/github-mcp-server",
      "local",
    ),
  );

  const dockerLink = await screen.findByRole("link", { name: /docker/i });
  expect(dockerLink).toHaveAttribute("href", expect.stringContaining("docker.com"));
  expect(dockerLink).toHaveAttribute("target", "_blank");
  expect(dockerLink).toHaveAttribute("rel", expect.stringContaining("noopener"));
});

it("local detail with missing npx shows a copy-paste install command", async () => {
  const user = userEvent.setup();
  searchMcpRegistry.mockResolvedValue(result([VERIFIED_HIT]));
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
  searchMcpRegistry.mockResolvedValue(result([VERIFIED_HIT]));
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
    expect(installMcpFromRegistry).toHaveBeenCalledWith("tok", "registry/github-mcp", "remote", {}, "");
    expect(onClose).toHaveBeenCalledWith(true);
  });
});

it("renders hits whose stars are null without crashing (unenriched catalog rows)", async () => {
  const user = userEvent.setup();
  const NULL_STARS_HIT: McpRegistryHit = {
    name: "unenriched-mcp",
    ref: "io.x/unenriched-mcp",
    registry: "github",
    kind: "local",
    description: "A server with no resolved star count",
    signals: { stars: null, owner_login: "x", verified: true }, // stars=null, not undefined
  };
  searchMcpRegistry.mockResolvedValue(result([NULL_STARS_HIT]));

  render(<McpDiscoverPane token="tok" onClose={vi.fn()} />);
  await user.type(screen.getByRole("textbox"), "unenriched");
  await user.click(screen.getByRole("button", { name: /search/i }));

  // Renders the row (no TypeError on null.toLocaleString) and shows no ★ for it
  expect(await screen.findByText("unenriched-mcp")).toBeInTheDocument();
  expect(screen.queryByText(/★/)).not.toBeInTheDocument();
});

it("offers OAuth by default when oauth-capable, hiding the token field, with a manual fallback", async () => {
  const user = userEvent.setup();
  describeMcpRegistryServer.mockResolvedValue({
    name: "acme", ref: "io.acme/srv", description: "", version: "1", repository: "",
    packages: [],
    remotes: [{
      transport_type: "streamable-http", url: "https://acme/mcp",
      headers: [{ name: "Authorization", description: "token", is_required: true, is_secret: true, default: null }],
    }],
  });
  mcpRegistryRuntime.mockResolvedValue({ kind: "remote", runtime: "", present: true, auto_installable: false, install_command: "" });
  mcpRegistryOauthCapability.mockResolvedValue({ oauth_capable: true });

  const ACME_HIT: McpRegistryHit = {
    name: "acme", ref: "io.acme/srv", registry: "github", kind: "remote", description: "",
    signals: { stars: 100, owner_login: "acme", verified: false },
  };
  searchMcpRegistry.mockResolvedValue(result([ACME_HIT]));

  render(<McpDiscoverPane token="t" onClose={() => {}} />);
  await user.type(screen.getByRole("textbox"), "acme");
  await user.click(screen.getByRole("button", { name: /search/i }));
  await user.click(await screen.findByText("acme"));

  // OAuth selected by default → the token field is hidden.
  await screen.findByText(/OAuth/i);
  expect(screen.queryByText(/Authorization/)).not.toBeInTheDocument();

  // Switch to manual token → field appears.
  await user.click(screen.getByText(/Manual token/i));
  expect(screen.getByText(/Authorization/)).toBeInTheDocument();
});

it("renders a credential help link from help_url", async () => {
  const user = userEvent.setup();
  describeMcpRegistryServer.mockResolvedValue({
    name: "gh", ref: "io.github.github/github-mcp-server", description: "", version: "1", repository: "",
    packages: [{
      registry_type: "oci", identifier: "x", version: "1", runtime_hint: "",
      transport_type: "stdio", runtime_arguments: [], package_arguments: [],
      env: [{ name: "GITHUB_PERSONAL_ACCESS_TOKEN", description: "Set an env var",
              is_required: true, is_secret: true, default: null,
              help_url: "https://github.com/settings/tokens" }],
    }],
    remotes: [],
  });
  mcpRegistryRuntime.mockResolvedValue({ kind: "local", runtime: "docker", present: true, auto_installable: false, install_command: "" });
  const GH_HIT: McpRegistryHit = {
    name: "gh", ref: "io.github.github/github-mcp-server", registry: "github", kind: "local", description: "",
    signals: { stars: 100, owner_login: "github", verified: true },
  };
  searchMcpRegistry.mockResolvedValue(result([GH_HIT]));
  render(<McpDiscoverPane token="t" onClose={() => {}} />);
  await user.type(screen.getByRole("textbox"), "github");
  await user.click(screen.getByRole("button", { name: /search/i }));
  await user.click(await screen.findByText("gh"));
  const link = await screen.findByRole("link", { name: /token|crear/i });
  expect(link).toHaveAttribute("href", "https://github.com/settings/tokens");
});

it("renders no credential help link when help_url is null", async () => {
  const user = userEvent.setup();
  describeMcpRegistryServer.mockResolvedValue({
    name: "gh", ref: "io.github.github/github-mcp-server", description: "", version: "1", repository: "",
    packages: [{
      registry_type: "oci", identifier: "x", version: "1", runtime_hint: "",
      transport_type: "stdio", runtime_arguments: [], package_arguments: [],
      env: [{ name: "GITHUB_PERSONAL_ACCESS_TOKEN", description: "Set an env var",
              is_required: true, is_secret: true, default: null,
              help_url: null }],
    }],
    remotes: [],
  });
  mcpRegistryRuntime.mockResolvedValue({ kind: "local", runtime: "docker", present: true, auto_installable: false, install_command: "" });
  const GH_HIT: McpRegistryHit = {
    name: "gh", ref: "io.github.github/github-mcp-server", registry: "github", kind: "local", description: "",
    signals: { stars: 100, owner_login: "github", verified: true },
  };
  searchMcpRegistry.mockResolvedValue(result([GH_HIT]));
  render(<McpDiscoverPane token="t" onClose={() => {}} />);
  await user.type(screen.getByRole("textbox"), "github");
  await user.click(screen.getByRole("button", { name: /search/i }));
  await user.click(await screen.findByText("gh"));
  await screen.findByText(/GITHUB_PERSONAL_ACCESS_TOKEN/);
  expect(screen.queryByRole("link", { name: /token|crear/i })).toBeNull();
});

it("sends auth_method=oauth when installing with OAuth selected", async () => {
  const user = userEvent.setup();
  describeMcpRegistryServer.mockResolvedValue({
    name: "acme", ref: "io.acme/srv", description: "", version: "1", repository: "",
    packages: [],
    remotes: [{ transport_type: "streamable-http", url: "https://acme/mcp",
      headers: [{ name: "Authorization", description: "token", is_required: true, is_secret: true, default: null }] }],
  });
  mcpRegistryRuntime.mockResolvedValue({ kind: "remote", runtime: "", present: true, auto_installable: false, install_command: "" });
  mcpRegistryOauthCapability.mockResolvedValue({ oauth_capable: true });
  installMcpFromRegistry.mockResolvedValue({});

  const ACME_HIT: McpRegistryHit = {
    name: "acme", ref: "io.acme/srv", registry: "github", kind: "remote", description: "",
    signals: { stars: 100, owner_login: "acme", verified: false },
  };
  searchMcpRegistry.mockResolvedValue(result([ACME_HIT]));

  render(<McpDiscoverPane token="t" onClose={() => {}} />);
  await user.type(screen.getByRole("textbox"), "acme");
  await user.click(screen.getByRole("button", { name: /search/i }));
  await user.click(await screen.findByText("acme"));
  await screen.findByText(/OAuth/i);
  await user.click(screen.getByRole("button", { name: /Connect/i }));
  await waitFor(() => expect(installMcpFromRegistry).toHaveBeenCalled());
  const call = installMcpFromRegistry.mock.calls[0];
  expect(call[4]).toBe("oauth"); // args: (token, ref, prefer, envValues, authMethod)
});

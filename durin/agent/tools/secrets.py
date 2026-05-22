"""Agent-facing secret tools — ``list_secrets`` and ``request_secret``.

The agent never receives a secret *value*. These tools let it:

* discover which credentials exist (`list_secrets`) — metadata only;
* ask the user to provide one it lacks (`request_secret`) — which
  yields, exactly like ``ask_user_question``: the agent presents the
  printed `durin secret set` command and the user runs it themselves,
  so the value goes straight to the store via the CLI.

Once stored, a secret whose ``scope`` includes ``exec`` is injected
into the shell subprocess environment (see ``ExecTool._build_env``), so
the agent uses it as ``$NAME`` without ever seeing the value.

See ``docs/11_secrets_design.md``.
"""

from __future__ import annotations

from typing import Any

from durin.agent.tools.base import Tool, tool_parameters
from durin.agent.tools.schema import StringSchema, tool_parameters_schema


@tool_parameters(tool_parameters_schema())
class ListSecretsTool(Tool):
    """List stored secrets (metadata only — never values)."""

    _scopes = {"core", "subagent"}

    @classmethod
    def create(cls, ctx: Any) -> Tool:
        return cls()

    @property
    def name(self) -> str:
        return "list_secrets"

    @property
    def description(self) -> str:
        return (
            "List the stored secrets available to you. Returns each "
            "secret's name, what it is for (service), scope, and "
            "description — NEVER the secret value. A secret whose scope "
            "includes 'exec' is available to your shell commands as an "
            "environment variable of the same name (e.g. $ATLASSIAN_WORK). "
            "Use this before request_secret to check what you already have."
        )

    @property
    def read_only(self) -> bool:
        return True

    async def execute(self, **kwargs: Any) -> str:
        from durin.security.secrets import get_secret_store

        entries = get_secret_store(reload=True).all()
        if not entries:
            return (
                "No secrets are stored. Use request_secret to ask the "
                "user to add one."
            )
        lines = ["Stored secrets (values never shown):"]
        for name, entry in sorted(entries.items()):
            scope = ", ".join(entry.scope) or "none"
            exec_hint = " — usable in exec as $" + name if "exec" in entry.scope else ""
            account = f" [{entry.account}]" if entry.account else ""
            desc = f" — {entry.description}" if entry.description else ""
            lines.append(
                f"  {name}{account}: service={entry.service}, "
                f"scope={scope}{desc}{exec_hint}"
            )
        return "\n".join(lines)


@tool_parameters(
    tool_parameters_schema(
        name=StringSchema(
            description="Secret name, UPPER_SNAKE (e.g. ATLASSIAN_API_TOKEN). "
            "Also the environment variable name your scripts will read.",
            min_length=1,
            max_length=128,
        ),
        service=StringSchema(
            description="What the secret is for — a short classifier like "
            "'atlassian', 'github', 'stripe'. Lets the secret be reused.",
            min_length=1,
            max_length=64,
        ),
        purpose=StringSchema(
            description="One line on why you need it and how it will be used.",
            min_length=1,
            max_length=400,
            nullable=True,
        ),
        required=["name", "service"],
    )
)
class RequestSecretTool(Tool):
    """Ask the user for a credential the agent needs but lacks."""

    _scopes = {"core", "subagent"}

    @classmethod
    def create(cls, ctx: Any) -> Tool:
        return cls()

    @property
    def name(self) -> str:
        return "request_secret"

    @property
    def description(self) -> str:
        return (
            "Request a credential you need but do not have. If a matching "
            "secret already exists you are told its name. Otherwise this "
            "YIELDS: present the printed `durin secret set` command and ask "
            "the user to run it, then stop. You never receive the value — "
            "once stored with the 'exec' scope it reaches your shell "
            "commands as an environment variable named after the secret."
        )

    @property
    def read_only(self) -> bool:
        return True

    async def execute(
        self,
        name: str | None = None,
        service: str | None = None,
        purpose: str | None = None,
        **kwargs: Any,
    ) -> str:
        from durin.security.secrets import get_secret_store, is_valid_secret_name

        name = (name or "").strip()
        service = (service or "").strip()
        if not name or not service:
            return "Error: both `name` and `service` are required."
        if not is_valid_secret_name(name):
            return (
                f"Error: '{name}' is not a valid secret name — use "
                "UPPER_SNAKE matching [A-Z][A-Z0-9_]*."
            )

        store = get_secret_store(reload=True)

        existing = store.get(name)
        if existing is not None:
            scope = ", ".join(existing.scope) or "none"
            usable = (
                f" It is exec-scoped, so use ${name} in your shell commands."
                if "exec" in existing.scope
                else " It is not exec-scoped; ask the user to run "
                f"`durin secret grant {name} --to exec` if you need it in exec."
            )
            return (
                f"Secret '{name}' already exists (service={existing.service}, "
                f"scope={scope}).{usable}"
            )

        same_service = [n for n in store.find_by_service(service) if n != name]
        if same_service:
            return (
                f"No secret named '{name}', but these already cover service "
                f"'{service}': {', '.join(same_service)}. Reuse one of those "
                f"(check list_secrets for its scope) instead of requesting a "
                f"new one — or, if you genuinely need a separate credential, "
                f"proceed with the request below.\n\n"
                + _request_block(name, service, purpose)
            )

        return _request_block(name, service, purpose)


def _request_block(name: str, service: str, purpose: str | None) -> str:
    """The YIELD message instructing the user to store the secret."""
    cmd = f"durin secret set {name} --service {service} --scope exec"
    reason = f"\nReason: {purpose.strip()}" if purpose else ""
    return (
        f"Secret '{name}' is not stored.{reason}\n\n"
        "YIELD TO USER. Present this exact instruction as your next "
        "assistant message, then STOP — do not call more tools:\n\n"
        f"  Please run this command and paste the secret at the hidden "
        f"prompt (it goes straight to durin's secret store — not to me, "
        f"and not into the chat):\n"
        f"    {cmd}\n\n"
        "After the user confirms they have run it, retry your task — the "
        f"secret will be available to shell commands as ${name}."
    )

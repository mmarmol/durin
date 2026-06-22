# Wiring channels

A **channel** is how durin receives messages and sends replies. Every channel
runs inside the gateway process. You can enable multiple channels at once —
each one listens independently on its platform.

Channels are configured under the `channels` key in `~/.durin/config.toml`.
Per-channel settings live as sub-tables (e.g. `[channels.telegram]`); each
channel adapter reads its own keys from that table.

> **Quick start.** Run `durin onboard channels` to get an interactive wizard
> that toggles channels on and off and prompts for the required credentials.

---

## Shared options

These keys apply across all channels. Set them at the top level of
`[channels]`:

| Key | Default | What it does |
|---|---|---|
| `send_progress` | `true` | Stream the agent's in-progress text to the channel while a turn is running |
| `send_tool_hints` | `false` | Also stream tool-call hints (e.g. "reading file …") as text |
| `show_reasoning` | `true` | Surface model reasoning when the channel implements it |
| `send_max_retries` | `3` | Total delivery attempts per message (initial send included) |
| `transcription_provider` | _(inherits)_ | Per-channel override for the transcription backend (e.g. `"groq"`, `"openai"`, `"local"`) |
| `transcription_language` | _(inherits)_ | Per-channel ISO 639-1 override (e.g. `"en"`) for the transcription engine |

Per-channel sections can override `send_progress`, `send_tool_hints`, and
`show_reasoning` by setting the same key inside the channel's table.

> **Primary transcription config.** `transcription_provider` and
> `transcription_language` in `[channels]` or a per-channel table are
> _per-channel overrides_. The global transcription backend — including the
> primary `provider` (default `"local"`) and its engine settings — is
> configured under the top-level `[transcription]` section. See the
> [configuration reference](configuration.md) for the full key list.

---

## Credentials and the secret store

Never put raw tokens or passwords in `config.toml`. Store them in durin's
encrypted secret store and reference them with `${secret:NAME}`:

```sh
durin secret set TELEGRAM_BOT_TOKEN
# then in config.toml:
# token = "${secret:TELEGRAM_BOT_TOKEN}"
```

The reference format is `${secret:NAME}` where `NAME` must match
`[A-Z][A-Z0-9_]*`. Secrets are resolved at gateway startup, so the plaintext
never lives in the shared config object.

---

## Access control and pairing

Every channel checks whether the incoming sender is allowed before forwarding
a message to the agent.

**`allow_from`** is a list of platform-specific sender IDs (Telegram numeric
IDs, Slack user IDs, email addresses, etc.). Set `["*"]` to allow anyone.

When `allow_from` is omitted or a sender is not on the list, durin enters
**pairing mode**: the unknown sender receives a time-limited code (valid for
10 minutes), and you approve or deny it from any active channel:

```
/pairing list                        # see pending codes
/pairing approve ABCD-EFGH           # grant access
/pairing deny ABCD-EFGH              # reject
/pairing revoke <user_id>            # remove an approved user from the current channel
/pairing revoke <channel> <user_id>  # remove an approved user from a specific channel
```

Approved senders persist in `~/.durin/pairing.json` across restarts.

---

## Web / dashboard (WebSocket)

The built-in dashboard and all browser-based clients connect via the
WebSocket channel.

```toml
[channels.websocket]
enabled = true
host = "127.0.0.1"      # bind address; use "0.0.0.0" only with a token set
port = 8765
path = "/"
token = "${secret:WEBUI_TOKEN}"          # static shared secret (optional)
token_issue_secret = ""                  # reverse-proxy auth (optional)
websocket_requires_token = true
streaming = true
```

**Key fields:**

| Key | Default | Notes |
|---|---|---|
| `host` | `127.0.0.1` | Binding to `0.0.0.0` or `::` requires either `token` or `token_issue_secret` to be set |
| `port` | `8765` | WebSocket listen port |
| `path` | `"/"` | URL path prefix |
| `token` | _(empty)_ | Static secret; clients pass it as `?token=…` |
| `token_issue_secret` | _(empty)_ | Bearer secret for `GET /webui/bootstrap`; used with reverse proxies |
| `websocket_requires_token` | `true` | Reject connections that present no valid token |
| `streaming` | `true` | Send incremental text deltas while the model is generating |
| `ssl_certfile` / `ssl_keyfile` | _(empty)_ | Paths to TLS certificate and key for direct TLS |
| `allow_from` | `["*"]` | Client IDs that may connect |

Open the dashboard at `http://127.0.0.1:8765` (or the configured host/port)
after starting the gateway.

---

## Telegram

Telegram uses **long polling** — no public IP or webhook is required.

```toml
[channels.telegram]
enabled = true
token = "${secret:TELEGRAM_BOT_TOKEN}"
allow_from = []          # leave empty to use pairing, or list numeric user IDs
group_policy = "mention" # "open" or "mention"
streaming = true
```

**How to get a token:**

1. Open Telegram and start a chat with `@BotFather`.
2. Send `/newbot` and follow the prompts.
3. Copy the token and store it: `durin secret set TELEGRAM_BOT_TOKEN`.

**Key fields (from `TelegramConfig`):**

| Key | Default | Notes |
|---|---|---|
| `token` | _(required)_ | Bot API token from BotFather |
| `allow_from` | `[]` | Telegram user IDs or usernames; empty = pairing mode for DMs |
| `group_policy` | `"mention"` | `"open"` (reply to all) or `"mention"` (reply only when @-mentioned) |
| `proxy` | _(none)_ | HTTP proxy URL for outbound connections |
| `reply_to_message` | `false` | Quote the original message in replies |
| `react_emoji` | `"👀"` | Reaction added while processing |
| `streaming` | `true` | Edit the message in-place as the model streams |
| `inline_keyboards` | `false` | Render choice buttons as inline keyboards |

**Pairing in Telegram:** when `allow_from` is empty, any new user who DMs the
bot receives a pairing code. The owner approves it with `/pairing approve
<code>` in any active channel.

---

## Slack

Slack uses **Socket Mode** — no public URL is needed.

```toml
[channels.slack]
enabled = true
bot_token = "${secret:SLACK_BOT_TOKEN}"
app_token = "${secret:SLACK_APP_TOKEN}"
allow_from = []           # leave empty for pairing on DMs
group_policy = "mention"  # "open", "mention", or "allowlist"
```

**How to create a Slack app:**

1. Go to [api.slack.com/apps](https://api.slack.com/apps) and create a new
   app from scratch in your workspace.
2. Under **Socket Mode**, enable it and generate an **App-Level Token** with
   the `connections:write` scope. This is your `app_token`.
3. Under **OAuth & Permissions**, add the bot scopes your use case needs
   (at minimum: `app_mentions:read`, `chat:write`, `im:history`,
   `im:read`, `im:write`, `reactions:write`). Install the app to get the
   **Bot User OAuth Token**. This is your `bot_token`.
4. Under **Event Subscriptions**, enable events and subscribe to
   `message.im` and `app_mention` bot events.
5. Store the tokens:
   ```sh
   durin secret set SLACK_BOT_TOKEN
   durin secret set SLACK_APP_TOKEN
   ```

> Slack and Discord require optional pip extras. If the extra is missing,
> durin logs a restart note after installing it automatically.

**Key fields (from `SlackConfig`):**

| Key | Default | Notes |
|---|---|---|
| `bot_token` | _(required)_ | `xoxb-…` Bot User OAuth Token |
| `app_token` | _(required)_ | `xapp-…` App-Level Token (Socket Mode) |
| `allow_from` | `[]` | Slack user IDs; empty = pairing mode for DMs |
| `group_policy` | `"mention"` | `"open"`, `"mention"`, or `"allowlist"` |
| `group_allow_from` | `[]` | Channel IDs allowed when `group_policy = "allowlist"` |
| `reply_in_thread` | `true` | Reply in the originating thread |
| `react_emoji` | `"eyes"` | Reaction added while processing |
| `done_emoji` | `"white_check_mark"` | Reaction added when done |
| `include_thread_context` | `true` | Prepend thread history on first mention in a thread |
| `thread_context_limit` | `20` | Max messages of thread context to include |
| `dm.enabled` | `true` | Accept direct messages |
| `dm.policy` | `"open"` | `"open"` or `"allowlist"` for DM access |

---

## Email

Email uses **IMAP polling** for inbound and **SMTP** for outbound.

```toml
[channels.email]
enabled = true
consent_granted = true   # must be explicitly set to true

imap_host = "imap.example.com"
imap_port = 993
imap_username = "agent@example.com"
imap_password = "${secret:EMAIL_IMAP_PASSWORD}"
imap_mailbox = "INBOX"
imap_use_ssl = true

smtp_host = "smtp.example.com"
smtp_port = 587
smtp_username = "agent@example.com"
smtp_password = "${secret:EMAIL_SMTP_PASSWORD}"
smtp_use_tls = true
from_address = "agent@example.com"

allow_from = ["trusted@example.com"]
```

**Key fields (from `EmailConfig`):**

| Key | Default | Notes |
|---|---|---|
| `consent_granted` | `false` | Must be `true` or the channel will not start |
| `imap_host` / `imap_port` | _(required)_ | IMAP server and port (default 993) |
| `imap_username` / `imap_password` | _(required)_ | IMAP credentials |
| `imap_mailbox` | `"INBOX"` | Mailbox to poll |
| `imap_use_ssl` | `true` | Use SSL/TLS for IMAP |
| `smtp_host` / `smtp_port` | _(required)_ | SMTP server and port (default 587) |
| `smtp_username` / `smtp_password` | _(required)_ | SMTP credentials |
| `smtp_use_tls` | `true` | Use STARTTLS |
| `smtp_use_ssl` | `false` | Use direct SSL (mutually exclusive with `smtp_use_tls`) |
| `from_address` | _(required)_ | The `From:` address on replies |
| `allow_from` | `[]` | Allowed sender addresses (glob patterns supported) |
| `poll_interval_seconds` | `30` | How often to poll IMAP (minimum 5 s) |
| `verify_dkim` | `true` | Require `dkim=pass` in `Authentication-Results` |
| `verify_spf` | `true` | Require `spf=pass` in `Authentication-Results` |
| `allowed_attachment_types` | `[]` | MIME types to accept (e.g. `["image/*", "application/pdf"]`); empty = no attachments |
| `max_body_chars` | `12000` | Truncate message body beyond this length |

> The `consent_granted` flag is a deliberate gate: the email channel reads
> your mailbox and replies on your behalf. Set it to `true` only after you
> have reviewed and accepted that behaviour.

---

## Remaining channels

The following built-in channel adapters are included in durin. Each one reads
its own config keys from its `__init__`. Consult the source module in
`durin/channels/<name>.py` or run `durin onboard channels` to configure them
interactively.

| Channel | Module | Notes |
|---|---|---|
| Discord | `discord.py` | Requires the `discord` pip extra (auto-installed) |
| WhatsApp | `whatsapp.py` | |
| Matrix | `matrix.py` | |
| Microsoft Teams | `msteams.py` | |
| Feishu | `feishu.py` | |
| DingTalk | `dingtalk.py` | |
| WeCom | `wecom.py` | |
| Weixin | `weixin.py` | |
| QQ | `qq.py` | |
| MoChat | `mochat.py` | |

All of them follow the same pattern: add an `enabled = true` key under
`[channels.<name>]`, supply the credentials as `${secret:…}` references, and
optionally list `allow_from` IDs. See the module's config class for the exact
key names.

---

## Writing a channel plugin

External channel adapters can be distributed as Python packages and are
discovered automatically via the `durin.channels` entry point group. Declare
your entry point in `pyproject.toml`:

```toml
[project.entry-points."durin.channels"]
myplatform = "mypkg.myplatform:MyPlatformChannel"
```

The exported class must subclass `durin.channels.base.BaseChannel`. Built-in
channels take priority over external plugins with the same name.

See [docs/internals/channels.md](../internals/channels.md) for the full
architecture, message bus contract, and streaming protocol.

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

Channels can also carry their own identity: a `persona` key on any channel
section makes sessions born there use that persona (and the model it pins)
instead of the global default — e.g. a work persona on Slack and a personal
one on Telegram. Precedence: cron-job override > persona picked in the
conversation > per-chat mapping > channel default > global default.

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
WebSocket channel. This channel is **always on while the dashboard is
enabled** (`gateway.webui_enabled = true`) — there is no separate enable
toggle for it.

The dashboard itself authenticates via short-lived bootstrap tokens issued
at page load; it never uses the static `token` field. The `token` field is
only needed for **external WebSocket clients** (scripts, integrations) that
connect without going through the dashboard bootstrap flow. When set, store
it as a durin secret and reference it with `${secret:…}`.

```toml
[channels.websocket]
host = "127.0.0.1"      # bind address; use "0.0.0.0" only with a token set
port = 8765
path = "/"
token = "${secret:WEBUI_TOKEN}"          # optional; external clients only
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
| `token` | _(empty)_ | Optional static secret for external clients; stored as a durin secret |
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

The bot token is **always stored as a durin secret**, never as plaintext in
`config.toml`. Both the guided and manual setup paths write a `${secret:…}`
reference into the config automatically.

### Guided setup (recommended)

Open the dashboard **Channels** tab, expand the Telegram section, and click
**Set up Telegram**. The panel walks you through:

1. **Create a bot** — follow the link to
   [t.me/BotFather](https://t.me/BotFather), send `/newbot`, and copy the
   token BotFather gives you.
2. **Validate** — paste the token into the panel and click **Test**. durin
   calls the Telegram `getMe` API to confirm the token is valid and shows
   the bot's username. Nothing is written at this step.
3. **Save** — click **Connect**. The token is saved to the secret store and
   the config is updated to reference it as `${secret:TELEGRAM_BOT_TOKEN}`.
   The gateway picks up the change on its next reload.

Once connected, any Telegram user who DMs the bot triggers **pairing mode**
unless their numeric user ID is already in `allow_from`. The dashboard
displays pending pairing requests; approve or deny them there, or from any
active channel:

```
/pairing list
/pairing approve ABCD-EFGH
/pairing deny ABCD-EFGH
```

### Slash commands

Telegram's bot command menu (the `/` picker in the Telegram client) is
generated from the same built-in command registry that drives the WebUI
palette and TUI autocomplete, scoped to the commands listed for channels.
Any slash text the menu doesn't cover is still forwarded to durin like a
normal message, so it reaches the agent instead of being dropped.

### Manual setup

Store the token in the secret store first, then write the config entry
referencing it:

```sh
durin secret set TELEGRAM_BOT_TOKEN
```

```toml
[channels.telegram]
enabled = true
token = "${secret:TELEGRAM_BOT_TOKEN}"
allow_from = []          # leave empty to use pairing, or list numeric user IDs
group_policy = "mention" # "open" or "mention"
streaming = true
```

Set `allow_from` to a list of numeric Telegram user IDs to grant access
without pairing, or leave it empty to require pairing for every new user.

**Key fields (from `TelegramConfig`):**

| Key | Default | Notes |
|---|---|---|
| `token` | _(required)_ | Bot API token from BotFather; always stored as a durin secret |
| `allow_from` | `[]` | Telegram user IDs or usernames; empty = pairing mode for DMs. Entries are bare numeric IDs (`"123456"`) or `"<id>\|<username>"` (written by the pairing flow). The numeric ID is permanent; the username part can go stale if the user renames their account. |
| `group_policy` | `"mention"` | `"open"` (reply to all) or `"mention"` (reply only when @-mentioned) |
| `proxy` | _(none)_ | HTTP proxy URL for outbound connections |
| `reply_to_message` | `false` | Quote the original message in replies |
| `react_emoji` | `"👀"` | Reaction added while processing |
| `streaming` | `true` | Edit the message in-place as the model streams |
| `inline_keyboards` | `false` | Render choice buttons as inline keyboards |
| `drop_pending_updates` | `true` | Drop messages queued while the bot was offline. Set to `false` to replay them on (re)start. |

---

## Slack

Slack uses **Socket Mode** — no public URL is needed. The channel reconnects
automatically when the WebSocket drops, deduplicates events that Slack
redelivers after a reconnect, and retries Web API calls on rate limits.

**Guided setup (recommended):** open the webui **Channels** tab → Slack. The
guided mode walks through the whole flow: it generates an **app manifest**
(all bot scopes, event subscriptions, and Socket Mode pre-configured) to paste
at [api.slack.com/apps](https://api.slack.com/apps?new_app=1) via
*Create from a manifest*, validates both tokens live, stores them as durin
secrets, and enables the channel. Once active, the same panel manages DM
pairing (approve/deny/revoke senders) and lets you join the bot to public
workspace channels directly (private channels still need a manual
`/invite @bot` from inside Slack). A *Manual* toggle exposes every config
field for advanced setups; both modes write the same config keys.

**Manual setup:**

```toml
[channels.slack]
enabled = true
bot_token = "${secret:SLACK_BOT_TOKEN}"
app_token = "${secret:SLACK_APP_TOKEN}"
allow_from = []           # leave empty for pairing on DMs
group_policy = "mention"  # "open", "mention", or "allowlist"
```

1. Fetch the app manifest from a running gateway
   (`GET /api/v1/channels/slack/manifest`) or build the app by hand at
   [api.slack.com/apps](https://api.slack.com/apps): enable **Socket Mode**,
   subscribe to the `app_mention` and `message.*` bot events, grant the
   bot scopes the channel uses (`app_mentions:read`, `chat:write`,
   `im:history`, `im:read`, `im:write`, `files:read`, `files:write`,
   `reactions:write`, `channels:history`, `channels:read`, `groups:history`,
   `groups:read`, `mpim:history`, `mpim:read`, `users:read`), and under
   **App Home → Messages Tab** allow users to send messages — without it
   Slack blocks all DMs to the bot.
2. Under **Basic Information → App-Level Tokens**, generate a token with the
   `connections:write` scope. This is your `app_token`.
3. Under **Install App**, install to your workspace and copy the
   **Bot User OAuth Token**. This is your `bot_token`.
4. Store the tokens:
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
| `allow_from` | `[]` | Slack user IDs (`U…`) allowed to talk to durin; `["*"]` allows anyone. Empty = pairing mode: unapproved DM senders receive a pairing code, unapproved channel senders are ignored. |
| `dm_enabled` | `true` | Listen to direct messages at all |
| `group_policy` | `"mention"` | `"open"`, `"mention"`, or `"allowlist"` |
| `group_allow_from` | `[]` | Channel IDs allowed when `group_policy = "allowlist"` |
| `open_channels` | `[]` | Channels where the bot replies to every message, regardless of `group_policy` — mix open rooms with mention-only rooms |
| `persona` | `""` | Default persona for sessions born on this channel (empty = global default) |
| `chat_personas` | `{}` | Per-conversation persona overrides (`{"C0123": "ops"}`); managed from the guided panel's channel list |
| `reply_in_thread` | `true` | Reply in the originating thread |
| `react_emoji` | `"eyes"` | Reaction added while processing |
| `done_emoji` | `"white_check_mark"` | Reaction added when done |
| `include_thread_context` | `true` | Prepend thread history on first mention in a thread |
| `thread_context_limit` | `20` | Max messages of thread context to include |
| `streaming` | `true` | Stream replies by editing the message in place as the model writes |
| `stream_edit_interval` | `1.2` | Min seconds between streaming edits (chat.update is rate-limited) |
| `thread_auto_follow` | `true` | After a mention in a channel thread, answer follow-ups there without a re-mention |

Sender authorization is enforced at durin's central inbound gate, the same as
every other channel: approve a sender by adding their Slack user ID to
`allow_from` or by completing the pairing exchange durin starts in the DM.

Both routing and identity can be tuned per workspace channel from the guided
panel's channel list: a *Reply to all* toggle (writes `open_channels`) and a
persona dropdown (writes `chat_personas`).

Content quoted or forwarded with Slack's *Share message* — which Slack omits
from the plain message text — is extracted from the rich-text blocks and
attachments and passed to the agent as `[quoted]` / `[shared]` context lines.

---

## Email

Email uses **IMAP polling** for inbound and **SMTP** for outbound. The full
channel can be configured from the webui **Channels** tab without editing
`config.toml` directly.

The IMAP and SMTP passwords are stored as durin secrets. When configuring
via the webui, the password fields save directly to the secret store and
write `${secret:…}` references into the channel config. When configuring
manually, set the secrets first and reference them:

```sh
durin secret set EMAIL_IMAP_PASSWORD
durin secret set EMAIL_SMTP_PASSWORD
```

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
| `imap_username` / `imap_password` | _(required)_ | IMAP credentials; password stored as a durin secret |
| `imap_mailbox` | `"INBOX"` | Mailbox to poll |
| `imap_use_ssl` | `true` | Use SSL/TLS for IMAP |
| `smtp_host` / `smtp_port` | _(required)_ | SMTP server and port (default 587) |
| `smtp_username` / `smtp_password` | _(required)_ | SMTP credentials; password stored as a durin secret |
| `smtp_use_tls` | `true` | Use STARTTLS |
| `smtp_use_ssl` | `false` | Use direct SSL (mutually exclusive with `smtp_use_tls`) |
| `from_address` | _(required)_ | The `From:` address on replies |
| `allow_from` | `[]` | Allowed sender addresses (glob patterns supported); must be set for the channel to authorize mail |
| `poll_interval_seconds` | `30` | How often to poll IMAP (minimum 5 s) |
| `verify_dkim` | `true` | Require `dkim=pass` in `Authentication-Results` |
| `verify_spf` | `true` | Require `spf=pass` in `Authentication-Results` |
| `allowed_attachment_types` | `[]` | MIME types to accept (e.g. `["image/*", "application/pdf"]`); empty = no attachments |
| `max_body_chars` | `12000` | Truncate message body beyond this length |

> The `consent_granted` flag is a deliberate gate: the email channel reads
> your mailbox and replies on your behalf. Set it to `true` only after you
> have reviewed and accepted that behaviour. Both `consent_granted` and a
> non-empty `allow_from` list must be set for the channel to receive and
> authorize mail.

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

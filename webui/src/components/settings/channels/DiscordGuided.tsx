import { useCallback, useEffect, useRef, useState, type ReactNode } from "react";
import { Check, ChevronDown, Copy, ExternalLink, Hash, Loader2, UserCheck, X } from "lucide-react";
import { useTranslation } from "react-i18next";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { useClient } from "@/providers/ClientProvider";
import {
  setConfigValue,
  startChannel,
  testDiscordToken,
  getDiscordPairing,
  approveDiscordPairing,
  denyDiscordPairing,
  revokeDiscordPairing,
  getDiscordGuilds,
  getDiscordInvite,
  getChannelsRuntime,
  type ChannelInfo,
  type DiscordGuildEntry,
  type DiscordInviteResult,
  type DiscordPairing,
} from "@/lib/api";

// Mirrors DiscordService.INVITE_PERMISSIONS / INVITE_SCOPES in
// durin/service/channels_discord.py (View Channels, Send Messages, Send
// Messages in Threads, Embed Links, Attach Files, Read Message History, Add
// Reactions — never Administrator). Used only before the token is saved,
// when GET /discord/invite has nothing configured to read yet; once the
// token is persisted the connected panel calls that endpoint instead.

const AUTO_VALIDATE_DEBOUNCE_MS = 700;

// ---------- shared bits ------------------------------------------------------

function useDiscordErrorLabel() {
  const { t } = useTranslation();
  return (code: string | null): string | null => {
    if (!code) return null;
    return t(`settings.channels.discord.errors.${code}`, code);
  };
}

function ModeSwitch({
  mode,
  onChange,
}: {
  mode: "guided" | "manual";
  onChange: (m: "guided" | "manual") => void;
}) {
  const { t } = useTranslation();
  return (
    <div className="inline-flex h-8 items-center rounded-full bg-muted p-0.5 text-[12px] font-medium text-muted-foreground">
      <button
        type="button"
        onClick={() => onChange("guided")}
        className={
          "rounded-full px-3 py-1 transition-colors " +
          (mode === "guided"
            ? "bg-background text-foreground shadow-sm"
            : "hover:text-foreground")
        }
      >
        {t("settings.channels.discord.modeGuided")}
      </button>
      <button
        type="button"
        onClick={() => onChange("manual")}
        className={
          "rounded-full px-3 py-1 transition-colors " +
          (mode === "manual"
            ? "bg-background text-foreground shadow-sm"
            : "hover:text-foreground")
        }
      >
        {t("settings.channels.discord.modeManual")}
      </button>
    </div>
  );
}

function StepHeader({ number, done, title }: { number: number; done: boolean; title: string }) {
  return (
    <div className="flex items-center gap-2">
      <span
        className={
          "grid h-5 w-5 shrink-0 place-items-center rounded-full text-[11px] font-semibold " +
          (done
            ? "bg-emerald-500/15 text-emerald-600 dark:text-emerald-400"
            : "bg-muted text-muted-foreground")
        }
      >
        {done ? <Check className="h-3 w-3" /> : number}
      </span>
      <span className="text-[12px] font-semibold text-foreground/80">{title}</span>
    </div>
  );
}

/** message_content_intent -> symptom-first chip. Never surfaces the words
 *  "intent" or "bitfield" — those are Discord portal jargon, not a user
 *  symptom. */
function PermissionBadge({ state }: { state: string }) {
  const { t } = useTranslation();
  const cls =
    state === "enabled"
      ? "bg-emerald-500/12 text-emerald-600 dark:text-emerald-400"
      : state === "limited"
      ? "bg-amber-500/12 text-amber-600 dark:text-amber-400"
      : state === "disabled"
      ? "bg-destructive/12 text-destructive"
      : "bg-muted text-muted-foreground";
  const key =
    state === "enabled"
      ? "permissionEnabled"
      : state === "limited"
      ? "permissionLimited"
      : state === "disabled"
      ? "permissionDisabled"
      : "permissionUnknown";
  return (
    <span
      className={`inline-flex shrink-0 items-center gap-1.5 rounded-full px-2.5 py-0.5 text-[11px] ${cls}`}
    >
      <span className="h-1.5 w-1.5 rounded-full bg-current" aria-hidden />
      {t(`settings.channels.discord.${key}`)}
    </span>
  );
}

function PermissionHint({ state }: { state: string }) {
  const { t } = useTranslation();
  const key =
    state === "limited"
      ? "permissionLimitedHint"
      : state === "disabled"
      ? "permissionDisabledHint"
      : state === "unknown"
      ? "permissionUnknownHint"
      : "permissionEnabledHint";
  return <>{t(`settings.channels.discord.${key}`)}</>;
}

/** Amber for a soft warning (limited/unknown), destructive for the state
 *  that actually breaks reading (disabled). */
function severityBoxClass(state: string): string {
  if (state === "disabled") return "border-destructive/30 bg-destructive/5 text-destructive";
  return "border-amber-500/30 bg-amber-500/10 text-amber-700 dark:text-amber-400";
}

// ---------- pairing panel ----------------------------------------------------

function PairingPanel({ token }: { token: string }) {
  const { t } = useTranslation();
  const [pairing, setPairing] = useState<DiscordPairing | null>(null);
  const [actioning, setActioning] = useState<string | null>(null);

  const fetchPairing = useCallback(async () => {
    try {
      const data = await getDiscordPairing(token);
      setPairing(data);
    } catch {
      // ignore fetch errors; we'll retry on the next interval tick
    }
  }, [token]);

  useEffect(() => {
    void fetchPairing();
    const id = setInterval(() => void fetchPairing(), 5000);
    return () => clearInterval(id);
  }, [fetchPairing]);

  const act = useCallback(
    async (action: () => Promise<unknown>, key: string) => {
      setActioning(key);
      try {
        await action();
        await fetchPairing();
      } finally {
        setActioning(null);
      }
    },
    [fetchPairing],
  );

  return (
    <div className="mt-4 space-y-1.5">
      <div className="text-[11px] font-semibold uppercase tracking-wide text-muted-foreground">
        {t("settings.channels.discord.pairingTitle")}
      </div>
      <p className="text-[12px] text-muted-foreground">
        {t("settings.channels.discord.pairingInstruction")}
      </p>

      {pairing && pairing.pending.length > 0 ? (
        <div className="space-y-1.5">
          <div className="text-[11px] font-semibold uppercase tracking-wide text-muted-foreground">
            {t("settings.channels.discord.pendingRequests")}
          </div>
          {pairing.pending.map((req) => (
            <div
              key={req.code}
              className="flex flex-wrap items-center gap-2 rounded-xl border border-border/40 bg-muted/30 px-3 py-2 text-[13px]"
            >
              <span className="flex-1 text-[13px]">
                {pairing?.names?.[req.sender_id] ?? req.sender_id}
                {pairing?.names?.[req.sender_id] ? (
                  <span className="ml-2 font-mono text-[11px] text-muted-foreground">
                    {req.sender_id}
                  </span>
                ) : null}
              </span>
              <span className="text-[11px] text-muted-foreground">#{req.code}</span>
              <Button
                size="sm"
                variant="outline"
                disabled={actioning !== null}
                onClick={() =>
                  act(() => approveDiscordPairing(token, req.code), `approve-${req.code}`)
                }
                className="h-7 rounded-full px-2.5 text-[12px]"
              >
                <Check className="mr-1 h-3.5 w-3.5" />
                {t("settings.channels.discord.approve")}
              </Button>
              <Button
                size="sm"
                variant="ghost"
                disabled={actioning !== null}
                onClick={() =>
                  act(() => denyDiscordPairing(token, req.code), `deny-${req.code}`)
                }
                className="h-7 rounded-full px-2.5 text-[12px] text-muted-foreground"
              >
                <X className="mr-1 h-3.5 w-3.5" />
                {t("settings.channels.discord.deny")}
              </Button>
            </div>
          ))}
        </div>
      ) : null}

      {pairing && pairing.approved.length > 0 ? (
        <div className="space-y-1.5">
          <div className="text-[11px] font-semibold uppercase tracking-wide text-muted-foreground">
            {t("settings.channels.discord.approvedSenders")}
          </div>
          {pairing.approved.map((senderId) => (
            <div
              key={senderId}
              className="flex flex-wrap items-center gap-2 rounded-xl border border-border/40 bg-muted/30 px-3 py-2 text-[13px]"
            >
              <UserCheck className="h-3.5 w-3.5 shrink-0 text-emerald-500" />
              <span className="flex-1 text-[13px]">
                {pairing?.names?.[senderId] ?? senderId}
                {pairing?.names?.[senderId] ? (
                  <span className="ml-2 font-mono text-[11px] text-muted-foreground">
                    {senderId}
                  </span>
                ) : null}
              </span>
              <Button
                size="sm"
                variant="ghost"
                disabled={actioning !== null}
                onClick={() =>
                  act(() => revokeDiscordPairing(token, senderId), `revoke-${senderId}`)
                }
                className="h-7 rounded-full px-2.5 text-[12px] text-muted-foreground"
              >
                {t("settings.channels.discord.revoke")}
              </Button>
            </div>
          ))}
        </div>
      ) : null}

      {pairing && pairing.pending.length === 0 && pairing.approved.length === 0 ? (
        <p className="text-[12px] text-muted-foreground">
          {t("settings.channels.discord.noPairings")}
        </p>
      ) : null}
    </div>
  );
}

// ---------- scope panel: where durin answers ---------------------------------

/** `allow_channels: []` means "answer everywhere" — an honest empty state,
 *  not "nothing picked yet". Ticking one box turns it into a closed
 *  allowlist that silently mutes every other channel in every server, so
 *  this is a radio pair (replace-the-whole-set semantics), not a checkbox
 *  list (which implies additive semantics). */
function ScopePanel({
  token,
  channelValues,
  onChanged,
  invite,
}: {
  token: string;
  channelValues: Record<string, unknown>;
  onChanged: () => void;
  invite: DiscordInviteResult | null;
}) {
  const { t } = useTranslation();
  const errorLabel = useDiscordErrorLabel();

  const allowChannels: string[] = Array.isArray(channelValues["allow_channels"])
    ? (channelValues["allow_channels"] as string[])
    : [];

  // The radio reflects user INTENT, not just the persisted array: choosing
  // "only" must reveal the channel list even when allow_channels is still
  // empty (nothing ticked yet). Seeded once from the persisted value; the
  // accordion remounts this panel fresh each time it's reopened.
  const [intent, setIntent] = useState<"all" | "only">(
    allowChannels.length > 0 ? "only" : "all",
  );

  const [guilds, setGuilds] = useState<DiscordGuildEntry[] | null>(null);
  const [guildsError, setGuildsError] = useState<string | null>(null);
  const [openGuilds, setOpenGuilds] = useState<Set<string>>(new Set());
  const [filter, setFilter] = useState("");
  const [savingId, setSavingId] = useState<string | null>(null);

  const fetchGuilds = useCallback(async () => {
    try {
      const data = await getDiscordGuilds(token);
      if (data.ok) {
        setGuilds(data.guilds);
        setGuildsError(null);
      } else {
        setGuildsError(data.error ?? "unknown");
      }
    } catch {
      setGuildsError("unknown");
    }
  }, [token]);

  // Only fetched once the operator asks to see channels — most setups leave
  // this on "everywhere" and never need the guild/channel list at all.
  useEffect(() => {
    if (intent === "only" && guilds === null && guildsError === null) void fetchGuilds();
  }, [intent, guilds, guildsError, fetchGuilds]);

  const chooseAll = async () => {
    setIntent("all");
    await setConfigValue(token, "channels.discord.allow_channels", []);
    onChanged();
  };

  const toggleChannel = async (channelId: string, checked: boolean) => {
    setSavingId(channelId);
    try {
      const next = checked
        ? Array.from(new Set([...allowChannels, channelId]))
        : allowChannels.filter((id) => id !== channelId);
      await setConfigValue(token, "channels.discord.allow_channels", next);
      onChanged();
    } finally {
      setSavingId(null);
    }
  };

  const toggleGuildOpen = (id: string) => {
    setOpenGuilds((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  };

  return (
    <div className="mt-4 space-y-1.5">
      <div className="text-[11px] font-semibold uppercase tracking-wide text-muted-foreground">
        {t("settings.channels.discord.scopeTitle")}
      </div>
      <label className="flex items-center gap-2 text-[13px] text-foreground/80">
        <input
          type="radio"
          name="discord-scope"
          checked={intent === "all"}
          onChange={() => void chooseAll()}
        />
        {t("settings.channels.discord.scopeAll")}
      </label>
      <label className="flex items-center gap-2 text-[13px] text-foreground/80">
        <input
          type="radio"
          name="discord-scope"
          checked={intent === "only"}
          onChange={() => setIntent("only")}
        />
        {t("settings.channels.discord.scopeOnly")}
      </label>

      {intent === "only" ? (
        <div className="mt-1.5 space-y-2 pl-6">
          {allowChannels.length === 0 ? (
            <p className="text-[12px] text-amber-600 dark:text-amber-400">
              {t("settings.channels.discord.scopeOnlyWarning")}
            </p>
          ) : null}

          {guildsError ? (
            <p className="text-[12px] text-amber-600 dark:text-amber-400">
              {errorLabel(guildsError)}
            </p>
          ) : null}

          {guilds === null && !guildsError ? (
            <p className="text-[12px] text-muted-foreground">{t("settings.status.loading")}</p>
          ) : null}

          {guilds && guilds.length === 0 ? (
            <div className="space-y-1.5">
              <p className="text-[12px] text-muted-foreground">
                {t("settings.channels.discord.noGuilds")}
              </p>
              {invite?.ok && invite.url ? (
                <a
                  href={invite.url}
                  target="_blank"
                  rel="noreferrer"
                  className="inline-flex items-center gap-1.5 rounded-full border border-border/60 bg-background px-3 py-1 text-[12px] font-medium text-foreground hover:bg-muted/50 transition-colors"
                >
                  <ExternalLink className="h-3.5 w-3.5" />
                  {t("settings.channels.discord.openInDiscord")}
                </a>
              ) : null}
            </div>
          ) : null}

          {guilds && guilds.length > 0 ? (
            <div className="space-y-2">
              <Input
                value={filter}
                onChange={(e) => setFilter(e.target.value)}
                placeholder={t("settings.channels.discord.searchChannels")}
                className="w-[240px]"
              />
              {guilds.map((guild) => {
                const filtered = filter
                  ? guild.channels.filter((c) =>
                      c.name.toLowerCase().includes(filter.toLowerCase()),
                    )
                  : guild.channels;
                if (filter && filtered.length === 0) return null;
                const isOpen = openGuilds.has(guild.id) || Boolean(filter);
                const selectedCount = guild.channels.filter((c) =>
                  allowChannels.includes(c.id),
                ).length;
                return (
                  <div key={guild.id} className="rounded-xl border border-border/40">
                    <button
                      type="button"
                      onClick={() => toggleGuildOpen(guild.id)}
                      aria-expanded={isOpen}
                      className="flex w-full items-center justify-between gap-2 px-3 py-2 text-left text-[12px] font-medium text-foreground/80"
                    >
                      <span className="flex items-center gap-1.5">
                        <ChevronDown
                          className={`h-3.5 w-3.5 transition-transform ${isOpen ? "" : "-rotate-90"}`}
                          aria-hidden
                        />
                        {guild.name}
                      </span>
                      <span className="text-[11px] font-normal text-muted-foreground">
                        {t("settings.channels.discord.guildSelectedCount", {
                          n: selectedCount,
                          m: guild.channels.length,
                        })}
                      </span>
                    </button>
                    {isOpen ? (
                      <div className="space-y-1 px-3 pb-2">
                        {filtered.map((c) => (
                          <label
                            key={c.id}
                            className="flex items-center gap-1.5 text-[12px] text-foreground/80"
                          >
                            <input
                              type="checkbox"
                              checked={allowChannels.includes(c.id)}
                              disabled={savingId !== null}
                              onChange={(e) => void toggleChannel(c.id, e.target.checked)}
                            />
                            <Hash className="h-3.5 w-3.5 shrink-0 text-muted-foreground" />
                            {c.name}
                            {c.type === 15 ? (
                              <span className="rounded-full bg-muted px-1.5 py-0.5 text-[10px] text-muted-foreground">
                                {t("settings.channels.discord.forumBadge")}
                              </span>
                            ) : null}
                          </label>
                        ))}
                      </div>
                    ) : null}
                  </div>
                );
              })}
            </div>
          ) : null}
        </div>
      ) : null}
    </div>
  );
}

// ---------- connected panel --------------------------------------------------

function ConnectedPanel({
  token,
  channelValues,
  onChanged,
  onReplaceToken,
}: {
  token: string;
  channelValues: Record<string, unknown>;
  onChanged: () => void;
  onReplaceToken: () => void;
}) {
  const { t } = useTranslation();
  const errorLabel = useDiscordErrorLabel();
  const [status, setStatus] = useState<
    | { state: "checking" }
    | { state: "ok"; bot_user: string | null; intent: string }
    | { state: "error"; code: string }
  >({ state: "checking" });
  const [running, setRunning] = useState<boolean | null>(null);
  const [starting, setStarting] = useState(false);
  const [startError, setStartError] = useState<string | null>(null);
  const [invite, setInvite] = useState<DiscordInviteResult | null>(null);

  const fetchStatus = useCallback(async () => {
    try {
      const res = await testDiscordToken(token, "");
      if (res.ok) {
        setStatus({
          state: "ok",
          bot_user: res.bot_user,
          intent: res.message_content_intent ?? "unknown",
        });
      } else {
        setStatus({ state: "error", code: res.error ?? "unknown" });
      }
    } catch {
      setStatus({ state: "error", code: "unknown" });
    }
  }, [token]);

  const fetchRuntime = useCallback(async () => {
    try {
      const data = await getChannelsRuntime(token);
      setRunning(Boolean(data.running["discord"]));
    } catch {
      // leave unknown; retried on the next tick
    }
  }, [token]);

  // Re-verified on mount AND on the same interval as the runtime poll — a
  // permission read once at setup and cached forever would show a stale
  // green chip weeks after someone flips the portal switch off.
  useEffect(() => {
    void fetchStatus();
    void fetchRuntime();
    const id = setInterval(() => {
      void fetchStatus();
      void fetchRuntime();
    }, 10000);
    return () => clearInterval(id);
  }, [fetchStatus, fetchRuntime]);

  useEffect(() => {
    let cancelled = false;
    getDiscordInvite(token)
      .then((res) => {
        if (!cancelled) setInvite(res);
      })
      .catch(() => {
        // the empty-guild-list state simply omits the invite link
      });
    return () => {
      cancelled = true;
    };
  }, [token]);

  const startNow = async () => {
    setStarting(true);
    setStartError(null);
    try {
      const res = await startChannel(token, "discord");
      if (!res.ok) setStartError(res.error ?? "error");
      await fetchRuntime();
    } finally {
      setStarting(false);
    }
  };

  return (
    <div className="mt-3">
      <div className="flex flex-wrap items-center gap-2 rounded-xl border border-border/40 bg-muted/30 px-3 py-2 text-[13px]">
        {status.state === "checking" ? (
          <>
            <Loader2 className="h-3.5 w-3.5 animate-spin text-muted-foreground" />
            <span className="flex-1 text-muted-foreground">
              {t("settings.channels.discord.statusChecking")}
            </span>
          </>
        ) : status.state === "ok" ? (
          <>
            <span className="h-1.5 w-1.5 rounded-full bg-emerald-500" aria-hidden />
            <span className="flex-1">
              {t("settings.channels.discord.connectedAs", { user: status.bot_user ?? "" })}
            </span>
            <PermissionBadge state={status.intent} />
          </>
        ) : (
          <>
            <span className="h-1.5 w-1.5 rounded-full bg-amber-500" aria-hidden />
            <span className="flex-1 text-amber-600 dark:text-amber-400">
              {errorLabel(status.code)}
            </span>
          </>
        )}
        <Button
          size="sm"
          variant="ghost"
          onClick={onReplaceToken}
          className="h-7 rounded-full px-2.5 text-[12px] text-muted-foreground"
        >
          {t("settings.channels.discord.replaceTokens")}
        </Button>
      </div>
      {status.state === "ok" && status.intent !== "enabled" ? (
        <p className="mt-1 pl-1 text-[12px] text-muted-foreground">
          <PermissionHint state={status.intent} />
        </p>
      ) : null}

      {/* Transport status — separate from token/permission validity on purpose */}
      <div className="mt-1.5 flex flex-wrap items-center gap-2 rounded-xl border border-border/40 bg-muted/30 px-3 py-2 text-[13px]">
        {running === null ? (
          <>
            <Loader2 className="h-3.5 w-3.5 animate-spin text-muted-foreground" />
            <span className="flex-1 text-muted-foreground">
              {t("settings.channels.discord.runtimeChecking")}
            </span>
          </>
        ) : running ? (
          <>
            <span className="h-1.5 w-1.5 rounded-full bg-emerald-500" aria-hidden />
            <span className="flex-1">{t("settings.channels.discord.runtimeRunning")}</span>
          </>
        ) : (
          <>
            <span className="h-1.5 w-1.5 rounded-full bg-amber-500" aria-hidden />
            <span className="flex-1 text-amber-600 dark:text-amber-400">
              {t("settings.channels.discord.runtimeStopped")}
            </span>
            <Button
              size="sm"
              variant="outline"
              disabled={starting}
              onClick={() => void startNow()}
              className="h-7 rounded-full px-2.5 text-[12px]"
            >
              {starting
                ? t("settings.channels.discord.runtimeStarting")
                : t("settings.channels.discord.runtimeStart")}
            </Button>
          </>
        )}
      </div>
      {startError ? (
        <p className="mt-1 text-[12px] text-amber-600 dark:text-amber-400">{startError}</p>
      ) : null}
      <p className="mt-1 text-[11px] text-muted-foreground">
        {t("settings.channels.discord.secretsStored")}
      </p>

      <PairingPanel token={token} />
      <ScopePanel
        token={token}
        channelValues={channelValues}
        onChanged={onChanged}
        invite={invite}
      />
    </div>
  );
}

// ---------- guided setup wizard ----------------------------------------------

type TokenCheck =
  | { state: "idle" }
  | { state: "checking" }
  | {
      state: "ok";
      bot_user: string | null;
      application_id: string | null;
      intent: string;
      invite_url: string | null;
    }
  | { state: "error"; code: string };

function GuidedSetup({
  channel,
  token,
  onChanged,
}: {
  channel: ChannelInfo;
  token: string;
  onChanged: () => void;
}) {
  const { t } = useTranslation();
  const { client } = useClient();
  const errorLabel = useDiscordErrorLabel();

  const [createTouched, setCreateTouched] = useState(false);
  const [rawToken, setRawToken] = useState("");
  const [tokenCheck, setTokenCheck] = useState<TokenCheck>({ state: "idle" });
  const [inviteCopied, setInviteCopied] = useState(false);
  const [inviteTouched, setInviteTouched] = useState(false);
  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState(false);
  const timer = useRef<number>();

  const runCheck = useCallback(
    async (value: string) => {
      const v = value.trim();
      if (!v) {
        setTokenCheck({ state: "idle" });
        return;
      }
      setTokenCheck({ state: "checking" });
      try {
        const res = await testDiscordToken(token, v);
        if (res.ok) {
          setTokenCheck({
            state: "ok",
            bot_user: res.bot_user,
            application_id: res.application_id,
            invite_url: res.invite_url,
            intent: res.message_content_intent ?? "unknown",
          });
        } else {
          setTokenCheck({ state: "error", code: res.error ?? "unknown" });
        }
      } catch {
        setTokenCheck({ state: "error", code: "unknown" });
      }
    },
    [token],
  );

  const scheduleCheck = (value: string) => {
    window.clearTimeout(timer.current);
    const v = value.trim();
    if (!v) {
      setTokenCheck({ state: "idle" });
      return;
    }
    setTokenCheck({ state: "checking" });
    timer.current = window.setTimeout(() => void runCheck(value), AUTO_VALIDATE_DEBOUNCE_MS);
  };

  const tokenOk = tokenCheck.state === "ok" && tokenCheck.intent !== "disabled";

  // The backend builds this: it owns the permission bitfield, and a second
  // copy here would be free to drift away from what the bot actually needs.
  const inviteUrl = tokenCheck.state === "ok" ? tokenCheck.invite_url : null;

  const copyInvite = async () => {
    if (!inviteUrl) return;
    try {
      await navigator.clipboard.writeText(inviteUrl);
      setInviteCopied(true);
      setInviteTouched(true);
      setTimeout(() => setInviteCopied(false), 2500);
    } catch {
      // clipboard failure — the open-in-Discord link still works
    }
  };

  // Save: storeSecret → write ${secret:...} ref → enable → start → notify
  // parent. The raw token NEVER goes to setConfigValue.
  const save = async () => {
    if (!tokenOk) return;
    setSaving(true);
    setSaveError(false);
    try {
      await client.storeSecret({
        name: "DISCORD_TOKEN",
        value: rawToken.trim(),
        service: "channel:discord",
        scope: ["channel:discord"],
      });
      await setConfigValue(token, "channels.discord.token", "${secret:DISCORD_TOKEN}");
      await setConfigValue(token, "channels.discord.enabled", true);
      await startChannel(token, "discord");
      onChanged();
    } catch {
      setSaveError(true);
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className="mt-3 space-y-4">
      {channel.enabled ? (
        <p className="text-[12px] text-amber-600 dark:text-amber-400">
          {t("settings.channels.discord.enabledWithoutTokens")}
        </p>
      ) : null}

      {/* Step 1: create the application in the Developer Portal */}
      <div className="space-y-1.5">
        <StepHeader
          number={1}
          done={createTouched}
          title={t("settings.channels.discord.step1Title")}
        />
        <div className="flex flex-wrap items-center gap-2 pl-7">
          <a
            href="https://discord.com/developers/applications"
            target="_blank"
            rel="noreferrer"
            onClick={() => setCreateTouched(true)}
            className="inline-flex items-center gap-1.5 rounded-full border border-border/60 bg-background px-3 py-1 text-[12px] font-medium text-foreground hover:bg-muted/50 transition-colors"
          >
            <ExternalLink className="h-3.5 w-3.5" />
            {t("settings.channels.discord.createAppLink")}
          </a>
        </div>
        <p className="pl-7 text-[12px] text-muted-foreground">
          {t("settings.channels.discord.step1Hint")}
        </p>
      </div>

      {/* Step 2: bot token, validated live against Discord */}
      <div className="space-y-1.5">
        <StepHeader
          number={2}
          done={tokenOk}
          title={t("settings.channels.discord.step2Title")}
        />
        <p className="pl-7 text-[12px] text-muted-foreground">
          {t("settings.channels.discord.step2Hint")}
        </p>
        <div className="pl-7 space-y-1.5">
          <div className="flex flex-wrap items-center gap-2">
            <Input
              type="password"
              value={rawToken}
              onChange={(e) => {
                setRawToken(e.target.value);
                scheduleCheck(e.target.value);
              }}
              placeholder={t("settings.channels.discord.tokenPlaceholder")}
              className="w-[280px]"
              autoComplete="off"
            />
            {tokenCheck.state === "checking" ? (
              <Loader2 className="h-3.5 w-3.5 animate-spin text-muted-foreground" />
            ) : null}
            {tokenCheck.state === "ok" ? (
              <Check className="h-3.5 w-3.5 text-emerald-500" />
            ) : null}
          </div>

          {tokenCheck.state === "error" ? (
            <p className="text-[12px] text-amber-600 dark:text-amber-400">
              {errorLabel(tokenCheck.code)}
            </p>
          ) : null}

          {tokenCheck.state === "ok" ? (
            <div className="flex flex-wrap items-center gap-2">
              <span className="text-[12px] text-emerald-600 dark:text-emerald-400">
                {t("settings.channels.discord.connectedAs", { user: tokenCheck.bot_user ?? "" })}
              </span>
              <PermissionBadge state={tokenCheck.intent} />
            </div>
          ) : null}

          {tokenCheck.state === "ok" && tokenCheck.intent !== "enabled" ? (
            <div className={`rounded-lg border px-3 py-2 text-[12px] ${severityBoxClass(tokenCheck.intent)}`}>
              <p>
                <PermissionHint state={tokenCheck.intent} />
              </p>
              <div className="mt-1.5 flex flex-wrap items-center gap-3">
                <a
                  href={
                    tokenCheck.application_id
                      ? `https://discord.com/developers/applications/${tokenCheck.application_id}/bot`
                      : "https://discord.com/developers/applications"
                  }
                  target="_blank"
                  rel="noreferrer"
                  className="inline-flex items-center gap-1 text-[12px] font-medium underline hover:no-underline"
                >
                  <ExternalLink className="h-3 w-3" />
                  {t("settings.channels.discord.openBotPage")}
                </a>
                <Button
                  size="sm"
                  variant="ghost"
                  onClick={() => void runCheck(rawToken)}
                  className="h-6 rounded-full px-2 text-[11px]"
                >
                  {t("settings.channels.discord.recheck")}
                </Button>
              </div>
            </div>
          ) : null}
        </div>
      </div>

      {/* Step 3: invite the bot, then save */}
      <div className="space-y-1.5">
        <StepHeader
          number={3}
          done={inviteTouched}
          title={t("settings.channels.discord.step3Title")}
        />
        <p className="pl-7 text-[12px] text-muted-foreground">
          {t("settings.channels.discord.step3Hint")}
        </p>
        {inviteUrl ? (
          <div className="pl-7 space-y-1.5">
            <div className="flex flex-wrap items-center gap-2">
              <Button
                size="sm"
                variant="outline"
                onClick={() => void copyInvite()}
                className="rounded-full"
              >
                {inviteCopied ? (
                  <Check className="mr-1 h-3.5 w-3.5" />
                ) : (
                  <Copy className="mr-1 h-3.5 w-3.5" />
                )}
                {inviteCopied
                  ? t("settings.channels.discord.inviteCopied")
                  : t("settings.channels.discord.inviteCopy")}
              </Button>
              <a
                href={inviteUrl}
                target="_blank"
                rel="noreferrer"
                onClick={() => setInviteTouched(true)}
                className="inline-flex items-center gap-1.5 rounded-full border border-border/60 bg-background px-3 py-1 text-[12px] font-medium text-foreground hover:bg-muted/50 transition-colors"
              >
                <ExternalLink className="h-3.5 w-3.5" />
                {t("settings.channels.discord.openInDiscord")}
              </a>
            </div>
            <p className="text-[11px] text-muted-foreground">
              {t("settings.channels.discord.invitePermissions")}
            </p>
          </div>
        ) : null}
      </div>

      {/* Save — stores the token as a durin secret, then enables + starts */}
      <div className="space-y-1.5">
        <div className="flex flex-wrap items-center gap-2">
          <Button
            size="sm"
            variant="outline"
            disabled={saving || !tokenOk}
            onClick={() => void save()}
            className="rounded-full"
          >
            {saving
              ? t("settings.channels.discord.saving")
              : t("settings.channels.discord.saveAndEnable")}
          </Button>
        </div>
        {saveError ? (
          <p className="text-[12px] text-amber-600 dark:text-amber-400">
            {t("settings.channels.saveError")}
          </p>
        ) : null}
      </div>
    </div>
  );
}

// ---------- top-level export -----------------------------------------------

/** Guided Discord setup with a manual escape hatch.
 *
 * `children` is the generic schema-driven field form rendered by
 * ChannelsSettings — manual mode reuses it verbatim so both modes write the
 * exact same config keys. */
export function DiscordGuided({
  channel,
  channelValues,
  token,
  onChanged,
  children,
}: {
  channel: ChannelInfo;
  channelValues: Record<string, unknown>;
  token: string;
  onChanged: () => void;
  children: ReactNode;
}) {
  const [mode, setMode] = useState<"guided" | "manual">("guided");
  const [replacingToken, setReplacingToken] = useState(false);

  const tokenConfigured = Boolean(channelValues["token"]);

  return (
    <div>
      <div className="mt-2">
        <ModeSwitch mode={mode} onChange={setMode} />
      </div>

      {mode === "manual" ? (
        children
      ) : tokenConfigured && !replacingToken ? (
        <ConnectedPanel
          token={token}
          channelValues={channelValues}
          onChanged={onChanged}
          onReplaceToken={() => setReplacingToken(true)}
        />
      ) : (
        <GuidedSetup
          channel={channel}
          token={token}
          onChanged={() => {
            setReplacingToken(false);
            onChanged();
          }}
        />
      )}
    </div>
  );
}

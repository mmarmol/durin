import { useMemo, useState } from "react";
import { useTranslation } from "react-i18next";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import type { DrawerTarget } from "@/components/DreamDrawer";
import {
  ApiError,
  type FlaggedPair,
  type ResolutionProposal,
  type ResolveFlaggedBody,
} from "@/lib/api";

/** Why resolving a pair failed. A 422 carries the server's reason (a taken
 *  key, a bad slug): it is shown, so the user can fix the edit instead of
 *  guessing; anything else reads as "already processed, refresh". */
export function flaggedResolveErrorMessage(
  err: unknown,
  t: (key: string, opts?: Record<string, unknown>) => string,
): string {
  const detail = err instanceof ApiError && err.status === 422 ? err.detail : undefined;
  return detail ? `${t("dream.bandeja.resolveInvalid")} ${detail}` : t("dream.bandeja.resolveError");
}

// Who keeps an alias: one of the pair's refs, both, or none (junk).
type Owner = "both" | "none" | "a" | "b";

function slugOf(ref: string): string {
  return ref.includes(":") ? ref.slice(ref.indexOf(":") + 1) : ref;
}

function typeOf(ref: string): string {
  return ref.includes(":") ? ref.slice(0, ref.indexOf(":")) : "";
}

/** The pair's proposal as a list of human-readable operations. */
export function describeProposal(
  proposal: ResolutionProposal,
  t: (key: string, opts?: Record<string, unknown>) => string,
): string[] {
  const lines: string[] = [];
  if (proposal.kind === "merge" && proposal.survivor) {
    lines.push(t("dream.bandeja.proposal.merge", { survivor: proposal.survivor }));
  }
  for (const [ref, spec] of Object.entries(proposal.renames ?? {})) {
    if (spec?.slug) {
      lines.push(t("dream.bandeja.proposal.rename", { from: ref, to: `${typeOf(ref)}:${spec.slug}` }));
    }
    if (spec?.name) {
      lines.push(t("dream.bandeja.proposal.renameName", { ref, name: spec.name }));
    }
  }
  for (const move of proposal.alias_moves ?? []) {
    if (move.keep_on === "none") {
      lines.push(t("dream.bandeja.proposal.aliasNone", { alias: move.alias }));
    } else if (move.keep_on === "both") {
      lines.push(t("dream.bandeja.proposal.aliasBoth", { alias: move.alias }));
    } else {
      lines.push(t("dream.bandeja.proposal.aliasOnly", { alias: move.alias, ref: move.keep_on }));
    }
  }
  if (proposal.relation) {
    lines.push(
      t("dream.bandeja.proposal.relation", {
        from: proposal.relation.from_ref,
        type: proposal.relation.type,
        to: proposal.relation.to_ref,
      }),
    );
  }
  if (lines.length === 0) lines.push(t("dream.bandeja.proposal.keep"));
  return lines;
}

interface EditorState {
  owners: Record<string, Owner>;
  slugA: string;
  slugB: string;
  nameA: string;
  nameB: string;
  relate: boolean;
  reverse: boolean; // false: A → B, true: B → A
  relationType: string;
}

function currentOwner(alias: string, pair: FlaggedPair): Owner {
  const low = alias.toLowerCase();
  const onA = (pair.aliases_a ?? []).some((a) => a.toLowerCase() === low);
  const onB = (pair.aliases_b ?? []).some((a) => a.toLowerCase() === low);
  if (onA && onB) return "both";
  return onA ? "a" : "b";
}

function initialEditor(pair: FlaggedPair): EditorState {
  const p = pair.proposal as ResolutionProposal | null | undefined;
  const owners: Record<string, Owner> = {};
  for (const alias of allAliases(pair)) owners[alias] = currentOwner(alias, pair);
  for (const move of p?.alias_moves ?? []) {
    const key = Object.keys(owners).find((a) => a.toLowerCase() === move.alias.toLowerCase());
    if (!key) continue;
    owners[key] =
      move.keep_on === pair.ref_a ? "a" : move.keep_on === pair.ref_b ? "b" : (move.keep_on as Owner);
  }
  const renA = p?.renames?.[pair.ref_a];
  const renB = p?.renames?.[pair.ref_b];
  const rel = p?.relation ?? null;
  return {
    owners,
    slugA: renA?.slug ?? slugOf(pair.ref_a),
    slugB: renB?.slug ?? slugOf(pair.ref_b),
    nameA: renA?.name ?? pair.name_a ?? "",
    nameB: renB?.name ?? pair.name_b ?? "",
    relate: rel !== null,
    reverse: rel !== null && rel.from_ref === pair.ref_b,
    relationType: rel?.type ?? "",
  };
}

/** Aliases of both pages, the contested (shared) ones first. */
function allAliases(pair: FlaggedPair): string[] {
  const seen = new Map<string, string>();
  for (const a of [...(pair.aliases_a ?? []), ...(pair.aliases_b ?? [])]) {
    if (!seen.has(a.toLowerCase())) seen.set(a.toLowerCase(), a);
  }
  const list = [...seen.values()];
  return list.sort((x, y) => {
    const sx = currentOwner(x, pair) === "both" ? 0 : 1;
    const sy = currentOwner(y, pair) === "both" ? 0 : 1;
    return sx - sy || x.localeCompare(y);
  });
}

/** Turn the editor into a resolve request: only what differs from now. */
export function editorToBody(pair: FlaggedPair, s: EditorState): ResolveFlaggedBody {
  const alias_moves = Object.entries(s.owners)
    .filter(([alias, owner]) => owner !== currentOwner(alias, pair))
    .map(([alias, owner]) => ({
      alias,
      keep_on: owner === "a" ? pair.ref_a : owner === "b" ? pair.ref_b : owner,
    }));
  const renames: Record<string, { slug?: string; name?: string }> = {};
  const rename = (ref: string, slug: string, name: string, curName: string) => {
    const spec: { slug?: string; name?: string } = {};
    if (slug.trim() && slug.trim() !== slugOf(ref)) spec.slug = slug.trim();
    if (name.trim() && name.trim() !== curName) spec.name = name.trim();
    if (spec.slug || spec.name) renames[ref] = spec;
  };
  rename(pair.ref_a, s.slugA, s.nameA, pair.name_a ?? "");
  rename(pair.ref_b, s.slugB, s.nameB, pair.name_b ?? "");
  const body: ResolveFlaggedBody = {
    ref_a: pair.ref_a,
    ref_b: pair.ref_b,
    action: s.relate ? "relate" : "disambiguate",
  };
  if (alias_moves.length) body.alias_moves = alias_moves;
  if (Object.keys(renames).length) body.renames = renames;
  if (s.relate) {
    body.relation = {
      from_ref: s.reverse ? pair.ref_b : pair.ref_a,
      type: s.relationType.trim(),
      to_ref: s.reverse ? pair.ref_a : pair.ref_b,
    };
  }
  return body;
}

interface FlaggedPairCardProps {
  pair: FlaggedPair;
  onOpen: (target: DrawerTarget) => void;
  onResolve: (pair: FlaggedPair, body: ResolveFlaggedBody) => void;
  resolving: boolean;
}

export function FlaggedPairCard({ pair, onOpen, onResolve, resolving }: FlaggedPairCardProps) {
  const { t } = useTranslation();
  const [editing, setEditing] = useState(false);
  const [editor, setEditor] = useState<EditorState>(() => initialEditor(pair));
  const proposal = (pair.proposal ?? null) as ResolutionProposal | null;
  const proposalLines = useMemo(
    () => (proposal ? describeProposal(proposal, t as never) : []),
    [proposal, t],
  );
  const aliases = useMemo(() => allAliases(pair), [pair]);
  const base = { ref_a: pair.ref_a, ref_b: pair.ref_b };

  function view(ref: string) {
    onOpen({ ref, ref_kind: "entity", summary: pair.reasoning });
  }

  const setOwner = (alias: string, owner: Owner) =>
    setEditor((s) => ({ ...s, owners: { ...s.owners, [alias]: owner } }));

  const relationMissingType = editor.relate && !editor.relationType.trim();

  return (
    <div className="flex flex-col gap-2 rounded-[8px] border border-border/40 bg-card px-4 py-3">
      <div className="flex items-start gap-2">
        <div className="flex min-w-0 flex-1 flex-col gap-0.5">
          <div className="flex items-center gap-2 flex-wrap">
            <button type="button" className="text-[12px] font-medium text-foreground hover:underline"
              onClick={() => view(pair.ref_a)}>{pair.ref_a}</button>
            <span className="text-[11px] text-muted-foreground">↔</span>
            <button type="button" className="text-[12px] font-medium text-foreground hover:underline"
              onClick={() => view(pair.ref_b)}>{pair.ref_b}</button>
            <span className="text-[11px] text-muted-foreground/60">
              {pair.verdict} · {pair.confidence}%
            </span>
            {pair.source && (
              <span className="rounded-full border border-border/60 px-1.5 text-[10px] text-muted-foreground">
                {t(`dream.bandeja.source.${pair.source}`, { defaultValue: pair.source })}
              </span>
            )}
          </div>
          <p className="text-[13px] text-muted-foreground mt-1 whitespace-pre-line">{pair.reasoning}</p>
        </div>
      </div>

      {proposal && (
        <div className="rounded-md border border-primary/30 bg-primary/5 px-3 py-2">
          <p className="text-[11px] font-semibold uppercase tracking-wide text-muted-foreground">
            {t("dream.bandeja.proposalTitle")}
          </p>
          <ul className="mt-1 flex flex-col gap-0.5">
            {proposalLines.map((line) => (
              <li key={line} className="text-[12px] text-foreground">{line}</li>
            ))}
          </ul>
        </div>
      )}

      <div className="flex items-center gap-2 flex-wrap">
        {proposal && (
          <Button type="button" variant="default" size="sm" className="text-[12px]" disabled={resolving}
            onClick={() => onResolve(pair, { ...base, action: "accept" })}>
            {t("dream.bandeja.applyProposal")}
          </Button>
        )}
        <Button type="button" variant={proposal ? "outline" : "default"} size="sm" className="text-[12px]"
          disabled={resolving}
          onClick={() => onResolve(pair, { ...base, action: "merge", survivor: pair.ref_a })}>
          {t("dream.bandeja.mergeInto", { ref: slugOf(pair.ref_a) })}
        </Button>
        <Button type="button" variant="outline" size="sm" className="text-[12px]" disabled={resolving}
          onClick={() => onResolve(pair, { ...base, action: "merge", survivor: pair.ref_b })}>
          {t("dream.bandeja.mergeInto", { ref: slugOf(pair.ref_b) })}
        </Button>
        <Button type="button" variant="outline" size="sm" className="text-[12px]" disabled={resolving}
          onClick={() => onResolve(pair, { ...base, action: "separate" })}>
          {t("dream.bandeja.keepSeparate")}
        </Button>
        <Button type="button" variant="ghost" size="sm" className="text-[12px]" disabled={resolving}
          aria-expanded={editing}
          onClick={() => setEditing((e) => !e)}>
          {t("dream.bandeja.edit")}
        </Button>
      </div>

      {editing && (
        <div className="flex flex-col gap-3 rounded-md border border-border/60 px-3 py-3">
          {aliases.length > 0 && (
            <div className="flex flex-col gap-1">
              <p className="text-[11px] font-semibold uppercase tracking-wide text-muted-foreground">
                {t("dream.bandeja.aliases")}
              </p>
              {aliases.map((alias) => (
                <label key={alias} className="flex items-center gap-2 text-[12px]">
                  <span className="min-w-0 flex-1 truncate">{alias}</span>
                  <select
                    aria-label={alias}
                    className="rounded border border-input bg-background px-1 py-0.5 text-[12px]"
                    value={editor.owners[alias]}
                    onChange={(e) => setOwner(alias, e.target.value as Owner)}
                  >
                    <option value="both">{t("dream.bandeja.keepBoth")}</option>
                    <option value="a">{t("dream.bandeja.keepOnly", { ref: slugOf(pair.ref_a) })}</option>
                    <option value="b">{t("dream.bandeja.keepOnly", { ref: slugOf(pair.ref_b) })}</option>
                    <option value="none">{t("dream.bandeja.keepNone")}</option>
                  </select>
                </label>
              ))}
            </div>
          )}

          <div className="flex flex-col gap-1">
            <p className="text-[11px] font-semibold uppercase tracking-wide text-muted-foreground">
              {t("dream.bandeja.keys")}
            </p>
            {([
              ["a", pair.ref_a, editor.slugA, editor.nameA],
              ["b", pair.ref_b, editor.slugB, editor.nameB],
            ] as const).map(([side, ref, slug, name]) => (
              <div key={side} className="flex items-center gap-2">
                <span className="w-24 shrink-0 truncate text-[12px] text-muted-foreground">{typeOf(ref)}:</span>
                <Input
                  aria-label={`${t("dream.bandeja.slug")} ${ref}`}
                  className="h-7 text-[12px]"
                  value={slug}
                  onChange={(e) =>
                    setEditor((s) => (side === "a" ? { ...s, slugA: e.target.value } : { ...s, slugB: e.target.value }))
                  }
                />
                <Input
                  aria-label={`${t("dream.bandeja.name")} ${ref}`}
                  className="h-7 text-[12px]"
                  value={name}
                  onChange={(e) =>
                    setEditor((s) => (side === "a" ? { ...s, nameA: e.target.value } : { ...s, nameB: e.target.value }))
                  }
                />
              </div>
            ))}
          </div>

          <div className="flex flex-col gap-1">
            <label className="flex items-center gap-2 text-[12px]">
              <input
                type="checkbox"
                checked={editor.relate}
                onChange={(e) => setEditor((s) => ({ ...s, relate: e.target.checked }))}
              />
              {t("dream.bandeja.relate")}
            </label>
            {editor.relate && (
              <div className="flex items-center gap-2">
                <select
                  aria-label={t("dream.bandeja.relationDirection")}
                  className="rounded border border-input bg-background px-1 py-0.5 text-[12px]"
                  value={editor.reverse ? "ba" : "ab"}
                  onChange={(e) => setEditor((s) => ({ ...s, reverse: e.target.value === "ba" }))}
                >
                  <option value="ab">{slugOf(pair.ref_a)} → {slugOf(pair.ref_b)}</option>
                  <option value="ba">{slugOf(pair.ref_b)} → {slugOf(pair.ref_a)}</option>
                </select>
                <Input
                  aria-label={t("dream.bandeja.relationType")}
                  placeholder="edition_of, part_of, specializes…"
                  className="h-7 text-[12px]"
                  value={editor.relationType}
                  onChange={(e) => setEditor((s) => ({ ...s, relationType: e.target.value }))}
                />
              </div>
            )}
          </div>

          <div className="flex items-center gap-2">
            <Button type="button" size="sm" className="text-[12px]"
              disabled={resolving || relationMissingType}
              onClick={() => onResolve(pair, editorToBody(pair, editor))}>
              {t("dream.bandeja.applyChanges")}
            </Button>
            <Button type="button" variant="ghost" size="sm" className="text-[12px]"
              onClick={() => { setEditor(initialEditor(pair)); setEditing(false); }}>
              {t("dream.bandeja.cancel")}
            </Button>
          </div>
        </div>
      )}
    </div>
  );
}

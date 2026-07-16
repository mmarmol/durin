import { useCallback, useEffect, useMemo, useState } from "react";
import { useTranslation } from "react-i18next";
import {
  BookOpen,
  ChevronRight,
  ExternalLink,
  FileText,
  Layers,
  Link2,
  RefreshCw,
  Search as SearchIcon,
  Sparkles,
  Trash2,
  type LucideIcon,
} from "lucide-react";

import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from "@/components/ui/alert-dialog";
import { Button } from "@/components/ui/button";
import MarkdownTextRenderer from "@/components/MarkdownTextRenderer";
import {
  ApiError,
  fetchMemoryDocument,
  fetchMemoryDocuments,
  forgetMemoryDocument,
  type ReferenceDocumentDetail,
  type ReferenceDocumentSummary,
} from "@/lib/api";
import { cn } from "@/lib/utils";

type DocEntity = ReferenceDocumentDetail["entities"][number];

interface DocumentsShelfProps {
  token: string | null;
  active: boolean;
  // Cross-navigation back into the graph: open a related entity's page in the
  // existing side panel.
  onOpenEntity?: (ref: string) => void;
}

function hostOf(source: string): string {
  try {
    return new URL(source).host;
  } catch {
    return source.length > 44 ? `${source.slice(0, 44)}…` : source;
  }
}

/**
 * Strip HTML comment markers (provenance metadata) from memory body text.
 * Applies the removal repeatedly until the string is stable so that nested or
 * overlapping ``<!-- -->`` sequences cannot leave a dangling ``<!--`` behind
 * (a single pass over multi-character delimiters is not sufficient).
 */
function stripHtmlComments(text: string): string {
  let prev: string;
  let out = text;
  do {
    prev = out;
    out = out.replace(/<!--[\s\S]*?-->/g, "");
  } while (out !== prev);
  return out;
}

/**
 * The Library "shelf": a searchable list of ingested reference documents on the
 * left, and — for the selected one — a tabbed detail (outline / related
 * entities / content) on the right.
 *
 * Ingested documents are deliberately kept out of default recall; this is the
 * deliberate surface for browsing them. Related entities are clickable back
 * into the graph, and the whole raw document opens in the reference panel.
 */
export function DocumentsShelf({
  token,
  active,
  onOpenEntity,
}: DocumentsShelfProps) {
  const { t } = useTranslation();
  const [docs, setDocs] = useState<ReferenceDocumentSummary[] | null>(null);
  const [listError, setListError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [query, setQuery] = useState("");
  const [selectedSlug, setSelectedSlug] = useState<string | null>(null);
  const [detail, setDetail] = useState<ReferenceDocumentDetail | null>(null);
  const [detailLoading, setDetailLoading] = useState(false);
  const [detailError, setDetailError] = useState<string | null>(null);
  // Which document a delete has been requested for (drives the confirm dialog).
  const [confirmDeleteSlug, setConfirmDeleteSlug] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    if (!token) return;
    setLoading(true);
    setListError(null);
    try {
      setDocs(await fetchMemoryDocuments(token));
    } catch (e) {
      const msg = e instanceof ApiError ? `HTTP ${e.status}` : (e as Error).message;
      setListError(msg);
      setDocs([]);
    } finally {
      setLoading(false);
    }
  }, [token]);

  // Load the list the first time the shelf becomes active (and a token exists).
  useEffect(() => {
    if (active && token && docs === null && !loading) void refresh();
  }, [active, token, docs, loading, refresh]);

  // Load the selected document's detail.
  useEffect(() => {
    if (!selectedSlug || !token) {
      setDetail(null);
      return;
    }
    let cancelled = false;
    setDetailLoading(true);
    setDetailError(null);
    setDetail(null);
    void fetchMemoryDocument(token, selectedSlug)
      .then((d) => {
        if (cancelled) return;
        if (d === null) setDetailError(t("memoryGraph.documentGone"));
        else setDetail(d);
      })
      .catch((e) => {
        if (cancelled) return;
        const msg = e instanceof ApiError ? `HTTP ${e.status}` : (e as Error).message;
        setDetailError(msg);
      })
      .finally(() => {
        if (!cancelled) setDetailLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [selectedSlug, token, t]);

  const filtered = useMemo(() => {
    const rows = docs ?? [];
    const q = query.trim().toLowerCase();
    if (!q) return rows;
    return rows.filter(
      (d) =>
        d.title.toLowerCase().includes(q) ||
        (d.source ?? "").toLowerCase().includes(q),
    );
  }, [docs, query]);

  // Forget an ingested document: archive it + drop its index rows, then drop the
  // row from the list and clear the detail if it was showing.
  const performDelete = useCallback(async () => {
    if (!token || !confirmDeleteSlug) return;
    const slug = confirmDeleteSlug;
    try {
      const out = await forgetMemoryDocument(token, slug);
      if (out.result === "archived" || out.result === "not_found") {
        setDocs((prev) => prev?.filter((d) => d.slug !== slug) ?? null);
        setSelectedSlug((cur) => (cur === slug ? null : cur));
      } else {
        setDetailError(out.detail || out.result);
      }
    } catch (e) {
      setDetailError(
        e instanceof ApiError ? `HTTP ${e.status}` : (e as Error).message,
      );
    } finally {
      setConfirmDeleteSlug(null);
    }
  }, [token, confirmDeleteSlug]);

  const confirmTitle =
    docs?.find((d) => d.slug === confirmDeleteSlug)?.title ?? "";

  return (
    <>
    <div className="flex min-h-0 flex-1">
      {/* Left: searchable list */}
      <div className="flex w-72 shrink-0 flex-col border-r border-border/40">
        <div className="flex items-center gap-2 border-b border-border/40 px-3 py-2">
          <div className="relative flex-1">
            <SearchIcon
              className="pointer-events-none absolute left-2 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-muted-foreground"
              aria-hidden
            />
            <input
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              placeholder={t("memoryGraph.documentsSearchPlaceholder")}
              className="h-7 w-full rounded-md border border-input bg-background pl-7 pr-2 text-[12.5px] outline-none focus:ring-1 focus:ring-ring"
            />
          </div>
          <Button
            variant="ghost"
            size="icon"
            aria-label={t("memoryGraph.refresh")}
            onClick={() => void refresh()}
            disabled={loading}
            className="h-7 w-7"
          >
            <RefreshCw className={cn("h-3.5 w-3.5", loading && "animate-spin")} />
          </Button>
        </div>
        <div className="min-h-0 flex-1 overflow-y-auto">
          {listError ? (
            <div className="px-3 py-3 text-xs text-destructive">{listError}</div>
          ) : null}
          {loading && docs === null ? (
            <div className="px-3 py-3 text-xs text-muted-foreground">
              {t("memoryGraph.loading")}
            </div>
          ) : null}
          {docs !== null && filtered.length === 0 && !loading ? (
            <div className="px-3 py-6 text-center text-xs text-muted-foreground">
              {docs.length === 0
                ? t("memoryGraph.documentsEmpty")
                : t("memoryGraph.noMatches")}
            </div>
          ) : null}
          <ul>
            {filtered.map((d) => (
              <li key={d.slug}>
                <button
                  type="button"
                  onClick={() => setSelectedSlug(d.slug)}
                  className={cn(
                    "flex w-full items-start gap-2 border-b border-border/20 px-3 py-2.5 text-left hover:bg-muted/60",
                    selectedSlug === d.slug && "bg-muted/80",
                  )}
                >
                  <FileText
                    className="mt-0.5 h-3.5 w-3.5 shrink-0 text-amber-500"
                    aria-hidden
                  />
                  <span className="min-w-0 flex-1">
                    <span className="block truncate text-[13px] font-medium">
                      {d.title}
                    </span>
                    <span className="mt-0.5 flex items-center gap-1.5 text-[10.5px] text-muted-foreground">
                      {d.ingested_at ? <span>{d.ingested_at.slice(0, 10)}</span> : null}
                      <span>
                        · {t("memoryGraph.documentChunksCount", { count: d.chunk_count })}
                      </span>
                      {d.distilled ? (
                        <Sparkles
                          className="h-3 w-3 text-primary"
                          aria-label={t("memoryGraph.documentDistilled")}
                        />
                      ) : null}
                    </span>
                  </span>
                </button>
              </li>
            ))}
          </ul>
        </div>
      </div>

      {/* Right: detail */}
      <div className="min-h-0 flex-1 overflow-hidden">
        {!selectedSlug ? (
          <div className="flex h-full flex-col items-center justify-center gap-2 p-6 text-center text-sm text-muted-foreground">
            <BookOpen className="h-6 w-6" aria-hidden />
            <span>{t("memoryGraph.documentsHint")}</span>
          </div>
        ) : detailLoading ? (
          <div className="p-4 text-xs text-muted-foreground">
            {t("memoryGraph.loading")}
          </div>
        ) : detailError ? (
          <div className="p-4 text-xs text-destructive">{detailError}</div>
        ) : detail ? (
          <DocumentDetailView
            key={detail.slug}
            detail={detail}
            onOpenEntity={onOpenEntity}
            onRequestDelete={setConfirmDeleteSlug}
          />
        ) : null}
      </div>
    </div>

      <AlertDialog
        open={confirmDeleteSlug !== null}
        onOpenChange={(o) => (!o ? setConfirmDeleteSlug(null) : undefined)}
      >
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>
              {t("memoryGraph.documentForgetTitle", { title: confirmTitle })}
            </AlertDialogTitle>
            <AlertDialogDescription>
              {t("memoryGraph.documentForgetBody")}
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel onClick={() => setConfirmDeleteSlug(null)}>
              {t("memoryGraph.documentForgetCancel")}
            </AlertDialogCancel>
            <AlertDialogAction
              className="bg-destructive text-destructive-foreground hover:bg-destructive/90"
              onClick={() => void performDelete()}
            >
              {t("memoryGraph.documentForgetConfirm")}
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </>
  );
}

type DetailTab = "document" | "fragments" | "entities";

function DocumentDetailView({
  detail,
  onOpenEntity,
  onRequestDelete,
}: {
  detail: ReferenceDocumentDetail;
  onOpenEntity?: (ref: string) => void;
  onRequestDelete: (slug: string) => void;
}) {
  const { t } = useTranslation();
  const [tab, setTab] = useState<DetailTab>("document");
  const isUrl = !!detail.source && /^https?:/i.test(detail.source);
  const distilled = detail.entities.filter((e) => e.relation === "distilled");
  const referenced = detail.entities.filter((e) => e.relation === "referenced");
  // Only sections the dream actually summarised — skip a bare "preamble" with no
  // text so a heading-less document doesn't render an almost-empty outline.
  const outlineSections =
    detail.outline?.sections.filter((s) => s.summary.trim()) ?? [];
  const body = stripHtmlComments(detail.body).trim();

  const tabs: Array<{ id: DetailTab; label: string; icon: LucideIcon; count?: number }> = [
    { id: "document", label: t("memoryGraph.documentTab"), icon: FileText },
    {
      id: "fragments",
      label: t("memoryGraph.documentFragmentsTab"),
      icon: Layers,
      count: detail.chunks_total,
    },
    {
      id: "entities",
      label: t("memoryGraph.documentEntitiesShort"),
      icon: Sparkles,
      count: detail.entities.length,
    },
  ];

  return (
    <div className="flex h-full flex-col">
      {/* Header (persistent above the tabs) */}
      <div className="flex items-start gap-2 border-b border-border/40 p-4">
        <BookOpen className="mt-1 h-4 w-4 shrink-0 text-amber-500" aria-hidden />
        <div className="min-w-0 flex-1">
          <h2 className="text-base font-semibold">{detail.title}</h2>
          <div className="mt-0.5 flex flex-wrap items-center gap-x-2 gap-y-0.5 text-[11px] text-muted-foreground">
            {detail.ingested_at ? (
              <span>
                {t("memoryGraph.documentIngested")}: {detail.ingested_at.slice(0, 10)}
              </span>
            ) : null}
            <span>
              · {t("memoryGraph.documentChunksCount", { count: detail.chunks_total })}
            </span>
            {detail.source ? (
              isUrl ? (
                <a
                  href={detail.source}
                  target="_blank"
                  rel="noreferrer"
                  className="inline-flex items-center gap-1 text-primary hover:underline"
                >
                  <ExternalLink className="h-3 w-3" /> {hostOf(detail.source)}
                </a>
              ) : (
                <span>· {detail.source}</span>
              )
            ) : null}
          </div>
        </div>
        <Button
          variant="ghost"
          size="icon"
          className="h-7 w-7 shrink-0 text-muted-foreground hover:text-destructive"
          aria-label={t("memoryGraph.documentForget")}
          title={t("memoryGraph.documentForget")}
          onClick={() => onRequestDelete(detail.slug)}
        >
          <Trash2 className="h-3.5 w-3.5" />
        </Button>
      </div>

      {/* Tab bar */}
      <div className="flex items-center gap-1 border-b border-border/40 px-3 py-1.5">
        {tabs.map((tb) => {
          const Icon = tb.icon;
          return (
            <button
              key={tb.id}
              type="button"
              onClick={() => setTab(tb.id)}
              className={cn(
                "flex items-center gap-1.5 rounded px-2.5 py-1 text-[12px] transition-colors",
                tab === tb.id
                  ? "bg-muted font-medium"
                  : "text-muted-foreground hover:bg-muted/60",
              )}
            >
              <Icon className="h-3.5 w-3.5" /> {tb.label}
              {tb.count !== undefined ? (
                <span className="text-[10px] text-muted-foreground">{tb.count}</span>
              ) : null}
            </button>
          );
        })}
      </div>

      {/* Tab content (scrolls) */}
      <div className="min-h-0 flex-1 overflow-y-auto p-4">
        {tab === "document" ? (
          <div className="flex flex-col gap-4">
            {detail.outline ? (
              detail.outline.abstract ? (
                <p className="text-[13px] leading-relaxed">
                  {detail.outline.abstract}
                </p>
              ) : null
            ) : (
              <p className="rounded-lg border border-dashed border-border/40 px-3 py-2 text-[12px] text-muted-foreground">
                {t("memoryGraph.documentNotDistilled")}
              </p>
            )}

            {outlineSections.length > 0 ? (
              <section>
                <h3 className="mb-1.5 flex items-center gap-1.5 text-xs font-semibold uppercase tracking-wide text-muted-foreground">
                  <Layers className="h-3.5 w-3.5" /> {t("memoryGraph.documentOutline")}
                </h3>
                <ul className="space-y-1.5">
                  {outlineSections.map((s, i) => (
                    <li key={`${s.breadcrumb}-${i}`} className="text-[12.5px]">
                      <span className="font-medium">
                        {s.breadcrumb || t("memoryGraph.documentPreamble")}
                      </span>
                      <span className="text-muted-foreground"> — {s.summary}</span>
                    </li>
                  ))}
                </ul>
              </section>
            ) : null}

            {body ? (
              <div
                className={cn(
                  detail.outline || outlineSections.length > 0
                    ? "border-t border-border/40 pt-4"
                    : undefined,
                )}
              >
                <MarkdownTextRenderer className="text-[12.5px] leading-relaxed">
                  {body}
                </MarkdownTextRenderer>
              </div>
            ) : null}
          </div>
        ) : null}

        {tab === "fragments" ? (
          <div className="flex flex-col gap-3">
            <p className="text-[12px] text-muted-foreground">
              {t("memoryGraph.documentFragmentsIntro")}
            </p>
            <ul className="space-y-2">
              {detail.chunks_preview.map((c) => (
                <li
                  key={c.idx}
                  className="rounded-lg border border-border/40 bg-background/40 p-2.5"
                >
                  {c.breadcrumb ? (
                    <div className="mb-1 font-mono text-[10.5px] text-muted-foreground">
                      {c.breadcrumb}
                    </div>
                  ) : null}
                  <p className="whitespace-pre-wrap text-[12.5px] leading-relaxed">
                    {c.text}
                  </p>
                </li>
              ))}
            </ul>
            {detail.chunks_total > detail.chunks_preview.length ? (
              <p className="text-[11px] text-muted-foreground">
                {t("memoryGraph.documentChunksMore", {
                  shown: detail.chunks_preview.length,
                  total: detail.chunks_total,
                })}
              </p>
            ) : null}
          </div>
        ) : null}

        {tab === "entities" ? (
          detail.entities.length === 0 ? (
            <p className="text-[12px] text-muted-foreground">
              {t("memoryGraph.documentEntitiesEmpty")}
            </p>
          ) : (
            <div className="flex flex-col gap-4">
              <p className="text-[12px] text-muted-foreground">
                {t("memoryGraph.documentEntitiesIntro")}
              </p>
              {distilled.length > 0 ? (
                <EntityGroup
                  title={t("memoryGraph.documentRelDistilledTitle")}
                  hint={t("memoryGraph.documentRelDistilledHint")}
                  icon={Sparkles}
                  items={distilled}
                  onOpenEntity={onOpenEntity}
                />
              ) : null}
              {referenced.length > 0 ? (
                <EntityGroup
                  title={t("memoryGraph.documentRelReferencedTitle")}
                  hint={t("memoryGraph.documentRelReferencedHint")}
                  icon={Link2}
                  items={referenced}
                  onOpenEntity={onOpenEntity}
                />
              ) : null}
            </div>
          )
        ) : null}

      </div>
    </div>
  );
}

/** One relationship group in the entities tab: a titled, explained list of the
 *  entities that relate to the document the same way (distilled vs referenced). */
function EntityGroup({
  title,
  hint,
  icon: Icon,
  items,
  onOpenEntity,
}: {
  title: string;
  hint: string;
  icon: LucideIcon;
  items: DocEntity[];
  onOpenEntity?: (ref: string) => void;
}) {
  return (
    <section>
      <h3 className="flex items-center gap-1.5 text-xs font-semibold uppercase tracking-wide text-muted-foreground">
        <Icon className="h-3.5 w-3.5" /> {title}
      </h3>
      <p className="mb-1.5 mt-0.5 text-[11px] text-muted-foreground">{hint}</p>
      <ul className="flex flex-col gap-1">
        {items.map((ent) => (
          <li key={ent.ref}>
            <button
              type="button"
              disabled={!onOpenEntity}
              onClick={() => onOpenEntity?.(ent.ref)}
              className={cn(
                "flex w-full items-start gap-2 rounded-md border border-border/40 bg-background/40 px-2.5 py-2 text-left",
                onOpenEntity && "hover:bg-muted/60",
              )}
            >
              <span className="mt-0.5 shrink-0 rounded bg-muted px-1 text-[10px] uppercase tracking-wide text-muted-foreground">
                {ent.type}
              </span>
              <span className="min-w-0 flex-1">
                <span className="block truncate text-[13px] font-medium">
                  {ent.name}
                </span>
                {ent.significance ? (
                  <span className="mt-0.5 block text-[11.5px] text-muted-foreground">
                    {ent.significance}
                  </span>
                ) : null}
              </span>
              {onOpenEntity ? (
                <ChevronRight className="mt-0.5 h-3.5 w-3.5 shrink-0 text-muted-foreground" />
              ) : null}
            </button>
          </li>
        ))}
      </ul>
    </section>
  );
}

import { Children, isValidElement } from "react";
import ReactMarkdown from "react-markdown";
import rehypeKatex from "rehype-katex";
import remarkGfm from "remark-gfm";
import remarkMath from "remark-math";
import wikiLinkPlugin from "@flowershow/remark-wiki-link";

import { CodeBlock } from "@/components/CodeBlock";
import { RichBlock } from "@/components/rich/RichBlock";
import { richKind } from "@/components/rich/rich-languages";
import { FormulaActions } from "@/components/math/FormulaActions";
import { cn } from "@/lib/utils";

import "katex/dist/katex.min.css";

interface MarkdownTextRendererProps {
  children: string;
  className?: string;
  /** Optional handler invoked when the user clicks a wikilink whose
   *  target resolves to a memory URI (``memory/<class>/<id>``). When
   *  unset, wikilinks render as plain anchors with the
   *  ``#wikilink:<target>`` fragment href and no navigation — chat
   *  bubbles intentionally leave this unset so wikilinks there don't
   *  hijack navigation. The MemoryGraphView drawer passes a handler
   *  that selects the linked entry in-place. */
  onWikiLinkClick?: (target: string) => void;
}

/**
 * Heavy markdown stack (GFM, math, KaTeX, syntax highlighting) kept in a
 * separate chunk so the app shell can paint sooner on refresh.
 */
// The wikilink plugin maps `[[target]]` to an `<a>` with this
// href shape. We intercept anchors with this prefix in the `a`
// component override to dispatch onWikiLinkClick instead of
// navigating. Using a non-navigable `#wikilink:` prefix (rather
// than `#memory/...`) so wikilinks without a handler are harmless
// fragment refs, never accidental cross-app navigation.
const WIKILINK_HREF_PREFIX = "#wikilink:";

// @flowershow/remark-wiki-link option names (NOT the ones from
// the older `remark-wiki-link` package): `className` (default
// `"internal"`), `newClassName` (default `"new"`), `urlResolver`,
// `format`. Since we don't pass a `permalinks` map, EVERY wikilink
// is classified as "new" — so we set both class names to the same
// value to get a single stable selector for the `<a>` override below.
//
// Note on `urlResolver` signature: the README documents it as
// `(name: string) => string` but the actual implementation (per
// `index.d.ts`) passes `{ filePath, isEmbed, heading }`. We use the
// typed object form here — relying on the README would silently emit
// `[object Object]` in the href.
const WIKILINK_PLUGIN_OPTIONS = {
  className: "wiki-link",
  newClassName: "wiki-link",
  format: "regular" as const,
  urlResolver: (opts: { filePath: string; isEmbed: boolean; heading: string }) =>
    `${WIKILINK_HREF_PREFIX}${opts.filePath}${opts.heading ? `#${opts.heading}` : ""}`,
};

export default function MarkdownTextRenderer({
  children,
  className,
  onWikiLinkClick,
}: MarkdownTextRendererProps) {
  return (
    <div
      className={cn(
        "markdown-content prose max-w-none dark:prose-invert",
        "prose-headings:mt-4 prose-headings:mb-2 prose-headings:font-semibold prose-headings:tracking-tight",
        "prose-h1:text-lg prose-h2:text-base prose-h3:text-sm prose-h4:text-[13px]",
        "prose-p:my-2",
        "prose-ul:my-2 prose-ol:my-2 prose-li:my-0.5",
        "prose-blockquote:my-3 prose-blockquote:border-l-2 prose-blockquote:font-normal",
        "prose-blockquote:not-italic prose-blockquote:text-foreground/80",
        "prose-a:text-primary prose-a:underline-offset-2 hover:prose-a:opacity-80",
        "prose-hr:my-6",
        "prose-pre:my-0 prose-pre:bg-transparent prose-pre:p-0",
        "prose-code:before:content-none prose-code:after:content-none prose-code:font-normal",
        "prose-table:my-3 prose-th:text-left prose-th:font-medium",
        className,
      )}
      style={{ lineHeight: "var(--cjk-line-height)" }}
    >
      <ReactMarkdown
        remarkPlugins={[
          remarkGfm,
          remarkMath,
          [wikiLinkPlugin, WIKILINK_PLUGIN_OPTIONS],
        ]}
        rehypePlugins={[rehypeKatex]}
        components={{
          code({ className: cls, children: kids, ...props }) {
            const match = /language-(\w[\w-]*)/.exec(cls || "");
            if (match) {
              const code = String(kids).replace(/\n$/, "");
              if (richKind(match[1])) {
                return <RichBlock language={match[1]} code={code} />;
              }
              return <CodeBlock language={match[1]} code={code} className="my-3" />;
            }
            const raw = String(kids).replace(/\n$/, "");
            /** Plain fenced ``` blocks (no language) & wide one-liners: block monospace, not inline pill. */
            const widePlainBlock = raw.includes("\n") || raw.length > 120;
            if (widePlainBlock) {
              return (
                <code
                  className={cn(
                    "block min-w-0 whitespace-pre bg-transparent p-0 font-mono text-[0.8125rem]",
                    "leading-snug text-inherit",
                    cls,
                  )}
                  {...props}
                >
                  {kids}
                </code>
              );
            }
            return (
              <code
                className={cn(
                  "rounded bg-muted px-1 py-0.5 font-mono text-[0.85em]",
                  cls,
                )}
                {...props}
              >
                {kids}
              </code>
            );
          },
          pre({ children: markdownChildren }) {
            const kids = Children.toArray(markdownChildren);
            const lone = kids.length === 1 ? kids[0] : null;
            /** Highlighted fences render ``CodeBlock`` or ``RichBlock`` (block shell); skip invalid ``<pre><div>``. */
            if (
              lone != null &&
              isValidElement(lone) &&
              (lone.type === CodeBlock || lone.type === RichBlock)
            ) {
              return <>{markdownChildren}</>;
            }
            return (
              <pre
                className={cn(
                  "my-3 overflow-x-auto rounded-lg border border-border/60 bg-muted/35",
                  "p-3 font-mono text-[0.8125rem] leading-snug text-foreground/90",
                  "whitespace-pre [overflow-wrap:normal]",
                )}
              >
                {markdownChildren}
              </pre>
            );
          },
          span({ className: cls, children: kids, ...props }) {
            const tokens = (cls || "").split(/\s+/);
            // Wrap only the KaTeX root (token `katex`), not its inner spans
            // (`katex-mathml`, `katex-html`, …) and not unrelated spans.
            if (tokens.includes("katex")) {
              return (
                <FormulaActions>
                  <span className={cls} {...props}>
                    {kids}
                  </span>
                </FormulaActions>
              );
            }
            return (
              <span className={cls} {...props}>
                {kids}
              </span>
            );
          },
          a({ href, children: markdownChildren, ...props }) {
            // Wikilink interception: the plugin emits hrefs prefixed
            // with WIKILINK_HREF_PREFIX. When a handler is registered,
            // click invokes it with the target (the bit after the
            // prefix) and suppresses navigation. Without a handler,
            // render as a plain non-navigating fragment anchor styled
            // to look like a soft link — visible but inert.
            if (href?.startsWith(WIKILINK_HREF_PREFIX)) {
              const target = href.slice(WIKILINK_HREF_PREFIX.length);
              const hasHandler = typeof onWikiLinkClick === "function";
              return (
                <a
                  href={href}
                  className={cn(
                    "wiki-link text-primary underline decoration-dotted underline-offset-2",
                    hasHandler ? "cursor-pointer hover:opacity-80" : "cursor-default opacity-80",
                  )}
                  onClick={(e) => {
                    if (!hasHandler) return;
                    e.preventDefault();
                    onWikiLinkClick(target);
                  }}
                  {...props}
                >
                  {markdownChildren}
                </a>
              );
            }
            return (
              <a
                href={href}
                target="_blank"
                rel="noreferrer noopener"
                className="text-primary underline underline-offset-2 hover:opacity-80"
                {...props}
              >
                {markdownChildren}
              </a>
            );
          },
        }}
      >
        {children}
      </ReactMarkdown>
    </div>
  );
}

import { useEffect, useState } from "react";
import { X, ExternalLink } from "lucide-react";
import { useTranslation } from "react-i18next";

import { Button } from "@/components/ui/button";
import MarkdownTextRenderer from "@/components/MarkdownTextRenderer";
import {
  describeSkill,
  fetchMemoryEntity,
  getSkill,
  type MemoryEntityDetail,
  type SkillDescribeResult,
  type SkillDetail,
} from "@/lib/api";
import { useClient } from "@/providers/ClientProvider";
import { cn } from "@/lib/utils";

export interface DrawerTarget {
  ref: string;
  ref_kind: "entity" | "skill";
  summary: string;
}

interface DreamDrawerProps {
  target: DrawerTarget | null;
  onClose: () => void;
}

/**
 * Strip inline HTML comment markers (<!-- ... -->) from entity body text.
 * These are provenance metadata injected by the memory engine and should not
 * be shown in the drawer UI.
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

function EntityDetail({ entity }: { entity: MemoryEntityDetail }) {
  const { t } = useTranslation();
  const page = entity.page;
  return (
    <div className="space-y-3 text-xs">
      {page ? (
        <>
          <dl className="space-y-1.5">
            <div className="flex justify-between gap-2">
              <dt className="text-muted-foreground">{t("dream.drawer.type")}</dt>
              <dd className="font-mono">{page.type}</dd>
            </div>
            {page.aliases.length > 0 ? (
              <div>
                <dt className="text-muted-foreground">{t("dream.drawer.aliases")}</dt>
                <dd className="mt-0.5 flex flex-wrap gap-1">
                  {page.aliases.map((a) => (
                    <span
                      key={a}
                      className="rounded bg-muted px-1.5 py-0.5 font-mono text-[10.5px]"
                    >
                      {a}
                    </span>
                  ))}
                </dd>
              </div>
            ) : null}
            {page.identifiers ? (
              <div>
                <dt className="mb-0.5 text-muted-foreground">{t("dream.drawer.identifiers")}</dt>
                <dd className="space-y-0.5">
                  {Object.entries(page.identifiers).map(([k, v]) => (
                    <div key={k} className="text-[11px]">
                      <span className="font-mono text-muted-foreground">{k}:</span>{" "}
                      {Array.isArray(v) ? v.join(", ") : String(v)}
                    </div>
                  ))}
                </dd>
              </div>
            ) : null}
          </dl>
          {page.body ? (
            <div className="border-t border-border/40 pt-3">
              <MarkdownTextRenderer className="text-[12.5px] leading-relaxed">
                {stripHtmlComments(page.body).trim()}
              </MarkdownTextRenderer>
            </div>
          ) : null}
        </>
      ) : (
        <p className="text-muted-foreground">{t("dream.drawer.noPage")}</p>
      )}
    </div>
  );
}

function SkillDetail({ skill }: { skill: SkillDetail | SkillDescribeResult }) {
  const { t } = useTranslation();
  // Local SkillDetail has `content` (the raw SKILL.md); registry
  // SkillDescribeResult has `description` + optional `body`.
  const content = "content" in skill ? skill.content : skill.body;
  const description = "description" in skill ? skill.description : undefined;
  return (
    <div className="space-y-3 text-xs">
      {description ? (
        <p className="text-[12.5px] leading-relaxed text-foreground">
          {description}
        </p>
      ) : null}
      {content ? (
        <div className={description ? "border-t border-border/40 pt-3" : undefined}>
          <MarkdownTextRenderer className="text-[12.5px] leading-relaxed">
            {content}
          </MarkdownTextRenderer>
        </div>
      ) : (
        !description && (
          <p className="text-muted-foreground">{t("dream.drawer.noDetail")}</p>
        )
      )}
    </div>
  );
}

export function DreamDrawer({ target, onClose }: DreamDrawerProps) {
  const { token } = useClient();
  const { t } = useTranslation();

  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [entityDetail, setEntityDetail] = useState<MemoryEntityDetail | null>(null);
  // Local SkillDetail (name+content) is preferred; SkillDescribeResult is the
  // fallback when the local lookup returns a 404 (skill was removed after the
  // dream ran).
  const [skillDetail, setSkillDetail] = useState<SkillDetail | SkillDescribeResult | null>(null);

  // Fetch the detail whenever the target changes.
  useEffect(() => {
    if (!target) {
      setEntityDetail(null);
      setSkillDetail(null);
      setError(null);
      return;
    }

    let cancelled = false;
    setLoading(true);
    setError(null);
    setEntityDetail(null);
    setSkillDetail(null);

    if (target.ref_kind === "entity") {
      fetchMemoryEntity(token, target.ref)
        .then((d) => {
          if (cancelled) return;
          if (d === null) {
            setError(t("dream.drawer.notFound"));
          } else {
            setEntityDetail(d);
          }
        })
        .catch((e: unknown) => {
          if (!cancelled) setError((e as Error).message);
        })
        .finally(() => {
          if (!cancelled) setLoading(false);
        });
    } else {
      // ref_kind === "skill": prefer the LOCAL skill record (getSkill →
      // GET /api/v1/skills/{name}) so the drawer shows the installed SKILL.md
      // content without a registry network call.  Falls back to describeSkill
      // only when the local lookup throws (e.g. skill was removed after the
      // dream ran).
      getSkill(token, target.ref)
        .then((d) => {
          if (!cancelled) setSkillDetail(d);
        })
        .catch(() => {
          if (cancelled) return;
          return describeSkill(token, target.ref).then((d) => {
            if (!cancelled) setSkillDetail(d);
          });
        })
        .catch((e: unknown) => {
          if (!cancelled) setError((e as Error).message);
        })
        .finally(() => {
          if (!cancelled) setLoading(false);
        });
    }

    return () => {
      cancelled = true;
    };
  }, [target, token, t]);

  const isOpen = target !== null;

  // Keyboard close: Escape key.
  useEffect(() => {
    if (!isOpen) return;
    function handleKey(e: KeyboardEvent) {
      if (e.key === "Escape") onClose();
    }
    document.addEventListener("keydown", handleKey);
    return () => document.removeEventListener("keydown", handleKey);
  }, [isOpen, onClose]);

  const displayName =
    entityDetail?.page?.name ??
    (skillDetail && ("name" in skillDetail ? skillDetail.name : skillDetail.ref)) ??
    target?.ref ??
    "";

  return (
    <>
      {/* Click-away backdrop (transparent, covers feed) */}
      {isOpen ? (
        <div
          className="absolute inset-0 z-10"
          aria-hidden
          onClick={onClose}
        />
      ) : null}

      {/* Drawer panel */}
      <div
        role="dialog"
        aria-modal="true"
        aria-label={t("dream.drawer.ariaLabel")}
        className={cn(
          "absolute right-0 top-0 z-20 flex h-full w-[340px] flex-col",
          "border-l border-border/60 bg-background shadow-xl",
          "transition-transform duration-200 ease-in-out",
          isOpen ? "translate-x-0" : "translate-x-full",
        )}
      >
        {/* Header */}
        <div className="flex shrink-0 items-center gap-2 border-b border-border/40 px-3 py-2.5">
          <span className="min-w-0 flex-1 truncate text-sm font-semibold">
            {displayName || t("dream.drawer.loading")}
          </span>
          <Button
            type="button"
            variant="ghost"
            size="icon"
            className="h-6 w-6 shrink-0"
            onClick={onClose}
            aria-label={t("dream.close")}
          >
            <X className="h-3.5 w-3.5" />
          </Button>
        </div>

        {/* Body */}
        <div className="min-h-0 flex-1 overflow-y-auto px-3 py-3">
          {loading ? (
            <div className="text-xs text-muted-foreground">{t("dream.drawer.loading")}</div>
          ) : error ? (
            <div className="text-xs text-destructive">{error}</div>
          ) : entityDetail ? (
            <EntityDetail entity={entityDetail} />
          ) : skillDetail ? (
            <SkillDetail skill={skillDetail} />
          ) : null}
        </div>

        {/* Footer */}
        <div className="shrink-0 border-t border-border/40 px-3 py-2.5">
          <Button
            type="button"
            variant="outline"
            size="sm"
            className="w-full text-xs"
            disabled
            aria-label={t("dream.openFull")}
          >
            <ExternalLink className="mr-1.5 h-3 w-3" />
            {t("dream.openFull")}
          </Button>
        </div>
      </div>
    </>
  );
}

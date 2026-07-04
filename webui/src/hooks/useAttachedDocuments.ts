import { useCallback, useEffect, useRef, useState } from "react";

/** Lifecycle of one document attachment.
 *
 * Documents are read to a ``data:`` URL as-is — no Worker re-encode (unlike
 * images) — so there is a brief ``reading`` stage while ``FileReader`` runs,
 * then ``ready``. ``error`` is only used for a read failure (validation
 * rejections never enter the list; they are returned to the caller). */
export type DocumentAttachmentStatus = "reading" | "ready" | "error";

export interface AttachedDocument {
  id: string;
  file: File;
  /** Original filename — the backend derives the saved file's extension from
   * this, and the extension drives its document dispatch. Always sent. */
  name: string;
  status: DocumentAttachmentStatus;
  /** Populated when ``status === "ready"``: ``data:<mime>;base64,...`` where
   * ``<mime>`` is a whitelisted document MIME resolved from the extension when
   * the browser reported a blank/unknown ``file.type``. */
  dataUrl?: string;
  error?: DocumentAttachmentError;
}

/** Machine-readable rejection reasons surfaced as inline chip errors or
 * returned to the caller. Localized via ``composer.documentRejected.*``. */
export type DocumentAttachmentError =
  | "unsupported_type" // extension/MIME not in the document whitelist
  | "too_many" // per-message cap (3) reached before enqueue
  | "too_large" // exceeds 25 MB
  | "io"; // FileReader failed

export const MAX_DOCUMENTS_PER_MESSAGE = 3;
export const MAX_DOCUMENT_BYTES = 25 * 1024 * 1024;

/** Extension → document MIME. Mirrors the server's ``_DOCUMENT_MIME_ALLOWED``
 * and the formats ``durin/memory/doc_convert.py`` handles. Used both to
 * validate by extension and to synthesize a proper ``data:`` MIME when the
 * browser reports ``file.type === ""`` (common for .epub / .md / .markdown). */
const EXTENSION_TO_MIME: Readonly<Record<string, string>> = {
  pdf: "application/pdf",
  docx: "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
  pptx: "application/vnd.openxmlformats-officedocument.presentationml.presentation",
  xlsx: "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
  doc: "application/msword",
  xls: "application/vnd.ms-excel",
  ppt: "application/vnd.ms-powerpoint",
  epub: "application/epub+zip",
  html: "text/html",
  htm: "text/html",
  csv: "text/csv",
  txt: "text/plain",
  md: "text/markdown",
  markdown: "text/markdown",
  json: "application/json",
  xml: "application/xml",
};

/** MIME whitelist — the accepted subset of ``data:`` MIME types. Mirrors the
 * server's ``_DOCUMENT_MIME_ALLOWED``. ``text/xml`` is accepted on input (a
 * browser may report it for ``.xml``) but we normalize to ``application/xml``
 * when building the data URL so the wire MIME is canonical. */
const ACCEPTED_MIMES: ReadonlySet<string> = new Set([
  "application/pdf",
  "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
  "application/vnd.openxmlformats-officedocument.presentationml.presentation",
  "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
  "application/msword",
  "application/vnd.ms-excel",
  "application/vnd.ms-powerpoint",
  "application/epub+zip",
  "text/html",
  "text/csv",
  "text/plain",
  "text/markdown",
  "application/json",
  "application/xml",
  "text/xml",
]);

function extensionOf(name: string): string {
  const dot = name.lastIndexOf(".");
  if (dot < 0 || dot === name.length - 1) return "";
  return name.slice(dot + 1).toLowerCase();
}

/** Strip any ``;codecs=`` / parameter suffix and lowercase, so a reported
 * ``text/plain; charset=utf-8`` matches the bare whitelist entry. */
function normalizeMime(mime: string): string {
  return mime.split(";")[0].trim().toLowerCase();
}

/** Resolve the whitelisted document MIME for a file, or ``null`` if it is not
 * a supported document. Prefers a browser-reported MIME that is already
 * whitelisted; otherwise derives one from the extension. This is the guard
 * that keeps documents out of the image bucket and, critically, guarantees the
 * outbound ``data:`` URL carries a MIME the backend recognizes as a document —
 * browsers frequently report ``""`` for .epub / .md. */
export function resolveDocumentMime(file: File): string | null {
  const reported = normalizeMime(file.type || "");
  const ext = extensionOf(file.name);
  const byExt = EXTENSION_TO_MIME[ext];
  // Extension is authoritative for the canonical wire MIME (normalizes
  // text/xml → application/xml); fall back to a whitelisted reported MIME
  // for files whose extension we don't map but whose type is recognized.
  if (byExt) return byExt;
  if (reported && ACCEPTED_MIMES.has(reported)) return reported;
  return null;
}

/** True when *file* is an accepted document (by whitelisted MIME or mapped
 * extension). Used by the Composer to route files into the document bucket. */
export function isDocumentFile(file: File): boolean {
  return resolveDocumentMime(file) !== null;
}

function uuid(): string {
  if (typeof crypto !== "undefined" && "randomUUID" in crypto) {
    return crypto.randomUUID();
  }
  return `doc-${Date.now()}-${Math.random().toString(36).slice(2)}`;
}

/** Read *file* to a base64 ``data:`` URL and rewrite its MIME to *mime*.
 *
 * ``FileReader.readAsDataURL`` prefixes the payload with the browser's own
 * ``data:<file.type>`` (often blank), so we splice in the resolved document
 * MIME. No re-encoding — the base64 body is the raw bytes. */
function readAsDataUrl(file: File, mime: string): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => {
      const result = reader.result;
      if (typeof result !== "string") {
        reject(new Error("unexpected FileReader result"));
        return;
      }
      const comma = result.indexOf(",");
      if (comma < 0) {
        reject(new Error("malformed data URL"));
        return;
      }
      const base64 = result.slice(comma + 1);
      resolve(`data:${mime};base64,${base64}`);
    };
    reader.onerror = () => reject(reader.error ?? new Error("read failed"));
    reader.readAsDataURL(file);
  });
}

export interface UseAttachedDocumentsApi {
  documents: AttachedDocument[];
  /** Enqueue new files. Returns the files rejected client-side (unsupported
   * type, size, count) so the caller can surface an inline error. Accepted
   * files are added optimistically (``reading``) and flip to ``ready`` once
   * their data URL is built (or ``error`` on a read failure). */
  enqueue: (files: Iterable<File>) => {
    rejected: Array<{ file: File; reason: DocumentAttachmentError }>;
  };
  remove: (id: string) => void;
  /** Drop all attachments. Called after a successful submit. */
  clear: () => void;
  /** ``true`` when we've hit ``MAX_DOCUMENTS_PER_MESSAGE``. */
  full: boolean;
}

/** Manage the lifecycle of documents attached to the Composer.
 *
 * Documents ride the same outbound ``media[]`` array as images but are NOT
 * inlined/re-encoded: the backend saves them to disk and hands the agent a
 * file path. Responsibilities here: validation (MIME/extension whitelist,
 * count + size caps) and building the base64 ``data:`` URL with a whitelisted
 * document MIME. */
export function useAttachedDocuments(): UseAttachedDocumentsApi {
  const [documents, setDocuments] = useState<AttachedDocument[]>([]);
  // Ref mirror so ``enqueue`` sees the authoritative length when invoked
  // multiple times in one tick (rapid selection / multi-file drop).
  const documentsRef = useRef<AttachedDocument[]>([]);
  documentsRef.current = documents;

  const setEntry = useCallback(
    (id: string, patch: Partial<AttachedDocument>) => {
      setDocuments((prev) => {
        const next = prev.map((d) => (d.id === id ? { ...d, ...patch } : d));
        documentsRef.current = next;
        return next;
      });
    },
    [],
  );

  const enqueue = useCallback(
    (files: Iterable<File>) => {
      const rejected: Array<{ file: File; reason: DocumentAttachmentError }> = [];
      const toAdd: Array<{ entry: AttachedDocument; mime: string }> = [];
      let slot = MAX_DOCUMENTS_PER_MESSAGE - documentsRef.current.length;

      for (const file of files) {
        const mime = resolveDocumentMime(file);
        if (!mime) {
          rejected.push({ file, reason: "unsupported_type" });
          continue;
        }
        if (file.size > MAX_DOCUMENT_BYTES) {
          rejected.push({ file, reason: "too_large" });
          continue;
        }
        if (slot <= 0) {
          rejected.push({ file, reason: "too_many" });
          continue;
        }
        slot -= 1;
        toAdd.push({
          entry: {
            id: uuid(),
            file,
            name: file.name,
            status: "reading",
          },
          mime,
        });
      }

      if (toAdd.length > 0) {
        const next = [...documentsRef.current, ...toAdd.map((t) => t.entry)];
        documentsRef.current = next;
        setDocuments(next);
        // Read after the commit so chips render first (good INP).
        for (const { entry, mime } of toAdd) {
          queueMicrotask(() => {
            readAsDataUrl(entry.file, mime).then(
              (dataUrl) => setEntry(entry.id, { status: "ready", dataUrl }),
              () => setEntry(entry.id, { status: "error", error: "io" }),
            );
          });
        }
      }
      return { rejected };
    },
    [setEntry],
  );

  const remove = useCallback((id: string) => {
    setDocuments((prev) => {
      const next = prev.filter((d) => d.id !== id);
      documentsRef.current = next;
      return next;
    });
  }, []);

  const clear = useCallback(() => {
    setDocuments([]);
    documentsRef.current = [];
  }, []);

  // Drop refs on unmount so a stale closure can't resurrect them.
  useEffect(() => {
    return () => {
      documentsRef.current = [];
    };
  }, []);

  const full = documents.length >= MAX_DOCUMENTS_PER_MESSAGE;
  return { documents, enqueue, remove, clear, full };
}

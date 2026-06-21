import type { ReactNode } from "react";

const TLD = "ai|io|com|org|net|dev|app|co|sh|cloud";
const TOKEN = new RegExp(
  String.raw`(https?:\/\/[^\s)]+)|((?:[a-z0-9-]+\.)+(?:${TLD})(?:\/[^\s)]*)?)`,
  "gi",
);

/** Split `text` into plain strings and external links. Full `http(s)://` URLs link as-is;
 *  bare allowlisted domains get an `https://` prefix. Conservative — no false links in
 *  short credential descriptions. */
export function linkify(text: string): ReactNode[] {
  const out: ReactNode[] = [];
  let last = 0;
  let m: RegExpExecArray | null;
  let i = 0;
  TOKEN.lastIndex = 0;
  while ((m = TOKEN.exec(text)) !== null) {
    if (m.index > last) out.push(text.slice(last, m.index));
    const raw = m[0];
    const href = m[1] ? raw : `https://${raw}`;
    out.push(
      <a
        key={`l${i++}`}
        href={href}
        target="_blank"
        rel="noopener noreferrer"
        className="text-primary underline"
      >
        {raw}
      </a>,
    );
    last = m.index + raw.length;
  }
  if (last < text.length) out.push(text.slice(last));
  return out;
}


const CSP =
  "default-src 'none'; img-src data: blob:; style-src 'unsafe-inline'; " +
  "font-src data:; script-src 'unsafe-inline'; connect-src 'none'";

function wrap(html: string): string {
  return (
    "<!doctype html><html><head>" +
    `<meta http-equiv="Content-Security-Policy" content="${CSP}">` +
    '<meta name="viewport" content="width=device-width, initial-scale=1">' +
    "<style>body{margin:0;font-family:system-ui,sans-serif}</style>" +
    `</head><body>${html}</body></html>`
  );
}

/** Renders untrusted, LLM-generated HTML/SVG in an isolated frame: scripts run
 *  but have no same-origin access and no network (CSP ``connect-src 'none'``). */
export function SandboxFrame({ html, title }: { html: string; title: string }) {
  return (
    <iframe
      title={title}
      sandbox="allow-scripts"
      srcDoc={wrap(html)}
      className="h-[360px] w-full rounded-b-lg border-0 bg-white"
    />
  );
}

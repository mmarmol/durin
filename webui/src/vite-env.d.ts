/// <reference types="vite/client" />

// MathML elements used by KaTeX in JSX test fixtures.
// Also registers the MathLive custom element for JSX.
declare namespace React {
  namespace JSX {
    interface IntrinsicElements {
      math: React.DetailedHTMLProps<React.HTMLAttributes<HTMLElement>, HTMLElement>;
      annotation: React.DetailedHTMLProps<
        React.HTMLAttributes<HTMLElement> & { encoding?: string },
        HTMLElement
      >;
      "math-field": React.DetailedHTMLProps<
        React.HTMLAttributes<HTMLElement> & { value?: string },
        HTMLElement
      >;
    }
  }
}

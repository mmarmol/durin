/** Splice ``snippet`` into ``value`` at the selection, returning the new value
 *  and the caret position just after the inserted text. */
export function insertAtCursor(
  value: string,
  selStart: number,
  selEnd: number,
  snippet: string,
): { next: string; caret: number } {
  const next = value.slice(0, selStart) + snippet + value.slice(selEnd);
  return { next, caret: selStart + snippet.length };
}

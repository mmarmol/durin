/** Sustained scroll anchoring for older-history prepends.
 *
 * A one-shot scrollHeight-delta compensation is not enough on real pages: the
 * DOM keeps growing AFTER the first post-prepend layout frame (progressive
 * markdown/image layout), so a single restore lands the view off by exactly
 * the post-snapshot growth. Instead, a `PrependPin` records a pre-prepend
 * anchor element and its viewport position, and re-restores that element to
 * the recorded position on every layout tick until the deadline passes, the
 * user scrolls, or the anchor unmounts without a re-acquirable replacement.
 *
 * Deliberately NOT released on "the position looks stable": async late
 * layout (image fallbacks resolving after 404s, markdown settling) lands
 * AFTER a quiet gap in which the position reads as settled — an early
 * stability release leaves that reflow with no pin watching. The pin holds
 * for the full window; a no-adjustment tick is simply a no-op.
 */

/** Positions within this epsilon count as "no adjustment needed this tick". */
const STABLE_EPSILON_PX = 1;
/** Two callers can observe the same commit (the prepend's layout effect plus
 *  a ResizeObserver tick, or back-to-back content-identical commits); a
 *  second apply within this gap re-reads geometry the browser has not
 *  re-laid-out yet, so it is skipped as a no-op. */
const MIN_APPLY_GAP_MS = 4;
/** Hard cap on the pinning window — the primary release — so the pin can
 *  never fight the user's own scrolling forever. */
export const PIN_MAX_MS = 1500;
/** Low-frequency safety tick while a pin is active: catches async late
 *  layout even when the ResizeObserver goes quiet. */
export const PIN_SAFETY_TICK_MS = 120;

export interface PinScroller {
  scrollTop: number;
}

export interface PinAnchor {
  isConnected: boolean;
  getBoundingClientRect: () => { top: number };
}

/** Find the current DOM element for the pinned message after a re-render
 *  swapped its node (e.g. prepended rows re-clustered with the previously
 *  first row). Returns null when the message is no longer rendered. */
export type PinReacquire = () => PinAnchor | null;

export class PrependPin {
  private el: PinAnchor;
  private readonly reacquire: PinReacquire | null;
  private readonly recordedTop: number;
  /** The last scrollTop value this pin itself produced (seeded with the
   *  scroll position at record time). Any observed position that deviates
   *  from it is the user's own scrolling, which always wins. */
  private lastSetScrollTop: number;
  private lastApplyAt: number | null = null;
  private readonly maxMs: number;
  /** Armed by the first restore tick — the cap bounds the restore window,
   *  not the fetch that precedes it (a slow page fetch must not expire the
   *  pin before the prepend ever lands). */
  private deadline: number | null = null;

  constructor(
    el: PinAnchor,
    recordedTop: number,
    initialScrollTop: number,
    reacquire: PinReacquire | null = null,
    maxMs: number = PIN_MAX_MS,
  ) {
    this.el = el;
    this.reacquire = reacquire;
    this.recordedTop = recordedTop;
    this.lastSetScrollTop = initialScrollTop;
    this.maxMs = maxMs;
  }

  /** True once the first restore tick has run (the prepend landed). */
  get started(): boolean {
    return this.deadline !== null;
  }

  /** Report an observed scroll position. Returns whether the pin stays
   *  active: `false` means the position is not one this pin set, i.e. the
   *  user scrolled themselves and the pin must be released. */
  notifyScroll(scrollTop: number): boolean {
    return Math.abs(scrollTop - this.lastSetScrollTop) <= STABLE_EPSILON_PX;
  }

  /** Restore the anchor to its recorded viewport position for one layout
   *  tick. Returns whether the pin stays active; `false` only when the
   *  deadline passed, the anchor unmounted with no re-acquirable
   *  replacement, or the user scrolled. A stable position is NOT a release:
   *  async late layout can land after an arbitrarily long quiet gap. */
  apply(scroller: PinScroller, now: number): boolean {
    if (this.deadline === null) {
      this.deadline = now + this.maxMs;
    } else if (now > this.deadline) {
      return false;
    }
    // Same-frame duplicate tick: geometry cannot have changed — no-op.
    if (this.lastApplyAt !== null && now - this.lastApplyAt < MIN_APPLY_GAP_MS) {
      return true;
    }
    this.lastApplyAt = now;
    if (!this.el.isConnected) {
      // A re-render swapped the anchor's DOM node (common: the prepended
      // rows re-clustered with the previously-first row). Re-acquire the
      // element for the SAME message and keep the same recordedTop — the
      // contract is unchanged: restore that message to that position.
      const next = this.reacquire?.() ?? null;
      if (!next || !next.isConnected) return false;
      this.el = next;
    }
    if (!this.notifyScroll(scroller.scrollTop)) return false;
    const delta = this.el.getBoundingClientRect().top - this.recordedTop;
    if (Math.abs(delta) < STABLE_EPSILON_PX) {
      // Nothing to adjust this tick — but HOLD the pin: late reflow may
      // still be coming, and only deadline/user-scroll/anchor-loss release.
      return true;
    }
    scroller.scrollTop += delta;
    // Read back rather than trusting the assignment: the browser clamps to
    // the scrollable range, and the clamped value is what the next scroll
    // event will report as "ours".
    this.lastSetScrollTop = scroller.scrollTop;
    return true;
  }
}

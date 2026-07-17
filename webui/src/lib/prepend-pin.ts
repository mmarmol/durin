/** Sustained scroll anchoring for older-history prepends.
 *
 * A one-shot scrollHeight-delta compensation is not enough on real pages: the
 * DOM keeps growing AFTER the first post-prepend layout frame (progressive
 * markdown/image layout), so a single restore lands the view off by exactly
 * the post-snapshot growth. Instead, a `PrependPin` records a pre-prepend
 * anchor element and its viewport position, and re-restores that element to
 * the recorded position on every layout tick until the position is stable,
 * the user scrolls, the anchor unmounts without a re-acquirable replacement,
 * or a hard deadline passes.
 */

/** Positions within this epsilon count as "no adjustment needed". */
const STABLE_EPSILON_PX = 1;
/** Release after this many consecutive no-adjustment ticks. */
const STABLE_TICKS_TO_RELEASE = 2;
/** Hard cap on the pinning window, so a pathologically restless layout can
 *  never leave the pin fighting the user's own scrolling forever. */
export const PIN_MAX_MS = 1500;

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
  private stableTicks = 0;
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
   *  tick. Returns whether the pin stays active; `false` when the deadline
   *  passed, the anchor unmounted with no re-acquirable replacement, the
   *  user scrolled, or the position has been stable for two consecutive
   *  ticks. */
  apply(scroller: PinScroller, now: number): boolean {
    if (this.deadline === null) {
      this.deadline = now + this.maxMs;
    } else if (now > this.deadline) {
      return false;
    }
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
      this.stableTicks += 1;
      return this.stableTicks < STABLE_TICKS_TO_RELEASE;
    }
    this.stableTicks = 0;
    scroller.scrollTop += delta;
    // Read back rather than trusting the assignment: the browser clamps to
    // the scrollable range, and the clamped value is what the next scroll
    // event will report as "ours".
    this.lastSetScrollTop = scroller.scrollTop;
    return true;
  }
}

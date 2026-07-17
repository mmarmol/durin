/** Sustained scroll anchoring for older-history prepends.
 *
 * A one-shot scrollHeight-delta compensation is not enough on real pages: the
 * DOM keeps growing AFTER the first post-prepend layout frame (progressive
 * markdown/image layout), so a single restore lands the view off by exactly
 * the post-snapshot growth. Instead, a `PrependPin` records a pre-prepend
 * anchor element and its viewport position, and re-restores that element to
 * the recorded position on every layout tick until genuinely quiet, the
 * user scrolls, or the anchor unmounts without a re-acquirable replacement.
 *
 * Release is measured in EXECUTED ticks plus time since the last actual
 * adjustment — never in wall-clock time since the pin started. A huge
 * prepend's own layout burst can block the main thread for seconds, during
 * which no tick (observer or interval) can run: wall-clock counts that
 * starvation against the window and expires the pin exactly when it is
 * needed most, right before the async late reflow (image fallbacks after
 * 404s, markdown settling) finally lands. Blocked time produces no ticks,
 * so a tick-counted window survives it; when execution resumes, the late
 * reflow triggers an adjusting restore which resets the window; genuine
 * quiet (several executed no-op ticks over a real time span) releases.
 */

/** Positions within this epsilon count as "no adjustment needed this tick". */
const STABLE_EPSILON_PX = 1;
/** Two callers can observe the same commit (the prepend's layout effect plus
 *  a ResizeObserver tick, or back-to-back content-identical commits); a
 *  second apply within this gap re-reads geometry the browser has not
 *  re-laid-out yet, so it is skipped as a no-op. */
const MIN_APPLY_GAP_MS = 4;
/** Executed no-adjustment ticks required before the pin may release. */
const RELEASE_QUIET_TICKS = 4;
/** Minimum time since the last actual adjustment before the pin may release. */
const RELEASE_QUIET_MS = 600;
/** Pathological ceiling on the whole pinning window. Generous by design: the
 *  tick-counted quiet release above is the real exit, and this cap exists
 *  only so a runaway layout can never hold the pin forever. */
const PIN_MAX_MS = 8000;
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
  /** Timestamp of the last apply that actually changed scrollTop (seeded by
   *  the first executed restore tick). */
  private lastAdjustAt: number | null = null;
  /** Executed apply ticks since the last actual adjustment. */
  private ticksSinceLastAdjust = 0;
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
   *  window is genuinely quiet (RELEASE_QUIET_TICKS executed no-adjustment
   *  ticks AND RELEASE_QUIET_MS since the last actual adjustment), the
   *  anchor unmounted with no re-acquirable replacement, the user scrolled,
   *  or the pathological ceiling passed. Wall-clock alone never releases: a
   *  blocked main thread produces no ticks, so starvation cannot expire the
   *  pin while nothing was able to run. */
  apply(scroller: PinScroller, now: number): boolean {
    if (this.deadline === null) {
      this.deadline = now + this.maxMs;
    } else if (now > this.deadline) {
      return false;
    }
    // Same-frame duplicate tick: geometry cannot have changed — no-op that
    // deliberately does NOT count as an executed tick.
    if (this.lastApplyAt !== null && now - this.lastApplyAt < MIN_APPLY_GAP_MS) {
      return true;
    }
    this.lastApplyAt = now;
    if (this.lastAdjustAt === null) this.lastAdjustAt = now;
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
      // Nothing to adjust this tick. Release only on genuine quiet: enough
      // EXECUTED no-op ticks over a real time span since the last
      // adjustment — late reflow after a starvation gap still finds the pin
      // alive, and its adjusting restore resets this window.
      this.ticksSinceLastAdjust += 1;
      return (
        this.ticksSinceLastAdjust < RELEASE_QUIET_TICKS
        || now - this.lastAdjustAt < RELEASE_QUIET_MS
      );
    }
    scroller.scrollTop += delta;
    // Read back rather than trusting the assignment: the browser clamps to
    // the scrollable range, and the clamped value is what the next scroll
    // event will report as "ours".
    this.lastSetScrollTop = scroller.scrollTop;
    this.lastAdjustAt = now;
    this.ticksSinceLastAdjust = 0;
    return true;
  }
}

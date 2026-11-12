"""Goal pacing (2026-10-19): the trader knows its goal and its clock, and trades toward the goal.

Every decision it compares the plan's net return with where it should be by now — the aim spread over the part of the
window in which it may still buy (no new buys in the last 15%) — and adjusts how picky it is and how big it buys:

  protect  goal reached           → much pickier (+0.12), half-size buys, tighter trailing stops on what it holds
  ahead    well above pace        → a bit pickier (+0.04), 0.8× buys — don't give the lead back
  behind   well below pace, time  → a bit less picky (down to −0.06) — but never in a bear market, never after
           left (>30%)              2+ recent stop-losses, never bigger buys, never past the plan's max-loss rules
  on pace                         → unchanged

The aim: your Plan target % (else your floor %), capped at what this risk × horizon can realistically make
(half a take-profit on the invested share). With no goal set, it paces toward half that, so every plan has a sensible aim.
A 100% goal on a one-week plan is therefore paced to the realistic ceiling, not chased.
"""
from dataclasses import dataclass

PICKIER_PROTECT, PICKIER_AHEAD, MAX_EASE = 0.12, 0.04, 0.06
BAND = 0.35          # "well above/below pace" = more than 35% of the aim away from the pace line


@dataclass
class Pace:
    mode: str            # protect | ahead | behind | on pace
    aim: float           # the return it paces toward (fraction)
    goal: float | None   # what you set (fraction), if anything
    expected: float      # where it should be by now
    entry_adj: float
    size_mult: float
    note: str

    def to_dict(self):
        return {"mode": self.mode, "aim": round(self.aim, 4), "goal": None if self.goal is None else round(self.goal, 4),
                "expected": round(self.expected, 4), "entry_adj": round(self.entry_adj, 3),
                "size": round(self.size_mult, 2), "note": self.note}


def aim_for(prof, min_pct, max_pct):
    # one full take-profit on the whole invested budget is the best case, not a plan; half of it is a strong window
    ceiling = prof.take_profit * prof.invest_frac * 0.5
    goal = max_pct if max_pct is not None else min_pct
    if goal is None:
        return ceiling * 0.5, None
    return min(goal / 100, ceiling), goal / 100


def pace(prof, min_pct, max_pct, ret, elapsed, regime, recent_stops) -> Pace:
    """elapsed = share of the plan's window already used (0..1)."""
    aim, goal = aim_for(prof, min_pct, max_pct)
    aim = max(aim, 0.002)
    expected = aim * min(1.0, max(0.0, elapsed) / 0.85)
    gap = ret - expected
    capped = goal is not None and goal > aim + 1e-9
    tag = f"aim {aim:+.1%}" + (f" (your {goal:.0%} is capped to what this mode can realistically make)" if capped else "")
    if ret >= aim:
        return Pace("protect", aim, goal, expected, PICKIER_PROTECT, 0.5,
                 f"{tag} reached at {ret:+.2%} — protecting gains: only very strong buys, half size, tighter trailing")
    if gap >= BAND * aim:
        return Pace("ahead", aim, goal, expected, PICKIER_AHEAD, 0.8,
                    f"{tag}: ahead of pace ({ret:+.2%} vs {expected:+.2%} by now) — a little pickier")
    if -gap >= BAND * aim:
        if elapsed > 0.7:
            why = "too little time left to chase"
        elif regime == "bear":
            why = "bear market, not chasing"
        elif recent_stops >= 2:
            why = "recent stop-losses, not chasing"
        else:
            ease = min(MAX_EASE, MAX_EASE * (-gap) / aim)
            return Pace("behind", aim, goal, expected, -ease, 1.0,
                        f"{tag}: behind pace ({ret:+.2%} vs {expected:+.2%} by now) — buy bar lowered {ease:.2f}")
        return Pace("on pace", aim, goal, expected, 0.0, 1.0, f"{tag}: behind pace ({ret:+.2%} vs {expected:+.2%}), {why}")
    return Pace("on pace", aim, goal, expected, 0.0, 1.0, f"{tag}: on pace ({ret:+.2%} vs {expected:+.2%} by now)")

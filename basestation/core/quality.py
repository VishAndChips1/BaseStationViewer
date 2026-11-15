"""Choosing how many points to ask the rig for.

The rig can send a thinned cloud (`GET /latest.bin?n=20000`) and advertises the
sizes it supports in an `X-Quality-Levels` header. Over a weak WiFi link the full
cloud can take several seconds to download, which caps the refresh rate far below
what the rig produces; a smaller cloud refreshes several times faster.

`choose_level` is the Auto mode: it keeps each download inside a time budget and
moves slowly, so the picture does not flip between sizes.
"""

AUTO_BUDGET_S = 1.0        # aim for a download of at most this long (about 1 Hz or better)
HEADROOM = 0.6             # to step UP, the larger size must fit in this fraction of the budget


def parse_levels(header) -> list[int]:
    """Sorted unique sizes from "8000,20000,40000"; anything unusable is skipped."""
    levels = set()
    for part in str(header or "").split(","):
        part = part.strip()
        if part.isdigit() and 0 < int(part) <= 50_000_000:
            levels.add(int(part))
    return sorted(levels)


def choose_level(levels, bytes_per_point: float, throughput: float, current=None,
                 budget: float = AUTO_BUDGET_S):
    """The size to request next, or None when there is nothing to choose.

    `throughput` is measured bytes/second over the link (including the rig's
    own response time, so it errs on the cautious side).
    """
    if not levels or bytes_per_point <= 0 or throughput <= 0:
        return current if current in (levels or ()) else (levels[0] if levels else None)

    def seconds(level):
        return level * bytes_per_point / throughput

    if current not in levels or seconds(current) > budget:
        fits = [lv for lv in levels if seconds(lv) <= budget]
        return max(fits) if fits else levels[0]            # too slow for any: the smallest
    roomy = [lv for lv in levels if seconds(lv) <= budget * HEADROOM]
    return max(roomy) if roomy and max(roomy) > current else current

"""How progress reads: counts and durations, for the stages' progress and the status alike (P-31).

Stage code uses them, so they're apart from the status's view (`browl.job_view`), and from `browl.jobtypes`, which
pre-caching's build keys cover (DESIGN.md §2.5, Build keys).
"""


def count_of(done: int, total: int | None, noun: str) -> str:
    """Return a count of a total, with its percentage, or just the count if the total isn't known.

    >>> count_of(640_000, 1_167_135, "triples"), count_of(640_000, None, "triples")
    ('640,000 of 1,167,135 triples (55%)', '640,000 triples')
    """
    if not total:
        return f"{done:,} {noun}"
    return f"{done:,} of {total:,} {noun} ({min(100, round(100 * done / total))}%)"


def format_duration(seconds: float) -> str:
    """Return a duration, rounded down to whole seconds, in at most its two largest units.

    >>> [format_duration(s) for s in (0.4, 59, 72, 3600, 3725)]
    ['0 s', '59 s', '1 min 12 s', '1 h', '1 h 2 min']
    """
    whole = int(seconds)
    hours, minutes, secs = whole // 3600, whole // 60 % 60, whole % 60
    if hours:
        return f"{hours} h {minutes} min" if minutes else f"{hours} h"
    if minutes:
        return f"{minutes} min {secs} s" if secs else f"{minutes} min"
    return f"{secs} s"

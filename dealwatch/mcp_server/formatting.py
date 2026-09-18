"""Money/time display conventions shared by every MCP tool (design.md §15,
"Conventions shared by every tool"): every price is both an int and a
display string, computed here, never by the model dividing by 100; every
timestamp is an epoch int plus an LA-local display string plus a
human age.

Deliberately small, independent implementations - not imports from
reporting/panels.py's _price_display()/_datetime_display(), which are
private to that module. V1.0 is scoped not to touch reporting/ at all (see
the milestone's out-of-scope list), so a private helper there isn't
available to import even if it did the identical job. Same output
convention by design agreement, not by shared code - if the two ever need
to be unified, that's its own small refactor, not something to force here.
"""

from datetime import datetime

from dealwatch.providers.ratelimit import PACIFIC


def money_display(cents: int | None) -> str | None:
    """"$215.00" from integer cents, or None when there is nothing to show.
    Every tool response pairs this with the raw `*_cents` field - the model
    never divides by 100 itself."""
    return f"${cents / 100:,.2f}" if cents is not None else None


def time_display(ts: int | None) -> str | None:
    """Short LA-local date/time string ("Sep 16 14:32"), or None for a
    missing timestamp. Timezone math belongs here, in tested Python, not
    left for the model to reconstruct from a bare epoch int."""
    if ts is None:
        return None
    return datetime.fromtimestamp(ts, PACIFIC).strftime("%b %-d %H:%M")


def age_display(ts: int | None, now: int) -> str | None:
    """"3h 12m ago" from a Unix-second timestamp and the current time, or
    None when ts itself is None - the same "absent is not zero" discipline
    reporting/status.py's own _age_mins() already uses, so a tool never
    reports "0m ago" for something that was never measured at all."""
    if ts is None:
        return None
    delta_seconds = max(now - ts, 0)
    minutes = delta_seconds // 60
    if minutes < 1:
        return f"{delta_seconds}s ago"
    hours, minutes = divmod(minutes, 60)
    if hours < 1:
        return f"{minutes}m ago"
    return f"{hours}h {minutes}m ago"


def parse_iso(value: object) -> datetime | None:
    """eBay's raw_json timestamps (e.g. itemCreationDate) are ISO 8601
    with a trailing "Z" - datetime.fromisoformat() parses that natively at
    Python >=3.11 (this project requires >=3.12, pyproject.toml). Returns
    None for anything else and never raises: a malformed or missing
    timestamp in third-party JSON must not break a tool call."""
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None

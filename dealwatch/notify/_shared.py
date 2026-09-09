"""Rendering helpers shared by notify/discord.py and notify/pushover.py
(V0.9a). Not a notifier abstraction - CLAUDE.md's "two concrete
implementations behind a dict, nothing more" instruction is about the send
path, not this - just the one rendering rule both notifiers need
identically: a spec field that's missing, or present but None (a
legitimate runtime state - a `partial` listing, per AlertsConfig's
docstring), renders as "unknown", never raises and never prints the
literal string "None". Pulled out once a second notifier needed the exact
same rule, rather than left to be copied by hand and drift the way
CLAUDE.md's collector/backfill normalization paths once did.
"""


class RenderSpec(dict):
    """A spec dict for str.format_map() where a missing key, OR a key whose
    value is None, both render as "unknown"."""

    def __missing__(self, key: str) -> str:
        return "unknown"

    def __getitem__(self, key: str):
        value = super().__getitem__(key)
        return "unknown" if value is None else value


def render_title(template: str, spec: dict) -> str:
    return template.format_map(RenderSpec(spec))


def render_field_value(spec: dict, name: str) -> str:
    value = spec.get(name)
    return "unknown" if value is None else str(value)

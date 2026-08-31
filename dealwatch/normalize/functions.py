"""Named transforms for extract rules' `apply:` field.

FUNCTIONS maps a name as written in profiles/*.yaml to a callable. An
`apply:` naming anything not in this dict is a profile load-time error
(dealwatch.normalize.engine.compile_profile) - never a silent pass-through
of the unconverted string.
"""

from typing import Callable


def to_int(value: str) -> int:
    return int(value)


def tb_to_gb(value: str) -> int:
    return int(value) * 1024


FUNCTIONS: dict[str, Callable[[str], object]] = {
    "to_int": to_int,
    "tb_to_gb": tb_to_gb,
}

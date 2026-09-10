"""Fixture module C: import fallback (unresolved attr call)."""

from . import d


def use() -> None:
    d.not_a_real_function()

"""Fixture module A: calls into B, plus a same-file helper."""

from .b import foo


def local_helper() -> int:
    return 1


def caller() -> int:
    local_helper()
    return foo()

"""Shim — use token_telemetry.live_dashboard.

Keeps ``python scripts/live_dashboard.py`` and ``from live_dashboard import ...``
working. When imported, replaces this module with the package module so private
names remain available.
"""
from importlib import import_module
import sys

_mod = import_module("token_telemetry.live_dashboard")

if __name__ == "__main__":
    raise SystemExit(_mod.main())

sys.modules[__name__] = _mod

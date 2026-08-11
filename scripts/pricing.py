"""Shim — use token_telemetry.pricing.

Replaces this module in ``sys.modules`` so ``from pricing import _private``
and star-imports keep working for tests and legacy scripts.
"""
from importlib import import_module
import sys

sys.modules[__name__] = import_module("token_telemetry.pricing")

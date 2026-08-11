"""Shim — use token_telemetry.tokenizer.

Replaces this module in ``sys.modules`` so ``from tokenizer import _private``
and star-imports keep working for tests and legacy scripts.
"""
from importlib import import_module
import sys

sys.modules[__name__] = import_module("token_telemetry.tokenizer")

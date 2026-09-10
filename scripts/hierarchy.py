"""Shim — use token_telemetry.hierarchy.

Replaces this module in ``sys.modules`` so ``from hierarchy import HierarchyBuilder``
keeps working for legacy scripts. Private helpers live on their owning modules.
"""
from importlib import import_module
import sys

sys.modules[__name__] = import_module("token_telemetry.hierarchy")

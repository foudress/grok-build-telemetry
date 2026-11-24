"""CLI entry for ``metryx`` and ``python -m token_telemetry``."""

from __future__ import annotations

from typing import Optional

from token_telemetry.live_dashboard import main as _dashboard_main


def main(argv: Optional[list[str]] = None) -> int:
    return int(_dashboard_main(argv))


if __name__ == "__main__":
    raise SystemExit(main())

"""Antigravity connector endpoints for the dashboard."""

from token_telemetry.antigravity.adapt import build_period, sessions_for_ui, state_for
from token_telemetry.antigravity.store import (
    catalog_rows,
    clear_cache,
    focused_snapshot,
    load_snapshot,
    source_revision,
)
from token_telemetry.session.connector_feed import ConnectorFeed


class AntigravityFeed(ConnectorFeed):
    def __init__(self) -> None:
        super().__init__(
            "antigravity", catalog=catalog_rows, focus=focused_snapshot,
            history=load_snapshot, revision=source_revision, clear=clear_cache,
            sessions=sessions_for_ui, state=state_for, period=build_period,
        )


FEED = AntigravityFeed()

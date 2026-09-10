"""Feature gates: Python constants stay in sync with dashboard/js/features.js."""

from __future__ import annotations

import re
from pathlib import Path

from token_telemetry import features as feat

_JS_TO_PY = {
    "mutationHistory": "MUTATION_HISTORY",
    "gantt": "GANTT",
    "toksPerSec": "TOKS_PER_SEC",
    "agentAnimationGraph": "AGENT_ANIMATION_GRAPH",
    "periodIoPriceStep": "PERIOD_IO_PRICE_STEP",
}

_FEATURES_JS = (
    Path(__file__).resolve().parents[1] / "dashboard" / "js" / "features.js"
)


def test_v1_gates_default_off():
    assert feat.MUTATION_HISTORY is False
    assert feat.GANTT is False
    assert feat.TOKS_PER_SEC is False
    assert feat.AGENT_ANIMATION_GRAPH is False
    assert feat.PERIOD_IO_PRICE_STEP is False


def test_features_js_keys_match_python():
    js = _FEATURES_JS.read_text(encoding="utf-8")
    m = re.search(r"export const FEATURES = Object\.freeze\(\{([\s\S]*?)\}\);", js)
    assert m, "FEATURES object missing from features.js"
    found: dict[str, bool] = {}
    for km in re.finditer(r"^\s*(\w+):\s*(true|false)\s*,?\s*$", m.group(1), re.M):
        found[km.group(1)] = km.group(2) == "true"
    assert set(found) == set(_JS_TO_PY)
    for js_key, py_name in _JS_TO_PY.items():
        assert found[js_key] is bool(getattr(feat, py_name)), js_key


def test_wip_html_is_gated_stub():
    html = feat.wip_html("Mutation History").decode("utf-8")
    assert "Mutation History" in html
    assert feat.WIP_NOTE in html
    assert 'href="/"' in html

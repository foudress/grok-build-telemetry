"""Feature gates for v1.0.0 polish surfaces."""

from token_telemetry import features as feat


def test_v1_gates_default_off():
    assert feat.MUTATION_HISTORY is False
    assert feat.GANTT is False
    assert feat.TOKS_PER_SEC is False
    assert feat.AGENT_ANIMATION_GRAPH is False
    assert feat.PERIOD_IO_PRICE_STEP is False


def test_wip_html_mentions_polish():
    html = feat.wip_html("Mutation History").decode("utf-8")
    assert "Mutation History" in html
    assert "polish" in html.lower()
    assert 'href="/"' in html

import importlib.util
import json
from pathlib import Path


def _load_tools_module():
    path = Path("skills/invest/scripts/tools.py").resolve()
    spec = importlib.util.spec_from_file_location("invest_tools_test", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


# ---------------------------------------------------------------------------
# Default graph
# ---------------------------------------------------------------------------

DESIGN_NODE_IDS = {
    "fed_liquidity", "sofr_level", "move_index", "yen_carry",
    "geo_risk", "trade_risk", "fed_policy_shift", "china_stimulus",
    "northbound_flow", "market_turnover", "margin_trading", "southbound_flow",
    "valuation_regime", "breakout_signal", "gold_anomaly",
    "us_equity", "a_share", "hk_equity", "gold", "crude_oil", "us_bond",
    "usd", "btc", "copper",
}


def test_default_graph_contains_all_design_nodes():
    tools = _load_tools_module()
    graph = tools._default_graph()
    assert set(graph["nodes"].keys()) == DESIGN_NODE_IDS


def test_default_graph_geo_risk_has_extreme_state():
    tools = _load_tools_module()
    graph = tools._default_graph()
    assert "extreme" in graph["nodes"]["geo_risk"]["states"]


def test_default_graph_northbound_flow_has_heavy_variants():
    tools = _load_tools_module()
    graph = tools._default_graph()
    states = graph["nodes"]["northbound_flow"]["states"]
    assert "heavy_outflow" in states
    assert "heavy_inflow" in states


def test_default_graph_valuation_uses_rich_not_expensive():
    tools = _load_tools_module()
    graph = tools._default_graph()
    states = graph["nodes"]["valuation_regime"]["states"]
    assert "rich" in states
    assert "expensive" not in states


def test_default_graph_has_chains():
    tools = _load_tools_module()
    graph = tools._default_graph()
    assert any(c["label"] == "Fed tightening to A-share" for c in graph["chains"])


# ---------------------------------------------------------------------------
# _node_effective
# ---------------------------------------------------------------------------

def test_node_effective_prefers_analyst_state():
    tools = _load_tools_module()
    node = {
        "id": "geo_risk",
        "states": ["low", "medium", "high", "extreme"],
        "observed_state": "medium",
        "observed_confidence": 0.6,
        "analyst_state": "high",
        "analyst_confidence": 0.75,
    }
    state, confidence, source = tools._node_effective(node)
    assert state == "high"
    assert confidence == 0.75
    assert source == "analyst"


def test_node_effective_falls_back_to_observed():
    tools = _load_tools_module()
    node = {
        "id": "geo_risk",
        "states": ["low", "medium", "high"],
        "observed_state": "medium",
        "observed_confidence": 0.6,
    }
    state, confidence, source = tools._node_effective(node)
    assert state == "medium"
    assert source == "observed"


def test_node_effective_returns_none_when_empty():
    tools = _load_tools_module()
    node = {"id": "test", "states": ["a", "b"]}
    state, confidence, source = tools._node_effective(node)
    assert state is None
    assert source == "none"


# ---------------------------------------------------------------------------
# _confidence
# ---------------------------------------------------------------------------

def test_confidence_clamps_low():
    tools = _load_tools_module()
    assert tools._confidence(0.0) == 0.05
    assert tools._confidence(-1.0) == 0.05


def test_confidence_clamps_high():
    tools = _load_tools_module()
    assert tools._confidence(1.0) == 0.99
    assert tools._confidence(2.0) == 0.99


def test_confidence_passthrough():
    tools = _load_tools_module()
    assert tools._confidence(0.5) == 0.5


# ---------------------------------------------------------------------------
# _validate_node_state
# ---------------------------------------------------------------------------

def test_validate_node_state_rejects_invalid():
    tools = _load_tools_module()
    node = {"id": "test", "states": ["a", "b"]}
    try:
        tools._validate_node_state(node, "c")
        assert False, "Should have raised ValueError"
    except ValueError as exc:
        assert "Invalid state" in str(exc)


def test_validate_node_state_accepts_valid():
    tools = _load_tools_module()
    node = {"id": "test", "states": ["a", "b"]}
    tools._validate_node_state(node, "a")  # should not raise


# ---------------------------------------------------------------------------
# _map_china (M4: outflow thresholds)
# ---------------------------------------------------------------------------

def test_map_china_neutral_on_small_negative():
    tools = _load_tools_module()
    result = {"dimensions": {"northbound": {"latest": -5, "consecutive_outflow": 0, "risk_score": 50}}, "risk_score": 50}
    mapped = tools._map_china(result)
    assert mapped[0]["state"] == "neutral"


def test_map_china_outflow_on_large_negative():
    tools = _load_tools_module()
    result = {"dimensions": {"northbound": {"latest": -30, "consecutive_outflow": 0, "risk_score": 70}}, "risk_score": 70}
    mapped = tools._map_china(result)
    assert mapped[0]["state"] == "outflow"


def test_map_china_heavy_outflow_on_extreme():
    tools = _load_tools_module()
    result = {"dimensions": {"northbound": {"latest": -60, "consecutive_outflow": 0, "risk_score": 90}}, "risk_score": 90}
    mapped = tools._map_china(result)
    assert mapped[0]["state"] == "heavy_outflow"


def test_map_china_heavy_outflow_on_consecutive():
    tools = _load_tools_module()
    result = {"dimensions": {"northbound": {"latest": -10, "consecutive_outflow": 5, "risk_score": 80}}, "risk_score": 80}
    mapped = tools._map_china(result)
    assert mapped[0]["state"] == "heavy_outflow"


def test_map_china_inflow():
    tools = _load_tools_module()
    result = {"dimensions": {"northbound": {"latest": 30, "consecutive_outflow": 0, "risk_score": 30}}, "risk_score": 30}
    mapped = tools._map_china(result)
    assert mapped[0]["state"] == "inflow"


def test_map_china_heavy_inflow():
    tools = _load_tools_module()
    result = {"dimensions": {"northbound": {"latest": 60, "consecutive_outflow": 0, "risk_score": 20}}, "risk_score": 20}
    mapped = tools._map_china(result)
    assert mapped[0]["state"] == "heavy_inflow"


# ---------------------------------------------------------------------------
# _preview_chain (M5: source field)
# ---------------------------------------------------------------------------

def test_preview_chain_source_reflects_analyst():
    tools = _load_tools_module()
    graph = tools._default_graph()
    graph["nodes"]["geo_risk"]["analyst_state"] = "high"
    graph["nodes"]["geo_risk"]["analyst_confidence"] = 0.8

    preview = tools._preview_chain(graph, ["geo_risk", "gold"])
    first = preview["path_detail"][0]
    assert first["source"] == "analyst"
    assert "observed" not in first


def test_preview_chain_source_reflects_observed():
    tools = _load_tools_module()
    graph = tools._default_graph()
    graph["nodes"]["geo_risk"]["observed_state"] = "medium"
    graph["nodes"]["geo_risk"]["observed_confidence"] = 0.6

    preview = tools._preview_chain(graph, ["geo_risk", "gold"])
    first = preview["path_detail"][0]
    assert first["source"] == "observed"


# ---------------------------------------------------------------------------
# _infer (H2: dedup + basic)
# ---------------------------------------------------------------------------

def test_infer_generates_asset_summary_from_default_chain():
    tools = _load_tools_module()
    graph = tools._default_graph()
    graph["nodes"]["fed_liquidity"]["observed_state"] = "tight"
    graph["nodes"]["fed_liquidity"]["observed_confidence"] = 0.9

    result = tools._infer(graph, top_n=5)
    assert result["top_chains"]
    assert "a_share" in result["asset_summary"]
    assert result["asset_summary"]["a_share"]["bearish"] > 0


def test_infer_same_root_different_first_hop_both_survive():
    """Chains from same root can both contribute when they diverge at the first hop."""
    tools = _load_tools_module()
    graph = tools._default_graph()

    # Set up fed_liquidity as observed
    graph["nodes"]["fed_liquidity"]["observed_state"] = "tight"
    graph["nodes"]["fed_liquidity"]["observed_confidence"] = 0.9

    # Add a second chain from fed_liquidity to a_share via a different path
    # First add the edge
    graph["edges"]["fed_liquidity->a_share"] = {
        "from": "fed_liquidity",
        "to": "a_share",
        "probabilities": {"tight->bearish": 0.50},
        "method": "test",
        "reason": "test",
        "updated_at": "2026-01-01T00:00:00Z",
    }
    graph["chains"].append({
        "id": 99,
        "path": ["fed_liquidity", "a_share"],
        "label": "Direct fed to A-share",
        "reasoning": "test",
        "updated_at": "2026-01-01T00:00:00Z",
    })

    result = tools._infer(graph, top_n=10)

    # These two chains diverge at the first hop, so both should remain after dedup.
    fed_a_share_bearish = [
        c for c in result["top_chains"]
        if c["target"] == "a_share" and c["target_state"] == "bearish" and c["root_node"] == "fed_liquidity"
    ]
    assert len(fed_a_share_bearish) >= 2


def test_infer_different_roots_both_contribute():
    """Chains from different roots targeting same asset should both contribute."""
    tools = _load_tools_module()
    graph = tools._default_graph()

    graph["nodes"]["fed_liquidity"]["observed_state"] = "tight"
    graph["nodes"]["fed_liquidity"]["observed_confidence"] = 0.9
    graph["nodes"]["geo_risk"]["observed_state"] = "high"
    graph["nodes"]["geo_risk"]["observed_confidence"] = 0.8

    result = tools._infer(graph, top_n=10)

    # Both fed chain and geo chain should contribute to a_share
    targets = {c["root_node"] for c in result["top_chains"] if c["target"] == "a_share"}
    assert "fed_liquidity" in targets
    assert "geo_risk" in targets


# ---------------------------------------------------------------------------
# cmd_node (H1: persist new node without --state)
# ---------------------------------------------------------------------------

def test_cmd_node_create_without_state_persists(tmp_path, monkeypatch):
    tools = _load_tools_module()
    monkeypatch.setattr(tools, "_workspace_root", lambda: tmp_path)

    import argparse
    args = argparse.Namespace(
        check=False, id="test_node", state=None, confidence=None,
        reason=None, type="event", states="low,medium,high", label="Test Node",
    )
    tools.cmd_node(args)

    # Verify the node was persisted to disk
    graph = json.loads((tmp_path / ".invest" / "graph.json").read_text())
    assert "test_node" in graph["nodes"]
    assert graph["nodes"]["test_node"]["origin"] == "dynamic"


def test_cmd_node_create_with_state_persists(tmp_path, monkeypatch):
    tools = _load_tools_module()
    monkeypatch.setattr(tools, "_workspace_root", lambda: tmp_path)

    import argparse
    args = argparse.Namespace(
        check=False, id="test_node", state="medium", confidence=0.8,
        reason="test reason", type="event", states="low,medium,high", label="Test Node",
    )
    tools.cmd_node(args)

    graph = json.loads((tmp_path / ".invest" / "graph.json").read_text())
    assert "test_node" in graph["nodes"]
    assert graph["nodes"]["test_node"]["analyst_state"] == "medium"
    assert graph["nodes"]["test_node"]["analyst_confidence"] == 0.8


# ---------------------------------------------------------------------------
# cmd_edge (M3: merge probabilities)
# ---------------------------------------------------------------------------

def test_cmd_edge_merges_probabilities(tmp_path, monkeypatch, capsys):
    tools = _load_tools_module()
    monkeypatch.setattr(tools, "_workspace_root", lambda: tmp_path)

    # Initialize store so graph exists
    tools._ensure_store()

    import argparse

    # Update one probability on existing edge
    args = argparse.Namespace(
        from_id="fed_liquidity", to="sofr_level",
        prob='{"tight->elevated": 0.95}', reason="updated",
    )
    tools.cmd_edge(args)

    # Read back the graph
    graph = json.loads((tmp_path / ".invest" / "graph.json").read_text())
    edge = graph["edges"]["fed_liquidity->sofr_level"]

    # New probability should be updated
    assert edge["probabilities"]["tight->elevated"] == 0.95
    # Old probabilities should still exist
    assert "abundant->low" in edge["probabilities"]
    assert "normal->normal" in edge["probabilities"]
    assert "crisis->dangerous" in edge["probabilities"]


def test_cmd_edge_rejects_invalid_state_transition(tmp_path, monkeypatch, capsys):
    tools = _load_tools_module()
    monkeypatch.setattr(tools, "_workspace_root", lambda: tmp_path)
    tools._ensure_store()

    import argparse

    args = argparse.Namespace(
        from_id="fed_liquidity", to="sofr_level",
        prob='{"tight->bullish": 0.95}', reason="bad transition",
    )
    tools.cmd_edge(args)

    output = json.loads(capsys.readouterr().out)
    assert output["ok"] is False
    assert "Invalid target state" in output["error"]


def test_cmd_edge_rejects_out_of_range_probability(tmp_path, monkeypatch, capsys):
    tools = _load_tools_module()
    monkeypatch.setattr(tools, "_workspace_root", lambda: tmp_path)
    tools._ensure_store()

    import argparse

    args = argparse.Namespace(
        from_id="fed_liquidity", to="sofr_level",
        prob='{"tight->elevated": 1.2}', reason="bad probability",
    )
    tools.cmd_edge(args)

    output = json.loads(capsys.readouterr().out)
    assert output["ok"] is False
    assert "must be within [0, 1]" in output["error"]


def test_cmd_chain_rejects_missing_edge(tmp_path, monkeypatch, capsys):
    tools = _load_tools_module()
    monkeypatch.setattr(tools, "_workspace_root", lambda: tmp_path)
    tools._ensure_store()

    import argparse

    args = argparse.Namespace(
        path="fed_liquidity -> gold", label="Invalid", reasoning="missing edge",
        preview=False, list=False, remove=None,
    )
    tools.cmd_chain(args)

    output = json.loads(capsys.readouterr().out)
    assert output["ok"] is False
    assert "Missing edge fed_liquidity->gold" in output["error"]

    graph = json.loads((tmp_path / ".invest" / "graph.json").read_text())
    assert all(chain["label"] != "Invalid" for chain in graph["chains"])


def test_infer_returns_skipped_invalid_chain():
    tools = _load_tools_module()
    graph = tools._default_graph()
    graph["nodes"]["fed_liquidity"]["observed_state"] = "tight"
    graph["nodes"]["fed_liquidity"]["observed_confidence"] = 0.9
    graph["chains"].append({
        "id": 999,
        "path": ["fed_liquidity", "gold"],
        "label": "Broken chain",
        "reasoning": "missing edge",
        "updated_at": "2026-01-01T00:00:00Z",
    })

    result = tools._infer(graph, top_n=10)

    assert any(chain["id"] == 999 for chain in result["skipped_chains"])


def test_infer_supports_long_registered_chain():
    tools = _load_tools_module()
    graph = tools._default_graph()
    graph["nodes"]["fed_liquidity"]["observed_state"] = "tight"
    graph["nodes"]["fed_liquidity"]["observed_confidence"] = 0.9

    result = tools._infer(graph, top_n=10)

    assert any(chain["path_length"] >= 5 for chain in result["top_chains"])


def test_infer_same_root_different_first_hop_both_contribute():
    tools = _load_tools_module()
    graph = tools._default_graph()

    graph["nodes"]["fed_liquidity"]["observed_state"] = "tight"
    graph["nodes"]["fed_liquidity"]["observed_confidence"] = 0.9

    graph["edges"]["fed_liquidity->move_index"] = {
        "from": "fed_liquidity",
        "to": "move_index",
        "probabilities": {"tight->elevated": 0.7},
        "method": "test",
        "reason": "test",
        "updated_at": "2026-01-01T00:00:00Z",
    }
    graph["edges"]["move_index->a_share"] = {
        "from": "move_index",
        "to": "a_share",
        "probabilities": {"elevated->bearish": 0.6},
        "method": "test",
        "reason": "test",
        "updated_at": "2026-01-01T00:00:00Z",
    }
    graph["chains"].append({
        "id": 88,
        "path": ["fed_liquidity", "move_index", "a_share"],
        "label": "Fed via MOVE to A-share",
        "reasoning": "test",
        "updated_at": "2026-01-01T00:00:00Z",
    })

    result = tools._infer(graph, top_n=10)

    fed_a_share_bearish = [
        c for c in result["top_chains"]
        if c["target"] == "a_share" and c["target_state"] == "bearish" and c["root_node"] == "fed_liquidity"
    ]
    assert len(fed_a_share_bearish) >= 2

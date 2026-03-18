#!/usr/bin/env python3
"""Unified investment skill entrypoint."""

from __future__ import annotations

import argparse
import importlib.util
import io
import json
import logging
import os
import sys
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


@contextmanager
def _suppress_stderr():
    """Capture stderr from submodules so it doesn't leak to the terminal.

    Any logger.error() or print(..., file=sys.stderr) from dynamically loaded
    modules is swallowed here; the caller is expected to surface errors through
    the structured JSON output instead.
    """
    old_stderr = sys.stderr
    old_handlers: list = []
    root_logger = logging.getLogger()
    for handler in root_logger.handlers[:]:
        if isinstance(handler, logging.StreamHandler) and handler.stream is old_stderr:
            old_handlers.append(handler)
            root_logger.removeHandler(handler)
    sys.stderr = io.StringIO()
    try:
        yield sys.stderr
    finally:
        sys.stderr = old_stderr
        for handler in old_handlers:
            root_logger.addHandler(handler)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _skill_dir() -> Path:
    return Path(__file__).resolve().parents[1]


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[3]


def _workspace_root() -> Path:
    root = os.environ.get("WORKSPACE_ROOT")
    if root:
        return Path(root).expanduser().resolve()
    return Path.cwd().resolve()


def _data_dir() -> Path:
    return _workspace_root() / ".invest"


def _graph_path() -> Path:
    return _data_dir() / "graph.json"


def _signals_path() -> Path:
    return _data_dir() / "signals.json"


def _inference_path() -> Path:
    return _data_dir() / "inference.json"


def _session_path() -> Path:
    return _data_dir() / "session.json"


def _json_ok(data: Dict[str, Any]) -> None:
    print(json.dumps({"ok": True, "data": data}, ensure_ascii=False, indent=2))


def _json_error(message: str, *, details: Optional[Dict[str, Any]] = None) -> None:
    payload: Dict[str, Any] = {"ok": False, "error": message}
    if details:
        payload["details"] = details
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Failed to load module from {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _confidence(value: float) -> float:
    return round(max(0.05, min(0.99, value)), 2)


def _default_graph() -> Dict[str, Any]:
    path = Path(__file__).with_name("default_graph.json")
    base = json.loads(path.read_text(encoding="utf-8"))
    now = _utc_now()
    base["updated_at"] = now
    for edge in base["edges"].values():
        edge["updated_at"] = now
    for chain in base["chains"]:
        chain["updated_at"] = now
    return base


def _default_session() -> Dict[str, Any]:
    return {
        "created_at": _utc_now(),
        "updated_at": _utc_now(),
        "steps": [],
    }


def _ensure_store() -> None:
    data_dir = _data_dir()
    data_dir.mkdir(parents=True, exist_ok=True)
    if not _graph_path().exists():
        _save_json(_graph_path(), _default_graph())
    if not _session_path().exists():
        _save_json(_session_path(), _default_session())


def _load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return deepcopy(default)
    return json.loads(path.read_text(encoding="utf-8"))


def _save_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=False)
    path.write_text(text + "\n", encoding="utf-8")


def _load_graph() -> Dict[str, Any]:
    _ensure_store()
    return _load_json(_graph_path(), _default_graph())


def _save_graph(graph: Dict[str, Any]) -> None:
    graph["updated_at"] = _utc_now()
    _save_json(_graph_path(), graph)


def _load_session() -> Dict[str, Any]:
    _ensure_store()
    return _load_json(_session_path(), _default_session())


def _touch_session(step: str) -> None:
    session = _load_session()
    session["updated_at"] = _utc_now()
    session["steps"].append({"step": step, "timestamp": session["updated_at"]})
    _save_json(_session_path(), session)


def _node_effective(node: Dict[str, Any]) -> Tuple[Optional[str], Optional[float], str]:
    if node.get("analyst_state"):
        return node.get("analyst_state"), node.get("analyst_confidence"), "analyst"
    if node.get("observed_state"):
        return node.get("observed_state"), node.get("observed_confidence"), "observed"
    return None, None, "none"


def _validate_node_state(node: Dict[str, Any], state: str) -> None:
    states = node.get("states", [])
    if state not in states:
        raise ValueError(f"Invalid state '{state}' for node '{node['id']}', allowed: {states}")


def _find_edge(graph: Dict[str, Any], from_id: str, to_id: str) -> Optional[Dict[str, Any]]:
    return graph["edges"].get(f"{from_id}->{to_id}")


def _next_chain_id(graph: Dict[str, Any]) -> int:
    if not graph["chains"]:
        return 1
    return max(chain["id"] for chain in graph["chains"]) + 1


def _parse_probabilities(raw: str) -> Dict[str, float]:
    parsed = json.loads(raw)
    if not isinstance(parsed, dict):
        raise ValueError("--prob must be a JSON object")
    out: Dict[str, float] = {}
    for key, value in parsed.items():
        out[str(key)] = float(value)
    return out


def _validate_probabilities(
    graph: Dict[str, Any],
    from_id: str,
    to_id: str,
    probabilities: Dict[str, float],
) -> Dict[str, float]:
    if not probabilities:
        raise ValueError("--prob must not be empty")

    src_states = set(graph["nodes"][from_id]["states"])
    dst_states = set(graph["nodes"][to_id]["states"])
    validated: Dict[str, float] = {}

    for transition, raw_value in probabilities.items():
        if "->" not in transition:
            raise ValueError(f"Invalid transition '{transition}', expected '<src_state>-><dst_state>'")

        src_state, dst_state = (part.strip() for part in transition.split("->", 1))
        if src_state not in src_states:
            raise ValueError(
                f"Invalid source state '{src_state}' for node '{from_id}', allowed: {sorted(src_states)}"
            )
        if dst_state not in dst_states:
            raise ValueError(
                f"Invalid target state '{dst_state}' for node '{to_id}', allowed: {sorted(dst_states)}"
            )
        if not 0.0 <= raw_value <= 1.0:
            raise ValueError(f"Probability for '{transition}' must be within [0, 1]")

        validated[f"{src_state}->{dst_state}"] = round(float(raw_value), 4)

    return validated


def _validate_chain_path(graph: Dict[str, Any], path: List[str]) -> None:
    if len(path) < 2:
        raise ValueError("Path must contain at least two nodes")

    seen: set[str] = set()
    repeated: set[str] = set()
    for node_id in path:
        if node_id in seen:
            repeated.add(node_id)
        seen.add(node_id)
    if repeated:
        raise ValueError(f"Path contains a cycle or repeated node: {sorted(repeated)}")

    for node_id in path:
        if node_id not in graph["nodes"]:
            raise ValueError(f"Unknown node in path: {node_id}")

    for src, dst in zip(path, path[1:]):
        if _find_edge(graph, src, dst) is None:
            raise ValueError(f"Missing edge {src}->{dst}")


def _chain_branch_key(path: List[str]) -> str:
    if len(path) >= 2:
        return f"{path[0]}->{path[1]}"
    return path[0]


def _path_hops(path: List[str]) -> int:
    return max(0, len(path) - 1)


def _graph_adjacency(graph: Dict[str, Any]) -> Dict[str, List[str]]:
    adjacency: Dict[str, List[str]] = {}
    for edge in graph["edges"].values():
        adjacency.setdefault(edge["from"], []).append(edge["to"])
    for values in adjacency.values():
        values.sort()
    return adjacency


def _discover_chains(
    graph: Dict[str, Any],
    *,
    max_hops: int,
    targets: Optional[List[str]] = None,
    limit: int = 200,
) -> List[Dict[str, Any]]:
    if max_hops < 1:
        return []

    target_set = set(targets or [])
    adjacency = _graph_adjacency(graph)
    existing_paths = {tuple(chain["path"]) for chain in graph["chains"]}
    discovered: List[Dict[str, Any]] = []

    def should_keep(node_id: str) -> bool:
        if targets and node_id not in target_set:
            return False
        node = graph["nodes"].get(node_id, {})
        return node.get("type") == "asset"

    def dfs(path: List[str]) -> None:
        if len(discovered) >= limit:
            return

        current = path[-1]
        hops = len(path) - 1
        if hops >= 1 and should_keep(current) and tuple(path) not in existing_paths:
            discovered.append(
                {
                    "id": f"auto:{len(discovered) + 1}",
                    "path": list(path),
                    "label": "Auto-discovered: " + " -> ".join(path),
                    "reasoning": "Auto-discovered from graph connectivity",
                    "updated_at": _utc_now(),
                    "auto_discovered": True,
                }
            )
            return

        if hops >= max_hops:
            return

        for nxt in adjacency.get(current, []):
            if nxt in path:
                continue
            dfs(path + [nxt])

    for node_id, node in graph["nodes"].items():
        state, _, _ = _node_effective(node)
        if state is None:
            continue
        dfs([node_id])
        if len(discovered) >= limit:
            break

    return discovered


def _load_macro_result(lookback_days: int) -> Dict[str, Any]:
    path = Path(__file__).resolve().parent / "signals" / "macro_liquidity.py"
    module = _load_module("invest_macro_liquidity", path)
    analyzer = module.MacroLiquidityAnalyzer()
    return module._serialize_result(analyzer.analyze(lookback_days=lookback_days))


def _load_china_result(lookback_days: int) -> Dict[str, Any]:
    path = Path(__file__).resolve().parent / "signals" / "china_market_signal.py"
    module = _load_module("invest_china_market_signal", path)
    analyzer = module.ChinaMarketSignalAnalyzer()
    return module._serialize_result(analyzer.analyze(lookback_days=lookback_days))


def _load_rhino_result(max_age: int, top_n: int, lookback_days: int) -> Dict[str, Any]:
    path = _repo_root() / "skills" / "gray-rhino" / "scripts" / "rhino_report.py"
    module = _load_module("invest_rhino_report", path)
    history_dir = str(_data_dir() / "rhino_history")
    return module.generate_report(
        max_age_hours=max_age,
        top_n=top_n,
        lookback_days=lookback_days,
        history_dir=history_dir,
        save_snapshot=True,
    )


def _map_macro(result: Dict[str, Any]) -> List[Dict[str, Any]]:
    dims = result.get("dimensions", {})
    overall = result.get("status", "Normal")
    if overall == "Crisis":
        liquidity_state = "crisis"
    elif overall == "Tight":
        liquidity_state = "tight"
    elif overall == "Abundant":
        liquidity_state = "abundant"
    else:
        liquidity_state = "normal"

    sofr_current = dims.get("sofr", {}).get("current")
    sofr_state = "normal"
    if isinstance(sofr_current, (int, float)):
        if sofr_current >= 5.5:
            sofr_state = "dangerous"
        elif sofr_current >= 5.0:
            sofr_state = "elevated"
        elif sofr_current < 4.0:
            sofr_state = "low"

    base_conf = _confidence(result.get("risk_score", 50) / 100)
    sofr_conf = _confidence(dims.get("sofr", {}).get("risk_score", 50) / 100)

    return [
        {
            "id": "fed_liquidity",
            "state": liquidity_state,
            "confidence": base_conf,
            "freshness": "live",
            "source": "macro",
            "raw": result,
        },
        {
            "id": "sofr_level",
            "state": sofr_state,
            "confidence": sofr_conf,
            "freshness": "live",
            "source": "macro",
            "raw": dims.get("sofr", {}),
        },
    ]


def _map_china(result: Dict[str, Any]) -> List[Dict[str, Any]]:
    dims = result.get("dimensions", {})
    north = dims.get("northbound", {})
    latest = north.get("latest")
    consecutive = north.get("consecutive_outflow", 0)
    if consecutive >= 5 or (isinstance(latest, (int, float)) and latest < -50):
        flow_state = "heavy_outflow"
    elif consecutive >= 3 or (isinstance(latest, (int, float)) and latest < -20):
        flow_state = "outflow"
    elif isinstance(latest, (int, float)) and latest > 50:
        flow_state = "heavy_inflow"
    elif isinstance(latest, (int, float)) and latest > 20:
        flow_state = "inflow"
    else:
        flow_state = "neutral"

    return [
        {
            "id": "northbound_flow",
            "state": flow_state,
            "confidence": _confidence(north.get("risk_score", result.get("risk_score", 50)) / 100),
            "freshness": "live",
            "source": "china",
            "raw": north,
        }
    ]


def _map_rhino(result: Dict[str, Any]) -> List[Dict[str, Any]]:
    if not result.get("ok"):
        raise RuntimeError(result.get("error", "Rhino report failed"))

    geo_signals = [sig for sig in result.get("signals", []) if sig.get("category") == "geopolitics"]
    trade_signals = [sig for sig in result.get("signals", []) if sig.get("category") == "trade"]
    mapped: List[Dict[str, Any]] = []

    if geo_signals:
        top = geo_signals[0]
        score = float(top.get("trend_score", 0.0))
        if score >= 8:
            state = "high"
        elif score >= 4:
            state = "medium"
        else:
            state = "low"
        mapped.append(
            {
                "id": "geo_risk",
                "state": state,
                "confidence": _confidence(0.45 + min(score, 10.0) / 20.0),
                "freshness": "live",
                "source": "rhino",
                "raw": top,
            }
        )

    if trade_signals:
        top = trade_signals[0]
        score = float(top.get("trend_score", 0.0))
        state = "high" if score >= 8 else "medium" if score >= 4 else "low"
        mapped.append(
            {
                "id": "trade_risk",
                "state": state,
                "confidence": _confidence(0.45 + min(score, 10.0) / 20.0),
                "freshness": "live",
                "source": "rhino",
                "raw": top,
            }
        )

    return mapped


def _apply_observations(graph: Dict[str, Any], observations: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    updated: List[Dict[str, Any]] = []
    for item in observations:
        node = graph["nodes"].get(item["id"])
        if node is None:
            continue
        _validate_node_state(node, item["state"])
        node["observed_state"] = item["state"]
        node["observed_confidence"] = _confidence(float(item["confidence"]))
        node["observed_at"] = _utc_now()
        state, confidence, _ = _node_effective(node)
        updated.append(
            {
                "id": item["id"],
                "state": item["state"],
                "confidence": node["observed_confidence"],
                "freshness": item.get("freshness", "live"),
                "effective_state": state,
                "effective_confidence": confidence,
                "source": item.get("source"),
            }
        )
    return updated


def cmd_scan(args: argparse.Namespace) -> None:
    graph = _load_graph()
    requested = [part.strip() for part in args.modules.split(",")] if args.modules else ["macro", "china", "rhino"]
    observations: List[Dict[str, Any]] = []
    errors: List[str] = []
    module_payloads: Dict[str, Any] = {}

    with _suppress_stderr():
        for module_name in requested:
            if module_name == "macro":
                try:
                    result = _load_macro_result(args.lookback_days)
                    module_payloads["macro"] = result
                    observations.extend(_map_macro(result))
                except Exception as exc:
                    errors.append(f"macro: {exc}")
            elif module_name == "china":
                try:
                    result = _load_china_result(min(args.lookback_days, 60))
                    module_payloads["china"] = result
                    observations.extend(_map_china(result))
                except Exception as exc:
                    errors.append(f"china: {exc}")
            elif module_name == "rhino":
                try:
                    result = _load_rhino_result(args.max_age, args.top, args.rhino_lookback)
                    module_payloads["rhino"] = result
                    observations.extend(_map_rhino(result))
                except Exception as exc:
                    errors.append(f"rhino: {exc}")
            else:
                errors.append(f"unknown module: {module_name}")

    updated = _apply_observations(graph, observations)
    _save_graph(graph)
    _save_json(
        _signals_path(),
        {
            "timestamp": _utc_now(),
            "modules_run": requested,
            "nodes_updated": updated,
            "errors": errors,
            "payloads": module_payloads,
        },
    )
    _touch_session("scan")
    _json_ok(
        {
            "timestamp": _utc_now(),
            "modules_run": requested,
            "nodes_updated": updated,
            "errors": errors,
        }
    )


def cmd_node(args: argparse.Namespace) -> None:
    graph = _load_graph()
    if args.check:
        nodes = []
        for node in graph["nodes"].values():
            nodes.append({"id": node["id"], "label": node["label"], "type": node["type"], "origin": node["origin"]})
        _json_ok({"nodes": nodes})
        return

    if not args.id:
        _json_error("--id is required unless --check is used")
        return

    node = graph["nodes"].get(args.id)

    if node is None:
        if not (args.type and args.states and args.label):
            _json_error("Creating a node requires --type, --states, and --label")
            return
        node = {
            "id": args.id,
            "label": args.label,
            "type": args.type,
            "origin": "dynamic",
            "states": [part.strip() for part in args.states.split(",") if part.strip()],
        }
        graph["nodes"][args.id] = node
        _save_graph(graph)
        _touch_session("node")

    if args.state:
        try:
            _validate_node_state(node, args.state)
        except ValueError as exc:
            _json_error(str(exc))
            return
        node["analyst_state"] = args.state
        node["analyst_confidence"] = _confidence(args.confidence if args.confidence is not None else 0.7)
        node["analyst_reason"] = args.reason or ""
        node["analyst_updated_at"] = _utc_now()
        _save_graph(graph)
        _touch_session("node")

    state, confidence, source = _node_effective(node)
    _json_ok(
        {
            "id": node["id"],
            "label": node["label"],
            "type": node["type"],
            "origin": node["origin"],
            "states": node["states"],
            "observed_state": node.get("observed_state"),
            "observed_confidence": node.get("observed_confidence"),
            "analyst_state": node.get("analyst_state"),
            "analyst_confidence": node.get("analyst_confidence"),
            "analyst_reason": node.get("analyst_reason"),
            "effective_state": state,
            "effective_confidence": confidence,
            "effective_source": source,
        }
    )


def cmd_edge(args: argparse.Namespace) -> None:
    graph = _load_graph()
    if not args.from_id or not args.to:
        _json_error("--from and --to are required")
        return

    if args.from_id not in graph["nodes"] or args.to not in graph["nodes"]:
        _json_error("Both --from and --to must reference existing nodes")
        return

    key = f"{args.from_id}->{args.to}"
    edge = graph["edges"].get(key)

    if args.prob:
        try:
            raw_probabilities = _parse_probabilities(args.prob)
            probabilities = _validate_probabilities(graph, args.from_id, args.to, raw_probabilities)
        except ValueError as exc:
            _json_error(str(exc))
            return
        if edge is not None:
            edge["probabilities"].update(probabilities)
            edge["method"] = "agent_prior"
            edge["reason"] = args.reason or edge.get("reason", "")
            edge["updated_at"] = _utc_now()
        else:
            edge = {
                "from": args.from_id,
                "to": args.to,
                "probabilities": probabilities,
                "method": "agent_prior",
                "reason": args.reason or "",
                "updated_at": _utc_now(),
            }
            graph["edges"][key] = edge
        _save_graph(graph)
        _touch_session("edge")
    elif edge is None:
        _json_error("Edge not found")
        return

    _json_ok(edge)


def _preview_chain(graph: Dict[str, Any], path: List[str]) -> Dict[str, Any]:
    detail: List[Dict[str, Any]] = []
    first_node = graph["nodes"][path[0]]
    current_state, current_confidence, source = _node_effective(first_node)
    if current_state is None or current_confidence is None:
        raise ValueError(f"Node '{path[0]}' has no effective state")

    probability = float(current_confidence)
    detail.append({"node": path[0], "state": current_state, "source": source, "p": round(float(current_confidence), 4)})

    for src, dst in zip(path, path[1:]):
        edge = _find_edge(graph, src, dst)
        if edge is None:
            raise ValueError(f"Missing edge {src}->{dst}")

        dst_node = graph["nodes"][dst]
        dst_state, _, _ = _node_effective(dst_node)
        selected_prob = None
        selected_state = dst_state

        if dst_state is not None:
            selected_prob = edge["probabilities"].get(f"{current_state}->{dst_state}")

        if selected_prob is None:
            candidates = []
            for key, prob in edge["probabilities"].items():
                left, right = key.split("->", 1)
                if left == current_state:
                    candidates.append((right, float(prob)))
            if not candidates:
                raise ValueError(f"No probability mapping for {src} state '{current_state}'")
            selected_state, selected_prob = max(candidates, key=lambda item: item[1])

        probability *= float(selected_prob)
        detail.append({"node": dst, "state": selected_state, "p": round(float(selected_prob), 4)})
        current_state = selected_state

    terminal = graph["nodes"][path[-1]]
    impact = terminal.get("impact_weight", 1.0)
    return {
        "path_detail": detail,
        "probability": round(probability, 4),
        "impact_weight": impact,
        "impact_score": round(probability * impact, 4),
        "terminal_node": path[-1],
        "terminal_state": current_state,
        "root_node": path[0],
    }


def cmd_chain(args: argparse.Namespace) -> None:
    graph = _load_graph()
    if args.list:
        _json_ok({"chains": graph["chains"]})
        return

    if args.remove is not None:
        before = len(graph["chains"])
        graph["chains"] = [chain for chain in graph["chains"] if chain["id"] != args.remove]
        if len(graph["chains"]) == before:
            _json_error(f"Chain id {args.remove} not found")
            return
        _save_graph(graph)
        _touch_session("chain_remove")
        _json_ok({"removed": args.remove})
        return

    if not args.path:
        _json_error("--path is required")
        return

    path = [part.strip() for part in args.path.split("->") if part.strip()]
    try:
        _validate_chain_path(graph, path)
    except ValueError as exc:
        _json_error(str(exc))
        return

    if args.preview:
        try:
            preview = _preview_chain(graph, path)
        except Exception as exc:
            _json_error(str(exc))
            return
        _json_ok(preview)
        return

    chain = {
        "id": _next_chain_id(graph),
        "path": path,
        "label": args.label or " -> ".join(path),
        "reasoning": args.reasoning or "",
        "updated_at": _utc_now(),
    }
    graph["chains"].append(chain)
    _save_graph(graph)
    _touch_session("chain")
    _json_ok(chain)


def _infer(
    graph: Dict[str, Any],
    top_n: int,
    targets: Optional[List[str]] = None,
    *,
    max_hops: int = 6,
    min_hops: int = 1,
) -> Dict[str, Any]:
    observed_nodes = sum(1 for n in graph["nodes"].values() if _node_effective(n)[0] is not None)
    min_hops = max(1, min_hops)

    chain_candidates = list(graph["chains"])
    seen_paths = {tuple(chain["path"]) for chain in chain_candidates}
    for chain in _discover_chains(graph, max_hops=max_hops, targets=targets):
        if tuple(chain["path"]) in seen_paths:
            continue
        chain_candidates.append(chain)
        seen_paths.add(tuple(chain["path"]))

    raw_chains: List[Tuple[Dict[str, Any], Dict[str, Any]]] = []
    skipped_chains: List[Dict[str, Any]] = []
    for chain in chain_candidates:
        hop_count = _path_hops(chain["path"])
        if hop_count < min_hops:
            continue
        try:
            preview = _preview_chain(graph, chain["path"])
        except Exception as exc:
            skipped_chains.append(
                {
                    "id": chain["id"],
                    "label": chain["label"],
                    "path": chain["path"],
                    "error": str(exc),
                    "auto_discovered": bool(chain.get("auto_discovered")),
                }
            )
            continue
        target_node = preview["terminal_node"]
        if targets and target_node not in targets:
            continue
        raw_chains.append((chain, preview))

    # Dedup chains that share the same root and first branch into the same target state.
    groups: Dict[Tuple[str, str, str], List[Tuple[Dict[str, Any], Dict[str, Any]]]] = {}
    for chain, preview in raw_chains:
        key = (
            preview["terminal_node"],
            preview["terminal_state"],
            _chain_branch_key(chain["path"]),
        )
        groups.setdefault(key, []).append((chain, preview))

    deduped: List[Tuple[Dict[str, Any], Dict[str, Any]]] = []
    for group in groups.values():
        best = max(group, key=lambda x: x[1]["probability"])
        deduped.append(best)

    # Build top_chains and grouped probabilities for noisy-or
    top_chains: List[Dict[str, Any]] = []
    grouped: Dict[str, Dict[str, List[float]]] = {}
    for chain, preview in deduped:
        target_node = preview["terminal_node"]
        target_state = preview["terminal_state"]
        grouped.setdefault(target_node, {}).setdefault(target_state, []).append(preview["probability"])
        top_chains.append(
            {
                "id": chain["id"],
                "label": chain["label"],
                "probability": preview["probability"],
                "impact": "high" if preview["impact_weight"] >= 0.9 else "medium",
                "chain_score": preview["impact_score"],
                "path_detail": preview["path_detail"],
                "target": target_node,
                "target_state": target_state,
                "root_node": preview["root_node"],
                "path_length": len(chain["path"]),
                "hop_count": _path_hops(chain["path"]),
                "auto_discovered": bool(chain.get("auto_discovered")),
            }
        )

    top_chains.sort(key=lambda item: item["chain_score"], reverse=True)
    top_chains = top_chains[:top_n]

    # Noisy-or + normalize for asset summary
    asset_summary: Dict[str, Dict[str, float]] = {}
    for target_node, state_map in grouped.items():
        node = graph["nodes"].get(target_node, {})
        if node.get("type") != "asset":
            continue
        distribution: Dict[str, float] = {}
        total = 0.0
        for state, probs in state_map.items():
            merged = 1.0
            for value in probs:
                merged *= (1.0 - float(value))
            score = 1.0 - merged
            distribution[state] = score
            total += score

        if total <= 0:
            normalized = {state: round(1.0 / len(node["states"]), 4) for state in node["states"]}
        else:
            normalized = {state: round(distribution.get(state, 0.0) / total, 4) for state in node["states"]}
        asset_summary[target_node] = normalized

    return {
        "timestamp": _utc_now(),
        "observed_nodes": observed_nodes,
        "min_hops": min_hops,
        "top_chains": top_chains,
        "asset_summary": asset_summary,
        "skipped_chains": skipped_chains,
    }


def cmd_infer(args: argparse.Namespace) -> None:
    graph = _load_graph()
    targets = [part.strip() for part in args.target.split(",")] if args.target else None
    result = _infer(graph, args.top, targets, max_hops=args.max_hops, min_hops=args.min_hops)
    snapshot = {
        "graph_updated_at": graph["updated_at"],
        **result,
    }
    _save_json(_inference_path(), snapshot)
    _touch_session("infer")
    _json_ok(result)


def _format_report(graph: Dict[str, Any], inference: Dict[str, Any]) -> str:
    lines = []
    lines.append("Current Investment Environment")
    lines.append("")

    top_chains = inference.get("top_chains", [])
    multi_hop_chains = [chain for chain in top_chains if int(chain.get("hop_count", 0)) >= 2]
    direct_chains = [chain for chain in top_chains if int(chain.get("hop_count", 0)) < 2]

    min_hops = inference.get("min_hops")
    if isinstance(min_hops, int) and min_hops > 1:
        lines.append(f"Chain filter: minimum hops = {min_hops}")
        lines.append("")

    if top_chains:
        if multi_hop_chains:
            lines.append("Multi-hop causal chains:")
            for chain in multi_hop_chains[:5]:
                path = " -> ".join(step["node"] for step in chain["path_detail"])
                pct = round(chain["probability"] * 100, 1)
                lines.append(f"- {chain['label']}: {path} ({pct}%, impact {chain['impact']})")
            lines.append("")

        if direct_chains:
            lines.append("Direct causal chains:")
            for chain in direct_chains[:5]:
                path = " -> ".join(step["node"] for step in chain["path_detail"])
                pct = round(chain["probability"] * 100, 1)
                lines.append(f"- {chain['label']}: {path} ({pct}%, impact {chain['impact']})")
            lines.append("")
    else:
        lines.append("No causal chains passed the current filter.")
        lines.append("")

    summary = inference.get("asset_summary", {})
    if summary:
        lines.append("Asset summary:")
        for asset, distribution in summary.items():
            best_state = max(distribution.items(), key=lambda item: item[1])
            pct = round(best_state[1] * 100, 1)
            lines.append(f"- {asset}: {best_state[0]} ({pct}%)")
        lines.append("")

    uncertainty = []
    for node in graph["nodes"].values():
        if node.get("observed_state") and not node.get("analyst_state"):
            conf = node.get("observed_confidence")
            if isinstance(conf, (int, float)) and conf < 0.65:
                uncertainty.append(f"{node['id']} observed confidence {conf:.2f}")
    if uncertainty:
        lines.append("Key uncertainty:")
        for item in uncertainty[:5]:
            lines.append(f"- {item}")
        lines.append("")

    lines.append("Disclaimer: analysis support only, not investment advice.")
    return "\n".join(lines)


def cmd_report(args: argparse.Namespace) -> None:
    graph = _load_graph()
    inference = _load_json(_inference_path(), {})
    if not inference:
        _json_error("No inference snapshot found, run infer first")
        return
    if inference.get("graph_updated_at") != graph.get("updated_at"):
        _json_error("Graph changed after last inference, run infer again")
        return

    report = {
        "generated_at": _utc_now(),
        "top_chains": inference.get("top_chains", []),
        "asset_summary": inference.get("asset_summary", {}),
        "text": _format_report(graph, inference),
    }

    if args.format == "text":
        print(report["text"])
    else:
        _json_ok(report)


def cmd_status(_: argparse.Namespace) -> None:
    graph = _load_graph()
    inference = _load_json(_inference_path(), {})
    observed = 0
    analyst = 0
    missing = []
    chain_issues = []
    for node in graph["nodes"].values():
        if node.get("observed_state"):
            observed += 1
        if node.get("analyst_state"):
            analyst += 1
        state, _, _ = _node_effective(node)
        if state is None:
            missing.append(node["id"])

    for chain in graph["chains"]:
        try:
            _validate_chain_path(graph, chain["path"])
        except ValueError as exc:
            chain_issues.append({"id": chain["id"], "label": chain["label"], "error": str(exc)})

    _json_ok(
        {
            "graph_updated_at": graph["updated_at"],
            "node_count": len(graph["nodes"]),
            "edge_count": len(graph["edges"]),
            "chain_count": len(graph["chains"]),
            "observed_nodes": observed,
            "analyst_nodes": analyst,
            "missing_nodes": missing,
            "invalid_chains": chain_issues,
            "has_inference": bool(inference),
        }
    )


def cmd_show(args: argparse.Namespace) -> None:
    graph = _load_graph()
    if args.section == "graph":
        _json_ok(graph)
        return
    if args.section == "signals":
        _json_ok(_load_json(_signals_path(), {}))
        return
    if args.section == "inference":
        _json_ok(_load_json(_inference_path(), {}))
        return
    _json_error("Unknown section")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Invest skill tools")
    sub = parser.add_subparsers(dest="command", required=True)

    scan = sub.add_parser("scan", help="Collect signals")
    scan.add_argument("--modules", default="macro,china,rhino")
    scan.add_argument("--lookback-days", type=int, default=365)
    scan.add_argument("--max-age", type=int, default=48)
    scan.add_argument("--top", type=int, default=5)
    scan.add_argument("--rhino-lookback", type=int, default=7)
    scan.set_defaults(func=cmd_scan)

    node = sub.add_parser("node", help="Manage nodes")
    node.add_argument("--id")
    node.add_argument("--check", action="store_true")
    node.add_argument("--state")
    node.add_argument("--confidence", type=float)
    node.add_argument("--reason")
    node.add_argument("--type")
    node.add_argument("--states")
    node.add_argument("--label")
    node.set_defaults(func=cmd_node)

    edge = sub.add_parser("edge", help="Manage edges")
    edge.add_argument("--from", dest="from_id")
    edge.add_argument("--to")
    edge.add_argument("--prob")
    edge.add_argument("--reason")
    edge.set_defaults(func=cmd_edge)

    chain = sub.add_parser("chain", help="Manage chains")
    chain.add_argument("--path")
    chain.add_argument("--label")
    chain.add_argument("--reasoning")
    chain.add_argument("--preview", action="store_true")
    chain.add_argument("--list", action="store_true")
    chain.add_argument("--remove", type=int)
    chain.set_defaults(func=cmd_chain)

    infer = sub.add_parser("infer", help="Run forward inference")
    infer.add_argument("--top", type=int, default=5)
    infer.add_argument("--target")
    infer.add_argument("--max-hops", type=int, default=6)
    infer.add_argument("--min-hops", type=int, default=1)
    infer.set_defaults(func=cmd_infer)

    report = sub.add_parser("report", help="Format report")
    report.add_argument("--format", choices=["text", "json"], default="text")
    report.set_defaults(func=cmd_report)

    status = sub.add_parser("status", help="Show session status")
    status.set_defaults(func=cmd_status)

    show = sub.add_parser("show", help="Show stored data")
    show.add_argument("section", choices=["graph", "signals", "inference"])
    show.set_defaults(func=cmd_show)

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    try:
        _ensure_store()
        args.func(args)
        return 0
    except Exception as exc:
        _json_error(str(exc))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

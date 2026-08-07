from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest

from eve_courier_optimizer.domain import SecurityBand, SecurityPolicy, ThreatCategory
from eve_courier_optimizer.sde import UniverseGraph, build_jump_matrix, load_bundled_graph


def test_shortest_paths_respect_security_policy(tiny_graph: UniverseGraph) -> None:
    highsec = SecurityPolicy(minimum_security=0.45)
    assert tiny_graph.shortest_path(1, 3, highsec) == (1, 2, 3)
    assert tiny_graph.shortest_path(1, 4, highsec) is None
    assert tiny_graph.distances_from(1, {1, 2, 3, 4}, highsec) == {1: 0, 2: 1, 3: 2}


def test_avoided_system_disconnects_route(tiny_graph: UniverseGraph) -> None:
    policy = SecurityPolicy(minimum_security=None, avoided_system_ids=frozenset({2}))
    assert tiny_graph.shortest_path(1, 3, policy) is None
    assert build_jump_matrix(tiny_graph, [1, 3], policy) == {(1, 1): 0, (3, 3): 0}


def test_gate_threat_avoid_blocks_an_intermediate_transit_system(
    tiny_graph: UniverseGraph,
    now: datetime,
) -> None:
    policy = SecurityPolicy(
        minimum_security=None,
        threat_avoided_system_ids=frozenset({2}),
        threat_categories=frozenset({ThreatCategory.ANY_GATE_PVP}),
        threat_min_events=1,
        threat_intel_fetched_at=now,
        threat_window_seconds=7_200,
        threat_gate_radius_m=250_000,
    )
    # Systems 1 and 3 are themselves safe; system 2 is their only connecting gate transit.
    assert tiny_graph.shortest_path(1, 3, policy) is None
    assert tiny_graph.distances_from(1, {3}, policy) == {}


def test_reachable_system_ball_respects_jump_budget_and_security(
    tiny_graph: UniverseGraph,
) -> None:
    highsec = SecurityPolicy(minimum_security=0.45)
    assert tiny_graph.reachable_system_ids(1, highsec, max_jumps=0) == frozenset({1})
    assert tiny_graph.reachable_system_ids(1, highsec, max_jumps=1) == frozenset({1, 2})
    assert tiny_graph.reachable_system_ids(1, highsec, max_jumps=2) == frozenset({1, 2, 3})

    unrestricted = SecurityPolicy(minimum_security=None)
    assert tiny_graph.reachable_system_ids(1, unrestricted, max_jumps=2) == frozenset(
        {1, 2, 3, 4}
    )
    with pytest.raises(ValueError, match="max_jumps cannot be negative"):
        tiny_graph.reachable_system_ids(1, highsec, max_jumps=-1)


def test_region_security_scopes_are_conservative_for_mixed_regions(
    tiny_graph: UniverseGraph,
) -> None:
    assert tiny_graph.region_ids_for_security_bands({SecurityBand.HIGH}) == frozenset({10})
    assert tiny_graph.region_ids_for_security_bands({SecurityBand.LOW}) == frozenset({10})
    assert tiny_graph.region_ids_for_security_bands({SecurityBand.NULL}) == frozenset()
    with pytest.raises(ValueError, match="at least one security band"):
        tiny_graph.region_ids_for_security_bands(set())


def test_jump_matrix_cache_returns_a_defensive_copy(tiny_graph: UniverseGraph) -> None:
    policy = SecurityPolicy(minimum_security=0.45)
    first = build_jump_matrix(tiny_graph, [1, 2, 3], policy)
    first[(1, 3)] = 999
    second = build_jump_matrix(tiny_graph, [3, 2, 1], policy)
    assert second[(1, 3)] == 2


def test_bundled_sde_is_current_and_contains_jita() -> None:
    graph = load_bundled_graph()
    assert graph.metadata.build_number == 3_458_726
    assert graph.systems[30_000_142].name == "Jita"
    assert graph.station_system(60_003_760) == 30_000_142
    assert len(graph.systems) > 8_000
    assert len(graph.gates) == 13_978
    assert graph.type_groups[72].name == "Smart Bomb"
    assert len(graph.type_group_by_type_id) > 50_000
    assert graph.regions[10000002].faction_id == 500001
    assert 10000002 in graph.empire_region_ids()  # The Forge
    assert 10000003 not in graph.empire_region_ids()  # player-sovereign nullsec
    assert 19000001 not in graph.empire_region_ids()  # special highsec without SDE faction


def test_missing_sde_database_is_rejected(tmp_path: Path) -> None:
    missing = tmp_path / "missing.sqlite3"
    try:
        UniverseGraph.from_sqlite(missing)
    except FileNotFoundError as error:
        assert "not found" in str(error)
    else:  # pragma: no cover - defensive assertion
        raise AssertionError("expected FileNotFoundError")

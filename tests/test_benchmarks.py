from __future__ import annotations

from benchmarks.run_empire import FIXTURE as EMPIRE_FIXTURE
from benchmarks.run_frozen import run_benchmark_scenarios
from eve_courier_optimizer.domain import ProofStatus
from eve_courier_optimizer.sde import load_bundled_graph
from eve_courier_optimizer.snapshot import read_snapshot


def test_frozen_dst_and_blockade_runner_targets_prove_global_optimality() -> None:
    results = run_benchmark_scenarios(time_limit_seconds=10.0)
    assert [result.name for result in results] == [
        "dst_highsec_10b_1h_gank_aware",
        "br_high_low_5b_1h_gank_aware",
    ]
    for result in results:
        assert result.status is ProofStatus.PROVEN_OPTIMAL
        assert result.scope_untruncated
        assert result.feasibility_verified
        assert result.eligible_contracts > 0
        assert result.selected_contracts > 0


def test_realistic_empire_fixture_has_complete_declared_scope() -> None:
    graph = load_bundled_graph()
    snapshot = read_snapshot(EMPIRE_FIXTURE)

    empire_regions = graph.empire_region_ids()
    assert snapshot.sde_build_number == graph.metadata.build_number == 3_458_726
    assert set(snapshot.region_ids) == empire_regions
    assert set(snapshot.threat_coverage_region_ids) == empire_regions
    assert snapshot.threat_incomplete_region_ids == ()
    assert len(snapshot.region_ids) == 24
    assert len(snapshot.contracts) == 421
    assert len(snapshot.gate_threat_events) == 250

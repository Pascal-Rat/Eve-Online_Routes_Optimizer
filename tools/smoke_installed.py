"""Exercise an installed wheel outside the checkout, without external services or test fixtures."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import eve_courier_optimizer
from eve_courier_optimizer.domain import ContractSnapshot, PublicCourierContract
from eve_courier_optimizer.eve.esi import EsiClient
from eve_courier_optimizer.routing.universe import (
    Region,
    SdeMetadata,
    SolarSystem,
    UniverseGraph,
    load_bundled_graph,
)
from eve_courier_optimizer.web.background_jobs import BackgroundJobs
from eve_courier_optimizer.web.operations import ScanObservation
from eve_courier_optimizer.web.server import asset
from eve_courier_optimizer.web.workspace import PlanningWorkspace


def smoke(version: str) -> None:
    package_path = Path(eve_courier_optimizer.__file__).resolve()
    if package_path.is_relative_to(Path(__file__).resolve().parents[1] / "src"):
        raise RuntimeError("smoke test imported checkout sources instead of the installed wheel")
    assert eve_courier_optimizer.__version__ == version
    graph = load_bundled_graph()
    assert graph.systems[30_000_142].name == "Jita"
    for name in (
        "index.html",
        "styles.css",
        "app.js",
        "api.js",
        "contract_schemas.js",
        "contract_validation.js",
        "display.js",
        "autocomplete.js",
        "planner_form.js",
        "route_view.js",
    ):
        assert asset(name)[0], name
    tiny = UniverseGraph(
        systems={1: SolarSystem(1, 10, "Alpha", 1), 2: SolarSystem(2, 10, "Beta", 1)},
        adjacency={1: (2,), 2: (1,)},
        station_systems={101: 1, 102: 2},
        regions={10: Region(10, "Test")},
        metadata=SdeMetadata(1, "2026-09-08", "test://smoke"),
    )
    now = datetime.now(UTC)
    snapshot = ContractSnapshot(
        now,
        "2026-08-05",
        1,
        (10,),
        (PublicCourierContract(1, 101, 102, 10, 100, 500, now + timedelta(days=1), 1),),
    )
    with tempfile.TemporaryDirectory(prefix="eve-installed-smoke-") as directory:
        root = Path(directory)
        for command in ("--help", "sde-info"):
            subprocess.run(
                [sys.executable, "-m", "eve_courier_optimizer", command],
                cwd=root,
                check=True,
                timeout=30,
                capture_output=True,
            )
        app = PlanningWorkspace(tiny, EsiClient(), root)
        app.publish(ScanObservation(snapshot), expected_revision=app.revision)
        jobs = BackgroundJobs(app)
        try:
            jobs.start(
                "solve",
                {
                    "start": "Alpha",
                    "cargo_m3": "1",
                    "collateral_isk": "2",
                    "hours": "1",
                    "time_limit": "5",
                    "workers": "1",
                },
            )
            deadline = time.monotonic() + 30
            while jobs.running and time.monotonic() < deadline:
                time.sleep(0.05)
            job = jobs.status()
            assert job is not None and job["status"] == "completed", job
            assert app.plan is not None and app.plan.result.certificate.feasibility_verified
            app.start_execution(
                {
                    "proposal_id": app.proposal_id,
                    "expected_revision": app.revision,
                    "confirm_locked_acceptance": True,
                }
            )
            app.record_action({"action": "pickup", "contract_id": 1})
            restored = PlanningWorkspace(tiny, EsiClient(), root)
            assert restored.trip == app.trip
            assert restored.trip is not None and restored.trip.active_shipments[0].picked
            restored.record_action({"action": "delivery", "contract_id": 1})
            assert restored.trip is not None and restored.trip.completed_contract_ids == (1,)
        finally:
            jobs.close()
    print(
        json.dumps(
            {
                "version": version,
                "package": str(package_path),
                "platform": sys.platform,
                "sde_build": graph.metadata.build_number,
                "spawn_solve_and_recovery": "passed",
            }
        )
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    args = parser.parse_args()
    smoke(json.loads(args.manifest.read_text())["version"])

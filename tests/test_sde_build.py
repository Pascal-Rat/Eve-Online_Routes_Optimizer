from __future__ import annotations

import json
from pathlib import Path
from zipfile import ZipFile

from eve_courier_optimizer.domain import SecurityPolicy
from eve_courier_optimizer.sde import UniverseGraph
from eve_courier_optimizer.sde_build import LatestBuild, build_route_database


def _write_jsonl(archive: ZipFile, name: str, rows: list[dict[str, object]]) -> None:
    archive.writestr(name, "".join(json.dumps(row) + "\n" for row in rows))


def test_build_route_database_from_jsonl_zip(tmp_path: Path) -> None:
    source = tmp_path / "sde.zip"
    with ZipFile(source, "w") as archive:
        _write_jsonl(
            archive,
            "mapRegions.jsonl",
            [{"_key": 10, "name": {"en": "R"}, "factionID": 500001}],
        )
        _write_jsonl(
            archive,
            "mapSolarSystems.jsonl",
            [
                {
                    "_key": 1,
                    "regionID": 10,
                    "name": {"en": "A"},
                    "securityStatus": 1.0,
                },
                {
                    "_key": 2,
                    "regionID": 10,
                    "name": {"en": "B"},
                    "securityStatus": 0.9,
                },
            ],
        )
        _write_jsonl(
            archive,
            "npcStations.jsonl",
            [{"_key": 101, "solarSystemID": 1}],
        )
        _write_jsonl(
            archive,
            "mapStargates.jsonl",
            [
                {
                    "_key": 501,
                    "solarSystemID": 1,
                    "position": {"x": 1.0, "y": 2.0, "z": 3.0},
                    "destination": {"solarSystemID": 2},
                }
            ],
        )
        _write_jsonl(
            archive,
            "groups.jsonl",
            [{"_key": 72, "categoryID": 7, "name": {"en": "Smart Bomb"}}],
        )
        _write_jsonl(
            archive,
            "types.jsonl",
            [{"_key": 2001, "groupID": 72, "name": {"en": "Test Smartbomb"}}],
        )
    output = tmp_path / "route.sqlite3"
    stats = build_route_database(
        source,
        output,
        build=LatestBuild(123, "2026-08-05T11:00:00Z"),
    )
    graph = UniverseGraph.from_sqlite(output)
    assert stats.systems == 2
    assert stats.jumps == 2
    assert stats.gates == 1
    assert stats.item_types == 1
    assert stats.type_groups == 1
    assert graph.metadata.build_number == 123
    assert graph.regions[10].faction_id == 500001
    assert graph.shortest_path(1, 2, SecurityPolicy(0.45)) == (1, 2)
    assert graph.gates[501].system_id == 1
    assert graph.item_group(2001) is not None

"""Reproducibly distill CCP's JSONL SDE into the route database used by the planner."""

from __future__ import annotations

import json
import shutil
import sqlite3
import tempfile
from collections.abc import Iterator, Mapping
from contextlib import closing
from dataclasses import dataclass
from io import TextIOWrapper
from pathlib import Path
from typing import Any, Final, cast
from urllib.request import Request, urlopen
from zipfile import ZipFile

LATEST_BUILD_URL: Final = "https://developers.eveonline.com/static-data/tranquility/latest.jsonl"
SDE_URL_TEMPLATE: Final = (
    "https://developers.eveonline.com/static-data/tranquility/"
    "eve-online-static-data-{build_number}-jsonl.zip"
)
USER_AGENT: Final = "eve-courier-route-optimizer/1.5.0 (+local EVE route planning)"


@dataclass(frozen=True, slots=True)
class LatestBuild:
    build_number: int
    release_date: str


@dataclass(frozen=True, slots=True)
class BuildStats:
    systems: int
    jumps: int
    stations: int
    regions: int
    gates: int = 0
    item_types: int = 0
    type_groups: int = 0


def _get_bytes(url: str, timeout_seconds: float = 30.0) -> bytes:
    request = Request(url, headers={"User-Agent": USER_AGENT})
    with closing(urlopen(request, timeout=timeout_seconds)) as response:  # noqa: S310
        return bytes(response.read())


def fetch_latest_build() -> LatestBuild:
    payload = json.loads(_get_bytes(LATEST_BUILD_URL), parse_float=str)
    if not isinstance(payload, Mapping) or payload.get("_key") != "sde":
        raise ValueError("unexpected CCP latest-build payload")
    return LatestBuild(
        build_number=int(payload["buildNumber"]),
        release_date=str(payload["releaseDate"]),
    )


def download_sde(build: LatestBuild, destination: Path) -> Path:
    """Download one immutable SDE build to ``destination`` using an atomic rename."""

    destination.parent.mkdir(parents=True, exist_ok=True)
    url = SDE_URL_TEMPLATE.format(build_number=build.build_number)
    request = Request(url, headers={"User-Agent": USER_AGENT})
    with tempfile.NamedTemporaryFile(
        dir=destination.parent,
        prefix=f".{destination.name}.",
        suffix=".tmp",
        delete=False,
    ) as temporary:
        temp_path = Path(temporary.name)
        try:
            with closing(urlopen(request, timeout=60.0)) as response:  # noqa: S310
                shutil.copyfileobj(response, temporary, length=1024 * 1024)
        except BaseException:
            temp_path.unlink(missing_ok=True)
            raise
    temp_path.replace(destination)
    return destination


def _jsonl(zip_file: ZipFile, member: str) -> Iterator[dict[str, Any]]:
    with zip_file.open(member) as raw, TextIOWrapper(raw, encoding="utf-8") as text:
        for line_number, line in enumerate(text, start=1):
            try:
                value = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"invalid JSON in {member}:{line_number}") from exc
            if not isinstance(value, dict):
                raise ValueError(f"expected object in {member}:{line_number}")
            yield cast(dict[str, Any], value)


def _english_name(record: Mapping[str, Any]) -> str:
    name = record.get("name")
    if isinstance(name, Mapping) and isinstance(name.get("en"), str):
        return str(name["en"])
    return ""


def _create_schema(connection: sqlite3.Connection) -> None:
    connection.executescript(
        """
        PRAGMA journal_mode = DELETE;
        PRAGMA synchronous = FULL;
        CREATE TABLE metadata (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
        CREATE TABLE regions (
            region_id INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            faction_id INTEGER
        );
        CREATE TABLE systems (
            system_id INTEGER PRIMARY KEY,
            region_id INTEGER NOT NULL,
            name TEXT NOT NULL,
            security_status REAL NOT NULL
        );
        CREATE INDEX systems_region_idx ON systems(region_id);
        CREATE TABLE stations (
            station_id INTEGER PRIMARY KEY,
            system_id INTEGER NOT NULL
        );
        CREATE INDEX stations_system_idx ON stations(system_id);
        CREATE TABLE jumps (
            from_system_id INTEGER NOT NULL,
            to_system_id INTEGER NOT NULL,
            PRIMARY KEY (from_system_id, to_system_id)
        ) WITHOUT ROWID;
        CREATE INDEX jumps_destination_idx ON jumps(to_system_id);
        CREATE TABLE gates (
            gate_id INTEGER PRIMARY KEY,
            system_id INTEGER NOT NULL,
            x_m REAL NOT NULL,
            y_m REAL NOT NULL,
            z_m REAL NOT NULL
        );
        CREATE INDEX gates_system_idx ON gates(system_id);
        CREATE TABLE type_groups (
            group_id INTEGER PRIMARY KEY,
            category_id INTEGER NOT NULL,
            name TEXT NOT NULL
        );
        CREATE TABLE item_types (
            type_id INTEGER PRIMARY KEY,
            group_id INTEGER NOT NULL
        );
        CREATE INDEX item_types_group_idx ON item_types(group_id);
        """
    )


def build_route_database(
    sde_zip: Path,
    output: Path,
    *,
    build: LatestBuild,
) -> BuildStats:
    """Create a minimal, deterministic SQLite routing database from the official SDE."""

    output.parent.mkdir(parents=True, exist_ok=True)
    source_url = SDE_URL_TEMPLATE.format(build_number=build.build_number)
    with tempfile.NamedTemporaryFile(
        dir=output.parent,
        prefix=f".{output.name}.",
        suffix=".tmp",
        delete=False,
    ) as temporary:
        temp_path = Path(temporary.name)
    temp_path.unlink(missing_ok=True)

    try:
        with sqlite3.connect(temp_path) as connection, ZipFile(sde_zip) as archive:
            _create_schema(connection)
            connection.executemany(
                "INSERT INTO metadata(key, value) VALUES (?, ?)",
                [
                    ("build_number", str(build.build_number)),
                    ("release_date", build.release_date),
                    ("source_url", source_url),
                    ("format", "CCP JSON Lines SDE route subset"),
                ],
            )

            region_count = 0
            for record in _jsonl(archive, "mapRegions.jsonl"):
                connection.execute(
                    "INSERT INTO regions(region_id, name, faction_id) VALUES (?, ?, ?)",
                    (
                        int(record["_key"]),
                        _english_name(record),
                        int(record["factionID"]) if record.get("factionID") is not None else None,
                    ),
                )
                region_count += 1

            system_count = 0
            for record in _jsonl(archive, "mapSolarSystems.jsonl"):
                connection.execute(
                    """INSERT INTO systems(system_id, region_id, name, security_status)
                       VALUES (?, ?, ?, ?)""",
                    (
                        int(record["_key"]),
                        int(record["regionID"]),
                        _english_name(record),
                        float(record["securityStatus"]),
                    ),
                )
                system_count += 1

            station_count = 0
            for record in _jsonl(archive, "npcStations.jsonl"):
                connection.execute(
                    "INSERT INTO stations(station_id, system_id) VALUES (?, ?)",
                    (int(record["_key"]), int(record["solarSystemID"])),
                )
                station_count += 1

            jump_pairs: set[tuple[int, int]] = set()
            gate_rows: list[tuple[int, int, float, float, float]] = []
            for record in _jsonl(archive, "mapStargates.jsonl"):
                source = int(record["solarSystemID"])
                position = record.get("position")
                if not isinstance(position, Mapping):
                    raise ValueError("stargate position must be an object")
                gate_rows.append(
                    (
                        int(record["_key"]),
                        source,
                        float(position["x"]),
                        float(position["y"]),
                        float(position["z"]),
                    )
                )
                destination_raw = record["destination"]
                if not isinstance(destination_raw, Mapping):
                    raise ValueError("stargate destination must be an object")
                destination = int(destination_raw["solarSystemID"])
                # CCP documents normal stargate connections as bidirectional. Inserting both
                # directions also makes the route DB robust to one-sided source records.
                jump_pairs.add((source, destination))
                jump_pairs.add((destination, source))
            connection.executemany(
                "INSERT OR IGNORE INTO jumps(from_system_id, to_system_id) VALUES (?, ?)",
                sorted(jump_pairs),
            )
            connection.executemany(
                "INSERT INTO gates(gate_id, system_id, x_m, y_m, z_m) VALUES (?, ?, ?, ?, ?)",
                sorted(gate_rows),
            )

            group_count = 0
            for record in _jsonl(archive, "groups.jsonl"):
                connection.execute(
                    "INSERT INTO type_groups(group_id, category_id, name) VALUES (?, ?, ?)",
                    (int(record["_key"]), int(record["categoryID"]), _english_name(record)),
                )
                group_count += 1

            type_count = 0
            for record in _jsonl(archive, "types.jsonl"):
                connection.execute(
                    "INSERT INTO item_types(type_id, group_id) VALUES (?, ?)",
                    (int(record["_key"]), int(record["groupID"])),
                )
                type_count += 1
            connection.commit()
            integrity = connection.execute("PRAGMA integrity_check").fetchone()
            if integrity is None or integrity[0] != "ok":
                raise RuntimeError(f"generated SQLite integrity check failed: {integrity}")

        temp_path.replace(output)
    except BaseException:
        temp_path.unlink(missing_ok=True)
        raise

    return BuildStats(
        systems=system_count,
        jumps=len(jump_pairs),
        stations=station_count,
        regions=region_count,
        gates=len(gate_rows),
        item_types=type_count,
        type_groups=group_count,
    )

"""Load the compact route subset of CCP's Static Data Export and find gate paths."""

from __future__ import annotations

import sqlite3
from collections import OrderedDict, deque
from collections.abc import Iterable
from dataclasses import dataclass
from importlib.resources import as_file, files
from pathlib import Path
from typing import Final

from .domain import SecurityBand, SecurityPolicy, security_band

DEFAULT_SDE_RESOURCE: Final = "data/route_sde.sqlite3"


@dataclass(frozen=True, slots=True)
class SolarSystem:
    system_id: int
    region_id: int
    name: str
    security_status: float


@dataclass(frozen=True, slots=True)
class Region:
    region_id: int
    name: str
    faction_id: int | None = None


@dataclass(frozen=True, slots=True)
class Stargate:
    gate_id: int
    system_id: int
    x_m: float
    y_m: float
    z_m: float


@dataclass(frozen=True, slots=True)
class TypeGroup:
    group_id: int
    category_id: int
    name: str


@dataclass(frozen=True, slots=True)
class SdeMetadata:
    build_number: int
    release_date: str
    source_url: str


class UniverseGraph:
    """In-memory static stargate graph optimized for many route queries."""

    def __init__(
        self,
        *,
        systems: dict[int, SolarSystem],
        adjacency: dict[int, tuple[int, ...]],
        station_systems: dict[int, int],
        regions: dict[int, Region],
        metadata: SdeMetadata,
        gates: dict[int, Stargate] | None = None,
        type_groups: dict[int, TypeGroup] | None = None,
        type_group_by_type_id: dict[int, int] | None = None,
    ) -> None:
        self.systems = systems
        self.adjacency = adjacency
        self.station_systems = station_systems
        self.regions = regions
        self.metadata = metadata
        self.gates = gates or {}
        self.type_groups = type_groups or {}
        self.type_group_by_type_id = type_group_by_type_id or {}
        region_bands: dict[int, set[SecurityBand]] = {
            region_id: set() for region_id in self.regions
        }
        for system in self.systems.values():
            region_bands.setdefault(system.region_id, set()).add(
                security_band(system.security_status)
            )
        self.region_security_bands = {
            region_id: frozenset(bands) for region_id, bands in region_bands.items()
        }
        gates_by_system: dict[int, list[Stargate]] = {}
        for gate in self.gates.values():
            gates_by_system.setdefault(gate.system_id, []).append(gate)
        self.gates_by_system = {
            system_id: tuple(sorted(items, key=lambda item: item.gate_id))
            for system_id, items in gates_by_system.items()
        }
        # Rank and solve commonly request the same policy-specific metric closure. Keeping a
        # handful of immutable-input results avoids repeating thousands of BFS traversals without
        # changing the mathematical problem. Values are copied on return so callers cannot poison
        # the cache.
        self._matrix_cache: OrderedDict[
            tuple[SecurityPolicy, tuple[int, ...]], dict[tuple[int, int], int]
        ] = OrderedDict()
        self._path_cache: OrderedDict[
            tuple[int, int, SecurityPolicy], tuple[int, ...] | None
        ] = OrderedDict()

    @classmethod
    def from_sqlite(cls, path: Path) -> UniverseGraph:
        if not path.exists():
            raise FileNotFoundError(f"SDE route database not found: {path}")
        uri = f"file:{path.resolve()}?mode=ro"
        with sqlite3.connect(uri, uri=True) as connection:
            system_rows = connection.execute(
                "SELECT system_id, region_id, name, security_status FROM systems"
            ).fetchall()
            jump_rows = connection.execute(
                "SELECT from_system_id, to_system_id FROM jumps"
            ).fetchall()
            station_rows = connection.execute(
                "SELECT station_id, system_id FROM stations"
            ).fetchall()
            region_columns = {
                str(row[1]) for row in connection.execute("PRAGMA table_info(regions)").fetchall()
            }
            region_rows = connection.execute(
                "SELECT region_id, name, faction_id FROM regions"
                if "faction_id" in region_columns
                else "SELECT region_id, name, NULL FROM regions"
            ).fetchall()
            table_names = {
                str(row[0])
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                ).fetchall()
            }
            gate_rows = (
                connection.execute(
                    "SELECT gate_id, system_id, x_m, y_m, z_m FROM gates"
                ).fetchall()
                if "gates" in table_names
                else []
            )
            group_rows = (
                connection.execute(
                    "SELECT group_id, category_id, name FROM type_groups"
                ).fetchall()
                if "type_groups" in table_names
                else []
            )
            type_rows = (
                connection.execute("SELECT type_id, group_id FROM item_types").fetchall()
                if "item_types" in table_names
                else []
            )
            meta = dict(connection.execute("SELECT key, value FROM metadata").fetchall())

        systems = {
            int(row[0]): SolarSystem(int(row[0]), int(row[1]), str(row[2]), float(row[3]))
            for row in system_rows
        }
        adjacency_work: dict[int, list[int]] = {system_id: [] for system_id in systems}
        for from_id, to_id in jump_rows:
            adjacency_work.setdefault(int(from_id), []).append(int(to_id))
        adjacency = {
            system_id: tuple(sorted(set(neighbours)))
            for system_id, neighbours in adjacency_work.items()
        }
        stations = {int(row[0]): int(row[1]) for row in station_rows}
        regions = {
            int(row[0]): Region(
                int(row[0]),
                str(row[1]),
                int(row[2]) if row[2] is not None else None,
            )
            for row in region_rows
        }
        gates = {
            int(row[0]): Stargate(
                int(row[0]),
                int(row[1]),
                float(row[2]),
                float(row[3]),
                float(row[4]),
            )
            for row in gate_rows
        }
        type_groups = {
            int(row[0]): TypeGroup(int(row[0]), int(row[1]), str(row[2]))
            for row in group_rows
        }
        type_group_by_type_id = {int(row[0]): int(row[1]) for row in type_rows}
        metadata = SdeMetadata(
            build_number=int(meta["build_number"]),
            release_date=meta["release_date"],
            source_url=meta["source_url"],
        )
        return cls(
            systems=systems,
            adjacency=adjacency,
            station_systems=stations,
            regions=regions,
            metadata=metadata,
            gates=gates,
            type_groups=type_groups,
            type_group_by_type_id=type_group_by_type_id,
        )

    def station_system(self, location_id: int) -> int | None:
        return self.station_systems.get(location_id)

    def item_group(self, type_id: int) -> TypeGroup | None:
        """Return the SDE group for an item/ship/weapon type, when published in the SDE."""

        group_id = self.type_group_by_type_id.get(type_id)
        return self.type_groups.get(group_id) if group_id is not None else None

    def region_ids_for_security_bands(
        self,
        allowed_bands: Iterable[SecurityBand],
    ) -> frozenset[int]:
        """Return regions containing at least one system in an allowed security band.

        Region-level contract discovery is coarser than system-level route policy. Retaining a
        mixed-security region whenever *any* system matches makes this a safe acquisition filter;
        endpoint and transit systems are still checked individually by the planner.
        """

        bands = frozenset(allowed_bands)
        if not bands:
            raise ValueError("at least one security band is required")
        return frozenset(
            region_id
            for region_id, present_bands in self.region_security_bands.items()
            if bands & present_bands
        )

    def empire_region_ids(self) -> frozenset[int]:
        """Return SDE faction-owned high/low regions (NPC Empire space).

        ``factionID`` prevents special non-faction high-security regions from being mistaken for
        Empire space. Requiring a high- or low-security system deliberately excludes NPC nullsec;
        this preset means Empire space, not every region with NPC sovereignty.
        """

        empire_bands = frozenset({SecurityBand.HIGH, SecurityBand.LOW})
        return frozenset(
            region_id
            for region_id, region in self.regions.items()
            if region.faction_id is not None
            and bool(self.region_security_bands.get(region_id, frozenset()) & empire_bands)
        )

    def nearest_gate(
        self,
        system_id: int,
        position: tuple[float, float, float],
        *,
        maximum_distance_m: float,
    ) -> tuple[Stargate, float] | None:
        """Return the nearest gate inside a strict, caller-declared distance radius."""

        if maximum_distance_m < 0:
            raise ValueError("maximum gate distance cannot be negative")
        x_m, y_m, z_m = position
        nearest: tuple[Stargate, float] | None = None
        maximum_squared = maximum_distance_m * maximum_distance_m
        for gate in self.gates_by_system.get(system_id, ()):
            distance_squared = (
                (gate.x_m - x_m) ** 2 + (gate.y_m - y_m) ** 2 + (gate.z_m - z_m) ** 2
            )
            if distance_squared > maximum_squared:
                continue
            distance = distance_squared**0.5
            if nearest is None or distance < nearest[1]:
                nearest = (gate, distance)
        return nearest

    def system_allowed(self, system_id: int, policy: SecurityPolicy) -> bool:
        system = self.systems.get(system_id)
        return system is not None and policy.permits(system_id, system.security_status)

    def distances_from(
        self,
        source: int,
        targets: Iterable[int],
        policy: SecurityPolicy,
    ) -> dict[int, int]:
        """Return exact shortest stargate jump counts under the security policy."""

        target_set = set(targets)
        if not target_set:
            return {}
        if not self.system_allowed(source, policy):
            return {}
        found: dict[int, int] = {}
        queue: deque[tuple[int, int]] = deque([(source, 0)])
        seen = {source}
        while queue and len(found) < len(target_set):
            node, distance = queue.popleft()
            if node in target_set:
                found[node] = distance
            for neighbour in self.adjacency.get(node, ()):
                if neighbour in seen or not self.system_allowed(neighbour, policy):
                    continue
                seen.add(neighbour)
                queue.append((neighbour, distance + 1))
        return found

    def reachable_system_ids(
        self,
        source: int,
        policy: SecurityPolicy,
        *,
        max_jumps: int,
    ) -> frozenset[int]:
        """Return the exact policy-permitted BFS ball around ``source``.

        This is useful before live threat collection: every system visited by any route that fits a
        ``max_jumps`` travel budget must be inside this set. Removing threat systems later can only
        shrink reachability, so collecting intel for this superset is proof-safe.
        """

        if max_jumps < 0:
            raise ValueError("max_jumps cannot be negative")
        if not self.system_allowed(source, policy):
            return frozenset()
        seen = {source}
        queue: deque[tuple[int, int]] = deque([(source, 0)])
        while queue:
            node, distance = queue.popleft()
            if distance >= max_jumps:
                continue
            for neighbour in self.adjacency.get(node, ()):
                if neighbour in seen or not self.system_allowed(neighbour, policy):
                    continue
                seen.add(neighbour)
                queue.append((neighbour, distance + 1))
        return frozenset(seen)

    def reachable_region_ids(
        self,
        source: int,
        policy: SecurityPolicy,
        *,
        max_jumps: int,
    ) -> frozenset[int]:
        """Return SDE regions containing a system inside the permitted BFS ball."""

        return frozenset(
            self.systems[system_id].region_id
            for system_id in self.reachable_system_ids(
                source,
                policy,
                max_jumps=max_jumps,
            )
        )

    def shortest_path(
        self,
        source: int,
        destination: int,
        policy: SecurityPolicy,
    ) -> tuple[int, ...] | None:
        """Return one shortest gate path, or ``None`` when the destination is unreachable."""

        key = (source, destination, policy)
        if key in self._path_cache:
            result = self._path_cache.pop(key)
            self._path_cache[key] = result
            return result
        if source == destination:
            result = (source,) if self.system_allowed(source, policy) else None
            self._remember_path(key, result)
            return result
        if not self.system_allowed(source, policy) or not self.system_allowed(destination, policy):
            self._remember_path(key, None)
            return None
        queue: deque[int] = deque([source])
        parent: dict[int, int | None] = {source: None}
        while queue:
            node = queue.popleft()
            for neighbour in self.adjacency.get(node, ()):
                if neighbour in parent or not self.system_allowed(neighbour, policy):
                    continue
                parent[neighbour] = node
                if neighbour == destination:
                    path: list[int] = [destination]
                    current: int | None = destination
                    while current is not None and current != source:
                        current = parent[current]
                        if current is not None:
                            path.append(current)
                    path.reverse()
                    result = tuple(path)
                    self._remember_path(key, result)
                    return result
                queue.append(neighbour)
        self._remember_path(key, None)
        return None

    def _remember_path(
        self,
        key: tuple[int, int, SecurityPolicy],
        path: tuple[int, ...] | None,
    ) -> None:
        self._path_cache[key] = path
        self._path_cache.move_to_end(key)
        while len(self._path_cache) > 2_048:
            self._path_cache.popitem(last=False)

    def jump_matrix(
        self,
        system_ids: Iterable[int],
        policy: SecurityPolicy,
    ) -> dict[tuple[int, int], int]:
        """Return a cached exact metric closure for one relevant system set and policy."""

        unique = tuple(sorted(set(system_ids)))
        key = (policy, unique)
        cached = self._matrix_cache.pop(key, None)
        if cached is not None:
            self._matrix_cache[key] = cached
            return dict(cached)
        matrix: dict[tuple[int, int], int] = {}
        for source in unique:
            distances = self.distances_from(source, unique, policy)
            for destination, jumps in distances.items():
                matrix[(source, destination)] = jumps
        self._matrix_cache[key] = matrix
        while len(self._matrix_cache) > 8:
            self._matrix_cache.popitem(last=False)
        return dict(matrix)


def build_jump_matrix(
    graph: UniverseGraph,
    system_ids: Iterable[int],
    policy: SecurityPolicy,
) -> dict[tuple[int, int], int]:
    """Compute exact shortest jump counts between all relevant systems."""

    return graph.jump_matrix(system_ids, policy)


def load_bundled_graph() -> UniverseGraph:
    resource = files("eve_courier_optimizer").joinpath(DEFAULT_SDE_RESOURCE)
    with as_file(resource) as path:
        return UniverseGraph.from_sqlite(Path(path))

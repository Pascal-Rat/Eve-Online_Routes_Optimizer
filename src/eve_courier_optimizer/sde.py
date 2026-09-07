"""Load the compact route subset of CCP's Static Data Export and find gate paths."""

from __future__ import annotations

import sqlite3
from collections import OrderedDict, deque
from collections.abc import Iterable, Mapping
from contextlib import closing
from dataclasses import dataclass
from importlib.resources import as_file, files
from pathlib import Path
from types import MappingProxyType
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


type PolicyKey = tuple[float | None, frozenset[SecurityBand] | None, frozenset[int]]


def _policy_key(policy: SecurityPolicy) -> PolicyKey:
    return (
        policy.minimum_security if policy.allowed_bands is None else None,
        policy.allowed_bands,
        policy.avoided_system_ids
        | policy.gank_avoided_system_ids
        | policy.threat_avoided_system_ids,
    )


@dataclass(slots=True)
class _ShortestPaths:
    distances: dict[int, int]
    parents: dict[int, int]

    def path_to(self, source: int, destination: int) -> tuple[int, ...] | None:
        if destination not in self.distances:
            return None
        path = [destination]
        while path[-1] != source:
            path.append(self.parents[path[-1]])
        return tuple(reversed(path))


def _shortest_paths(
    adjacency: dict[int, tuple[int, ...]],
    source: int,
    *,
    targets: set[int] | None = None,
    max_jumps: int | None = None,
) -> _ShortestPaths:
    if source not in adjacency:
        return _ShortestPaths({}, {})
    distances = {source: 0}
    parents: dict[int, int] = {}
    remaining = targets.copy() if targets is not None else None
    queue = deque([source])
    while queue and (remaining is None or remaining):
        node = queue.popleft()
        distance = distances[node]
        if remaining is not None:
            remaining.discard(node)
        if max_jumps is not None and distance >= max_jumps:
            continue
        for neighbor in adjacency[node]:
            if neighbor not in distances:
                distances[neighbor] = distance + 1
                parents[neighbor] = node
                queue.append(neighbor)
    return _ShortestPaths(distances, parents)


class UniverseGraph:
    """In-memory static stargate graph optimized for many route queries."""

    def __init__(
        self,
        *,
        systems: Mapping[int, SolarSystem],
        adjacency: Mapping[int, tuple[int, ...]],
        station_systems: Mapping[int, int],
        regions: Mapping[int, Region],
        metadata: SdeMetadata,
        gates: Mapping[int, Stargate] | None = None,
        type_groups: Mapping[int, TypeGroup] | None = None,
        type_group_by_type_id: Mapping[int, int] | None = None,
    ) -> None:
        self._systems = dict(systems)
        self._adjacency = {node: tuple(sorted(set(adjacency.get(node, ())))) for node in systems}
        self._station_systems = dict(station_systems)
        self._regions = dict(regions)
        self.metadata = metadata
        self._gates = dict(gates or {})
        self._type_groups = dict(type_groups or {})
        self._type_group_by_type_id = dict(type_group_by_type_id or {})
        region_bands: dict[int, set[SecurityBand]] = {
            region_id: set() for region_id in self._regions
        }
        for system in self._systems.values():
            region_bands.setdefault(system.region_id, set()).add(
                security_band(system.security_status)
            )
        self._region_security_bands = {
            region_id: frozenset(bands) for region_id, bands in region_bands.items()
        }
        gates_by_system: dict[int, list[Stargate]] = {}
        for gate in self._gates.values():
            gates_by_system.setdefault(gate.system_id, []).append(gate)
        self._gates_by_system = {
            system_id: tuple(sorted(items, key=lambda item: item.gate_id))
            for system_id, items in gates_by_system.items()
        }
        self._reset_caches()

    def _reset_caches(self) -> None:
        self._policy_graphs: OrderedDict[PolicyKey, dict[int, tuple[int, ...]]] = OrderedDict()
        self._matrix_cache: OrderedDict[
            tuple[PolicyKey, tuple[int, ...]], dict[tuple[int, int], int]
        ] = OrderedDict()
        self._path_cache: OrderedDict[tuple[int, int, PolicyKey], tuple[int, ...] | None] = (
            OrderedDict()
        )

    def __getstate__(self) -> dict[str, object]:
        # Spawned workers need the SDE, not the parent's expendable query caches.
        return {
            key: value
            for key, value in self.__dict__.items()
            if key not in {"_policy_graphs", "_matrix_cache", "_path_cache"}
        }

    def __setstate__(self, state: dict[str, object]) -> None:
        self.__dict__.update(state)
        self._reset_caches()

    @property
    def systems(self) -> Mapping[int, SolarSystem]:
        return MappingProxyType(self._systems)

    @property
    def adjacency(self) -> Mapping[int, tuple[int, ...]]:
        return MappingProxyType(self._adjacency)

    @property
    def station_systems(self) -> Mapping[int, int]:
        return MappingProxyType(self._station_systems)

    @property
    def regions(self) -> Mapping[int, Region]:
        return MappingProxyType(self._regions)

    @property
    def gates(self) -> Mapping[int, Stargate]:
        return MappingProxyType(self._gates)

    @property
    def type_groups(self) -> Mapping[int, TypeGroup]:
        return MappingProxyType(self._type_groups)

    @property
    def type_group_by_type_id(self) -> Mapping[int, int]:
        return MappingProxyType(self._type_group_by_type_id)

    @classmethod
    def from_sqlite(cls, path: Path) -> UniverseGraph:
        if not path.exists():
            raise FileNotFoundError(f"SDE route database not found: {path}")
        uri = f"file:{path.resolve()}?mode=ro"
        with closing(sqlite3.connect(uri, uri=True)) as connection:
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
                connection.execute("SELECT gate_id, system_id, x_m, y_m, z_m FROM gates").fetchall()
                if "gates" in table_names
                else []
            )
            group_rows = (
                connection.execute("SELECT group_id, category_id, name FROM type_groups").fetchall()
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
            system_id: tuple(neighbours) for system_id, neighbours in adjacency_work.items()
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
            int(row[0]): TypeGroup(int(row[0]), int(row[1]), str(row[2])) for row in group_rows
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

    def resolve_system(self, value: object) -> int:
        text = str(value).strip()
        if not text:
            raise ValueError("start system is required")
        try:
            system_id = int(text)
        except ValueError:
            matches = [
                system.system_id
                for system in self.systems.values()
                if system.name.casefold() == text.casefold()
            ]
            if len(matches) != 1:
                raise ValueError(f"could not resolve unique system {text!r}") from None
            return matches[0]
        if system_id not in self.systems:
            raise ValueError(f"unknown system ID {system_id}")
        return system_id

    def resolve_region(self, value: object) -> int:
        text = str(value).strip()
        if not text:
            raise ValueError("region cannot be empty")
        try:
            region_id = int(text)
        except ValueError:
            matches = [
                region.region_id
                for region in self.regions.values()
                if region.name.casefold() == text.casefold()
            ]
            if len(matches) != 1:
                raise ValueError(f"could not resolve unique region {text!r}") from None
            return matches[0]
        if region_id not in self.regions:
            raise ValueError(f"unknown region ID {region_id}")
        return region_id

    def station_system(self, location_id: int) -> int | None:
        return self._station_systems.get(location_id)

    def item_group(self, type_id: int) -> TypeGroup | None:
        """Return the SDE group for an item/ship/weapon type, when published in the SDE."""

        group_id = self._type_group_by_type_id.get(type_id)
        return self._type_groups.get(group_id) if group_id is not None else None

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
            for region_id, present_bands in self._region_security_bands.items()
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
            for region_id, region in self._regions.items()
            if region.faction_id is not None
            and bool(self._region_security_bands.get(region_id, frozenset()) & empire_bands)
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
        for gate in self._gates_by_system.get(system_id, ()):
            distance_squared = (gate.x_m - x_m) ** 2 + (gate.y_m - y_m) ** 2 + (gate.z_m - z_m) ** 2
            if distance_squared > maximum_squared:
                continue
            distance = distance_squared**0.5
            if nearest is None or distance < nearest[1]:
                nearest = (gate, distance)
        return nearest

    def system_allowed(self, system_id: int, policy: SecurityPolicy) -> bool:
        system = self._systems.get(system_id)
        return system is not None and policy.permits(system_id, system.security_status)

    def _permitted_graph(self, policy: SecurityPolicy) -> dict[int, tuple[int, ...]]:
        key = _policy_key(policy)
        cached = self._policy_graphs.get(key)
        if cached is not None:
            self._policy_graphs.move_to_end(key)
            return cached
        allowed = {
            node
            for node, system in self._systems.items()
            if policy.permits(node, system.security_status)
        }
        adjacency = {
            node: tuple(neighbor for neighbor in self._adjacency[node] if neighbor in allowed)
            for node in allowed
        }
        self._policy_graphs[key] = adjacency
        if len(self._policy_graphs) > 8:
            self._policy_graphs.popitem(last=False)
        return adjacency

    def distances_from(
        self,
        source: int,
        targets: Iterable[int],
        policy: SecurityPolicy,
    ) -> dict[int, int]:
        targets = set(targets)
        if not targets:
            return {}
        paths = _shortest_paths(self._permitted_graph(policy), source, targets=targets)
        return {node: distance for node, distance in paths.distances.items() if node in targets}

    def reachable_system_ids(
        self,
        source: int,
        policy: SecurityPolicy,
        *,
        max_jumps: int,
    ) -> frozenset[int]:
        """Exact policy-permitted BFS ball, including the source at zero jumps."""
        if max_jumps < 0:
            raise ValueError("max_jumps cannot be negative")
        return frozenset(
            _shortest_paths(self._permitted_graph(policy), source, max_jumps=max_jumps).distances
        )

    def reachable_region_ids(
        self,
        source: int,
        policy: SecurityPolicy,
        *,
        max_jumps: int,
    ) -> frozenset[int]:
        """Return SDE regions containing a system inside the permitted BFS ball."""

        return frozenset(
            self._systems[system_id].region_id
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

        key = (source, destination, _policy_key(policy))
        if key in self._path_cache:
            self._path_cache.move_to_end(key)
            return self._path_cache[key]
        adjacency = self._permitted_graph(policy)
        path = (
            _shortest_paths(adjacency, source, targets={destination}).path_to(source, destination)
            if source in adjacency and destination in adjacency
            else None
        )
        self._path_cache[key] = path
        if len(self._path_cache) > 2_048:
            self._path_cache.popitem(last=False)
        return path

    def jump_matrix(
        self,
        system_ids: Iterable[int],
        policy: SecurityPolicy,
    ) -> dict[tuple[int, int], int]:
        """Return a cached exact metric closure for one relevant system set and policy."""

        unique = tuple(sorted(set(system_ids)))
        key = (_policy_key(policy), unique)
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


def load_bundled_graph() -> UniverseGraph:
    resource = files("eve_courier_optimizer").joinpath(DEFAULT_SDE_RESOURCE)
    with as_file(resource) as path:
        return UniverseGraph.from_sqlite(Path(path))

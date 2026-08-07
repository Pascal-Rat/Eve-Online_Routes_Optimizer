"""Core immutable domain types and exact unit conversions.

The solver never receives binary floating-point cargo or ISK values. ESI exposes some
contract fields as JSON numbers backed by floating point, so values are parsed as
``Decimal`` and normalized at this boundary.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_UP, Decimal
from enum import StrEnum
from typing import Final

VOLUME_UNITS_PER_M3: Final = 1_000  # 0.001 m3; contract demand rounds upward.
ISK_UNITS_PER_ISK: Final = 100  # centi-ISK.
_ISK_SUFFIXES: Final[dict[str, Decimal]] = {
    "": Decimal(1),
    "isk": Decimal(1),
    "k": Decimal(1_000),
    "m": Decimal(1_000_000),
    "b": Decimal(1_000_000_000),
}
_ISK_PATTERN: Final = re.compile(
    r"^\s*([+]?(?:\d+(?:\.\d*)?|\.\d+))\s*([kKmMbB]?)\s*(?:ISK)?\s*$",
    re.IGNORECASE,
)


def _decimal(value: Decimal | int | float | str) -> Decimal:
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


def cargo_volume_to_units(value_m3: Decimal | int | float | str) -> int:
    """Convert contract volume to conservative integer units (round demand upward)."""

    value = _decimal(value_m3)
    if not value.is_finite() or value < 0:
        raise ValueError("cargo volume must be a finite non-negative number")
    return int((value * VOLUME_UNITS_PER_M3).to_integral_value(rounding=ROUND_CEILING))


def cargo_capacity_to_units(value_m3: Decimal | int | float | str) -> int:
    """Convert ship capacity to conservative integer units (round capacity downward)."""

    value = _decimal(value_m3)
    if not value.is_finite() or value < 0:
        raise ValueError("cargo capacity must be a finite non-negative number")
    return int((value * VOLUME_UNITS_PER_M3).to_integral_value(rounding=ROUND_FLOOR))


def isk_to_units(value_isk: Decimal | int | float | str) -> int:
    """Convert ISK to centi-ISK, removing harmless JSON floating-point residue."""

    value = _decimal(value_isk)
    if not value.is_finite() or value < 0:
        raise ValueError("ISK value must be a finite non-negative number")
    return int((value * ISK_UNITS_PER_ISK).to_integral_value(rounding=ROUND_HALF_UP))


def parse_human_isk(value: object, *, unit: str = "auto") -> Decimal:
    """Parse a human collateral amount such as ``750M`` or ``4B`` exactly.

    When the text contains a K/M/B suffix it is authoritative. Otherwise ``unit`` may be
    ``auto``/``isk`` or one of those suffixes. Commas and underscores are accepted as visual
    separators, so ``1,250M`` is valid without introducing binary floating-point arithmetic.
    """

    normalized_unit = unit.strip().casefold()
    if normalized_unit == "auto":
        normalized_unit = ""
    if normalized_unit not in _ISK_SUFFIXES:
        raise ValueError("collateral unit must be auto, ISK, K, M, or B")
    text = str(value).replace(",", "").replace("_", "")
    match = _ISK_PATTERN.fullmatch(text)
    if match is None:
        raise ValueError("collateral must be a number, optionally followed by K, M, or B")
    number = Decimal(match.group(1))
    suffix = match.group(2).casefold()
    multiplier = _ISK_SUFFIXES[suffix or normalized_unit]
    amount = number * multiplier
    if not amount.is_finite() or amount < 0:
        raise ValueError("collateral must be a finite non-negative amount")
    return amount


def volume_units_to_decimal(units: int) -> Decimal:
    return Decimal(units) / VOLUME_UNITS_PER_M3


def isk_units_to_decimal(units: int) -> Decimal:
    return Decimal(units) / ISK_UNITS_PER_ISK


def parse_esi_datetime(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("ESI timestamp must include a timezone")
    return parsed.astimezone(UTC)


class CollateralMode(StrEnum):
    """How optional public contracts consume collateral."""

    LOCKED = "locked"
    ROLLING = "rolling"


class ActionKind(StrEnum):
    PICKUP = "pickup"
    DELIVERY = "delivery"


class TravelLegKind(StrEnum):
    PICKUP = "pickup"
    DELIVERY = "delivery"
    WAYPOINT = "waypoint"
    FINISH = "finish"


class ProofStatus(StrEnum):
    PROVEN_OPTIMAL = "proven_optimal"
    PROVEN_INFEASIBLE = "proven_infeasible"
    FEASIBLE_NOT_PROVEN = "feasible_not_proven"
    UNKNOWN = "unknown"


class SecurityBand(StrEnum):
    """Displayed EVE security bands derived from the SDE's raw security status."""

    HIGH = "high"
    LOW = "low"
    NULL = "null"


class ThreatCategory(StrEnum):
    """Gate-to-gate danger signatures available from enriched zKill killmails."""

    SUICIDE_GANK = "suicide_gank"
    SMARTBOMB = "smartbomb"
    HEAVY_INTERDICTOR = "heavy_interdictor"
    CARRIER = "carrier"
    GATE_CAMP = "gate_camp"
    HAULER_LOSS = "hauler_loss"
    ANY_GATE_PVP = "any_gate_pvp"


class GateEvidence(StrEnum):
    """Why a killmail is considered relevant to stargate travel."""

    ZKILL_LOCATION = "zkill_location"
    VICTIM_POSITION = "victim_position"


def security_band(security_status: float) -> SecurityBand:
    """Classify raw SDE security using the route model's 0.45 high-sec boundary."""

    if security_status >= 0.45:
        return SecurityBand.HIGH
    if security_status > 0.0:
        return SecurityBand.LOW
    return SecurityBand.NULL


@dataclass(frozen=True, slots=True)
class SystemKillActivity:
    """One ESI system-kill activity row; this is not a suicide-gank classification."""

    system_id: int
    ship_kills: int
    pod_kills: int
    npc_kills: int

    def __post_init__(self) -> None:
        if self.system_id <= 0:
            raise ValueError("activity system_id must be positive")
        if self.ship_kills < 0 or self.pod_kills < 0 or self.npc_kills < 0:
            raise ValueError("system kill counts cannot be negative")


@dataclass(frozen=True, slots=True)
class GateThreatEvent:
    """One player-caused kill located at, or close to, an SDE stargate.

    Pure NPC losses are never materialized as events. ``categories`` records observed signatures,
    not a claim that every future gate transit will encounter the same attackers.
    """

    killmail_id: int
    occurred_at: datetime
    system_id: int
    region_id: int
    gate_id: int
    distance_to_gate_m: int
    evidence: GateEvidence
    categories: frozenset[ThreatCategory]
    victim_ship_type_id: int
    attacker_ship_type_ids: tuple[int, ...] = ()
    attacker_weapon_type_ids: tuple[int, ...] = ()
    player_attacker_count: int = 0
    zkill_labels: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if min(
            self.killmail_id,
            self.system_id,
            self.region_id,
            self.gate_id,
            self.victim_ship_type_id,
        ) <= 0:
            raise ValueError("gate-threat identifiers must be positive")
        if self.occurred_at.tzinfo is None:
            raise ValueError("gate-threat timestamp must be timezone-aware")
        if self.distance_to_gate_m < 0 or not math.isfinite(float(self.distance_to_gate_m)):
            raise ValueError("gate-threat distance must be finite and non-negative")
        if not self.categories:
            raise ValueError("gate-threat event needs at least one category")
        if self.player_attacker_count <= 0:
            raise ValueError("gate-threat event needs at least one player attacker")
        if any(type_id <= 0 for type_id in self.attacker_ship_type_ids):
            raise ValueError("attacker ship type IDs must be positive")
        if any(type_id <= 0 for type_id in self.attacker_weapon_type_ids):
            raise ValueError("attacker weapon type IDs must be positive")


@dataclass(frozen=True, slots=True)
class PublicCourierContract:
    contract_id: int
    origin_location_id: int
    destination_location_id: int
    volume_units: int
    collateral_units: int
    reward_units: int
    date_expired: datetime
    days_to_complete: int
    title: str = ""
    date_issued: datetime | None = None

    def __post_init__(self) -> None:
        if self.contract_id <= 0:
            raise ValueError("contract_id must be positive")
        if self.origin_location_id <= 0 or self.destination_location_id <= 0:
            raise ValueError("contract locations must be positive")
        if self.volume_units < 0 or self.collateral_units < 0 or self.reward_units < 0:
            raise ValueError("volume, collateral and reward cannot be negative")
        if self.days_to_complete <= 0:
            raise ValueError("days_to_complete must be positive")
        if self.date_expired.tzinfo is None:
            raise ValueError("date_expired must be timezone-aware")


@dataclass(frozen=True, slots=True)
class RoutableContract:
    contract: PublicCourierContract
    origin_system_id: int
    destination_system_id: int

    def __post_init__(self) -> None:
        if self.origin_system_id <= 0 or self.destination_system_id <= 0:
            raise ValueError("system IDs must be positive")


@dataclass(frozen=True, slots=True)
class ActiveShipment:
    """An already accepted courier contract during replanning.

    ``picked`` distinguishes collateral-only commitments awaiting pickup from packages already in
    cargo. Both are mandatory: replanning may reorder them but may not silently drop them.
    """

    contract: RoutableContract
    deadline: datetime
    picked: bool = True

    def __post_init__(self) -> None:
        if self.deadline.tzinfo is None:
            raise ValueError("active-shipment deadline must be timezone-aware")


@dataclass(frozen=True, slots=True)
class TravelTimeModel:
    """Deterministic time model under which an optimality proof is meaningful."""

    seconds_per_jump: int = 60
    service_seconds: int = 30

    def __post_init__(self) -> None:
        if self.seconds_per_jump <= 0:
            raise ValueError("seconds_per_jump must be positive")
        if self.service_seconds < 0:
            raise ValueError("service_seconds cannot be negative")


@dataclass(frozen=True, slots=True)
class SecurityPolicy:
    """Declared system constraints for stargate routing.

    CCP's documented route model treats security status >= 0.45 as high-security.
    ``allowed_bands`` supersedes the legacy ``minimum_security`` threshold when supplied. The
    separate manual/activity avoidance sets make the proof's policy boundary auditable.
    """

    minimum_security: float | None = 0.45
    avoided_system_ids: frozenset[int] = field(default_factory=frozenset)
    allowed_bands: frozenset[SecurityBand] | None = None
    gank_avoided_system_ids: frozenset[int] = field(default_factory=frozenset)
    gank_ship_kill_threshold: int | None = None
    gank_activity_fetched_at: datetime | None = None
    threat_avoided_system_ids: frozenset[int] = field(default_factory=frozenset)
    threat_categories: frozenset[ThreatCategory] = field(default_factory=frozenset)
    threat_min_events: int | None = None
    threat_intel_fetched_at: datetime | None = None
    threat_window_seconds: int | None = None
    threat_gate_radius_m: int | None = None
    threat_coverage_region_ids: frozenset[int] = field(default_factory=frozenset)
    threat_incomplete_region_ids: frozenset[int] = field(default_factory=frozenset)

    def __post_init__(self) -> None:
        if self.allowed_bands is not None and not self.allowed_bands:
            raise ValueError("at least one security band must be allowed")
        if self.gank_ship_kill_threshold is not None and self.gank_ship_kill_threshold <= 0:
            raise ValueError("gank ship-kill threshold must be positive")
        if (
            self.gank_activity_fetched_at is not None
            and self.gank_activity_fetched_at.tzinfo is None
        ):
            raise ValueError("gank activity timestamp must be timezone-aware")
        if self.gank_avoided_system_ids and self.gank_ship_kill_threshold is None:
            raise ValueError("gank-derived avoids require a ship-kill threshold")
        if self.gank_ship_kill_threshold is not None and self.gank_activity_fetched_at is None:
            raise ValueError("gank awareness requires a recorded activity timestamp")
        if self.threat_min_events is not None and self.threat_min_events <= 0:
            raise ValueError("gate-threat minimum event count must be positive")
        if self.threat_window_seconds is not None and self.threat_window_seconds <= 0:
            raise ValueError("gate-threat window must be positive")
        if self.threat_gate_radius_m is not None and self.threat_gate_radius_m < 0:
            raise ValueError("gate-threat radius cannot be negative")
        if self.threat_intel_fetched_at is not None and self.threat_intel_fetched_at.tzinfo is None:
            raise ValueError("gate-threat timestamp must be timezone-aware")
        threat_enabled = bool(self.threat_categories) or bool(self.threat_avoided_system_ids)
        if threat_enabled and self.threat_min_events is None:
            raise ValueError("gate-threat categories require a minimum event count")
        if threat_enabled and self.threat_intel_fetched_at is None:
            raise ValueError("gate-threat categories require a recorded intel timestamp")
        if threat_enabled and self.threat_window_seconds is None:
            raise ValueError("gate-threat categories require a recorded lookback window")
        if threat_enabled and self.threat_gate_radius_m is None:
            raise ValueError("gate-threat categories require a recorded gate radius")
        if self.threat_avoided_system_ids and not self.threat_categories:
            raise ValueError("gate-threat avoids require at least one selected category")
        if any(region_id <= 0 for region_id in self.threat_coverage_region_ids):
            raise ValueError("gate-threat coverage region IDs must be positive")
        if any(region_id <= 0 for region_id in self.threat_incomplete_region_ids):
            raise ValueError("gate-threat incomplete region IDs must be positive")

    def rejection_reason(self, system_id: int, security_status: float) -> str | None:
        """Return the first declared policy that excludes a system, if any."""

        if system_id in self.avoided_system_ids:
            return "manual_avoid_policy"
        if system_id in self.threat_avoided_system_ids:
            return "gate_threat_policy"
        if system_id in self.gank_avoided_system_ids:
            return "gank_activity_policy"
        if self.allowed_bands is not None:
            return (
                None
                if security_band(security_status) in self.allowed_bands
                else "security_policy"
            )
        if self.minimum_security is not None and security_status < self.minimum_security:
            return "security_policy"
        return None

    def permits(self, system_id: int, security_status: float) -> bool:
        return self.rejection_reason(system_id, security_status) is None


@dataclass(frozen=True, slots=True)
class PlanningConstraints:
    start_system_id: int
    cargo_capacity_units: int
    collateral_budget_units: int
    horizon_seconds: int
    snapshot_time: datetime
    collateral_mode: CollateralMode = CollateralMode.LOCKED
    travel: TravelTimeModel = field(default_factory=TravelTimeModel)
    security: SecurityPolicy = field(default_factory=SecurityPolicy)
    return_to_start: bool = True
    required_system_ids: frozenset[int] = frozenset()
    finish_system_id: int | None = None
    max_simultaneous_contracts: int | None = None

    def __post_init__(self) -> None:
        if self.start_system_id <= 0:
            raise ValueError("start_system_id must be positive")
        if self.cargo_capacity_units < 0 or self.collateral_budget_units < 0:
            raise ValueError("capacity and collateral budget cannot be negative")
        if self.horizon_seconds < 0:
            raise ValueError("horizon cannot be negative")
        if self.snapshot_time.tzinfo is None:
            raise ValueError("snapshot_time must be timezone-aware")
        if any(system_id <= 0 for system_id in self.required_system_ids):
            raise ValueError("required route system IDs must be positive")
        if self.finish_system_id is not None and self.finish_system_id <= 0:
            raise ValueError("finish_system_id must be positive when supplied")
        if (
            self.return_to_start
            and self.finish_system_id is not None
            and self.finish_system_id != self.start_system_id
        ):
            raise ValueError("a loop route cannot finish somewhere other than its start")
        if self.max_simultaneous_contracts is not None and self.max_simultaneous_contracts < 0:
            raise ValueError("max_simultaneous_contracts cannot be negative")

    @property
    def terminal_system_id(self) -> int | None:
        """Return the required final system, or ``None`` for an unconstrained open route."""

        return self.start_system_id if self.return_to_start else self.finish_system_id


@dataclass(frozen=True, slots=True)
class ProblemScope:
    snapshot_fetched_at: datetime
    snapshot_compatibility_date: str
    sde_build_number: int
    scanned_region_ids: tuple[int, ...]
    public_couriers_seen: int
    eligible_contracts: int
    policy_exclusions: tuple[tuple[str, int], ...] = ()
    safe_reductions: tuple[tuple[str, int], ...] = ()
    heuristic_reductions: tuple[tuple[str, int], ...] = ()

    @property
    def is_untruncated(self) -> bool:
        return not self.heuristic_reductions


@dataclass(frozen=True, slots=True)
class RouteProblem:
    constraints: PlanningConstraints
    contracts: tuple[RoutableContract, ...]
    scope: ProblemScope
    active_shipments: tuple[ActiveShipment, ...] = ()


@dataclass(frozen=True, slots=True)
class RouteStep:
    sequence: int
    action: ActionKind
    contract_id: int
    system_id: int
    location_id: int
    arrival_seconds: int
    completion_seconds: int
    cargo_after_units: int
    collateral_after_units: int
    cumulative_reward_units: int
    jump_path: tuple[int, ...] = ()


@dataclass(frozen=True, slots=True)
class TravelLeg:
    sequence: int
    kind: TravelLegKind
    from_system_id: int
    to_system_id: int
    arrival_seconds: int
    completion_seconds: int
    jump_path: tuple[int, ...]
    contract_id: int | None = None


@dataclass(frozen=True, slots=True)
class OptimalityCertificate:
    status: ProofStatus
    solver_status: str
    objective_units: int | None
    best_bound_units: int | None
    absolute_gap_units: int | None
    relative_gap: float | None
    problem_sha256: str
    solver_name: str
    solver_version: str
    wall_time_seconds: float
    branches: int
    conflicts: int
    scope_untruncated: bool
    feasibility_verified: bool
    independent_reference_verified: bool
    claim: str
    system_relaxation_status: str | None = None
    system_relaxation_bound_units: int | None = None
    system_relaxation_wall_time_seconds: float = 0.0
    system_relaxation_systems: int = 0
    incompatibility_pairs: int = 0
    incompatibility_cliques: int = 0
    decomposition_status: str | None = None
    decomposition_iterations: int = 0
    decomposition_learned_cuts: int = 0
    decomposition_subproblem_wall_time_seconds: float = 0.0
    decomposition_proof_closed: bool = False


@dataclass(frozen=True, slots=True)
class SolveResult:
    selected_contract_ids: tuple[int, ...]
    route: tuple[RouteStep, ...]
    total_reward_units: int
    finish_seconds: int
    certificate: OptimalityCertificate
    travel_legs: tuple[TravelLeg, ...] = ()


@dataclass(frozen=True, slots=True)
class ValidationReport:
    valid: bool
    violations: tuple[str, ...]

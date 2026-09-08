"""Budgets for the exact route search and its proof-preserving prepasses."""

from __future__ import annotations

import math
import time
from dataclasses import dataclass

from ortools.sat.python import cp_model

# Reserve a bounded route-neighborhood phase inside the overall solve budget.
INCUMBENT_DIVERSIFICATION_SECONDS = 1.0


class SearchBudget:
    """One monotonic, cooperative deadline shared by setup, search and refinement."""

    def __init__(self, seconds: float | None) -> None:
        self.deadline = math.inf if seconds is None else time.perf_counter() + seconds

    def remaining(self, cap: float = math.inf) -> float:
        return max(0.0, min(cap, self.deadline - time.perf_counter()))

    def expired(self) -> bool:
        return self.remaining() <= 0


@dataclass(frozen=True, slots=True)
class SolverConfig:
    # Total RouteOptimizer.solve allowance; phase limits below share this budget.
    max_time_seconds: float | None = 300.0
    num_workers: int = 1
    random_seed: int = 0
    log_search_progress: bool = False
    minimize_finish_time_after_proof: bool = True
    secondary_time_seconds: float = 30.0
    independent_reference_limit: int = 10
    relaxation_time_seconds: float = 10.0
    decomposition_time_seconds: float = 20.0
    decomposition_subproblem_time_seconds: float = 2.0
    decomposition_max_iterations: int = 8

    def __post_init__(self) -> None:
        for name, value, allow_zero in (
            ("max_time_seconds", self.max_time_seconds, False),
            ("secondary_time_seconds", self.secondary_time_seconds, False),
            ("relaxation_time_seconds", self.relaxation_time_seconds, True),
            ("decomposition_time_seconds", self.decomposition_time_seconds, True),
            (
                "decomposition_subproblem_time_seconds",
                self.decomposition_subproblem_time_seconds,
                False,
            ),
        ):
            if value is None:
                continue
            if not math.isfinite(value) or value < 0 or (value == 0 and not allow_zero):
                limit = "non-negative" if allow_zero else "positive"
                raise ValueError(f"{name} must be finite and {limit}")
        if self.num_workers <= 0:
            raise ValueError("num_workers must be positive")
        if self.independent_reference_limit < 0:
            raise ValueError("independent_reference_limit cannot be negative")
        if self.decomposition_max_iterations <= 0:
            raise ValueError("decomposition_max_iterations must be positive")

    def solver(
        self, *, seconds: float | None = None, workers: int | None = None
    ) -> cp_model.CpSolver:
        solver = cp_model.CpSolver()
        budget = self.max_time_seconds if seconds is None else seconds
        if budget is not None:
            solver.parameters.max_time_in_seconds = budget
        solver.parameters.num_search_workers = self.num_workers if workers is None else workers
        solver.parameters.random_seed = self.random_seed
        solver.parameters.log_search_progress = self.log_search_progress
        return solver

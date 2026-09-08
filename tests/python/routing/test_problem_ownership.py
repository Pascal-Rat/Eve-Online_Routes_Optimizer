from __future__ import annotations

import pickle
from dataclasses import replace
from datetime import datetime

import pytest

from eve_courier_optimizer.routing.route_problem import RouteProblem
from eve_courier_optimizer.routing.universe import UniverseGraph
from tests.support.scenarios import make_contract, make_snapshot
from tests.support.scenarios import reward_constraints as constraints


def test_problem_owns_read_only_distances_across_subsets_and_spawn(
    now: datetime, tiny_graph: UniverseGraph
) -> None:
    problem = RouteProblem.from_snapshot(
        make_snapshot(now, make_contract(now, 1, 101, 103)), tiny_graph, constraints(now)
    )
    supplied = dict(problem.jump_matrix)
    owned = replace(problem, jump_matrix=supplied)
    supplied[1, 3] = 999
    assert owned.jump_matrix[1, 3] == 2
    subset = owned.restrict_contracts(())
    with pytest.raises(TypeError):
        subset.jump_matrix[1, 3] = 999  # type: ignore[index]  # pyright: ignore[reportIndexIssue]
    restored = pickle.loads(pickle.dumps(owned))
    assert restored == owned
    assert restored.jump_matrix[1, 3] == 2
    with pytest.raises(TypeError):
        restored.jump_matrix[1, 3] = 999

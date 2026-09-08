"""Importable spawn targets for competing-writer integration tests."""

from multiprocessing.connection import Connection
from pathlib import Path

from eve_courier_optimizer.application.file_lock import WorkspaceConflict
from eve_courier_optimizer.eve.esi import EsiClient
from eve_courier_optimizer.routing.universe import UniverseGraph
from eve_courier_optimizer.web.workspace import PlanningWorkspace


def stale_reset(graph: UniverseGraph, directory: Path, channel: Connection) -> None:
    try:
        workspace = PlanningWorkspace(graph, EsiClient(), directory)
        channel.send(workspace.revision)
        channel.recv()
        try:
            workspace.reset_execution()
        except WorkspaceConflict:
            channel.send("conflict")
        else:
            channel.send("overwritten")
    finally:
        channel.close()

#!/usr/bin/env python3
"""Download the current CCP SDE and regenerate the bundled route subset."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from eve_courier_optimizer.sde_build import (
    LatestBuild,
    build_route_database,
    download_sde,
    fetch_latest_build,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("src/eve_courier_optimizer/data/route_sde.sqlite3"),
    )
    parser.add_argument("--zip", dest="zip_path", type=Path)
    parser.add_argument(
        "--build-number",
        type=int,
        help="SDE build represented by --zip; pair with --release-date for an offline old build",
    )
    parser.add_argument(
        "--release-date",
        help="SDE release timestamp represented by --zip; pair with --build-number",
    )
    arguments = parser.parse_args()

    explicit_build = arguments.build_number is not None or arguments.release_date is not None
    if explicit_build and (arguments.build_number is None or arguments.release_date is None):
        parser.error("--build-number and --release-date must be supplied together")
    if explicit_build and arguments.zip_path is None:
        parser.error("explicit build metadata requires --zip")
    build = (
        LatestBuild(arguments.build_number, arguments.release_date)
        if explicit_build
        else fetch_latest_build()
    )
    zip_path = arguments.zip_path
    if zip_path is None:
        zip_path = Path(".cache") / f"eve-sde-{build.build_number}-jsonl.zip"
        if not zip_path.exists():
            download_sde(build, zip_path)
    elif str(build.build_number) not in zip_path.name:
        parser.error(
            f"--zip filename must identify SDE build {build.build_number}; refusing to label "
            "an ambiguous archive with different metadata"
        )
    stats = build_route_database(zip_path, arguments.output, build=build)
    print(
        f"built {arguments.output} from SDE {build.build_number}: "
        f"{stats.systems} systems, {stats.jumps} directed jumps, "
        f"{stats.stations} NPC stations, {stats.regions} regions, "
        f"{stats.gates} gate positions, {stats.type_groups} type groups, "
        f"{stats.item_types} item-to-group mappings"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())

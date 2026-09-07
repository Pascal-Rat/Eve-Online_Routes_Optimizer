from __future__ import annotations

import ast
from importlib.util import resolve_name
from pathlib import Path

import eve_courier_optimizer

PACKAGE = "eve_courier_optimizer"
SOURCE = Path(eve_courier_optimizer.__file__).parent
ALLOWED_DEPENDENCIES = {
    "domain": set(),
    "jsonio": set(),
    "routing": {"domain", "jsonio", "routing"},
    "verification": {"domain", "routing", "verification"},
    "eve": {"domain", "jsonio", "routing", "eve"},
    "optimization": {"domain", "jsonio", "routing", "verification", "optimization"},
    "application": {
        "domain",
        "jsonio",
        "routing",
        "verification",
        "eve",
        "optimization",
        "application",
    },
    "web": {"", "domain", "jsonio", "routing", "eve", "optimization", "application", "web"},
}


def imports(path: Path) -> list[str]:
    relative = path.relative_to(SOURCE).with_suffix("")
    package = PACKAGE + "." + ".".join(relative.parts[:-1])
    package = package.rstrip(".")
    dependencies: list[str] = []
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Import):
            dependencies.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            target = (
                resolve_name("." * node.level + (node.module or ""), package)
                if node.level
                else node.module or ""
            )
            children = []
            for alias in node.names:
                child = target + "." + alias.name
                if child.startswith(PACKAGE + "."):
                    local = SOURCE.joinpath(*child.removeprefix(PACKAGE + ".").split("."))
                    if local.with_suffix(".py").exists() or local.is_dir():
                        children.append(child)
            dependencies.extend(children or [target])
    return dependencies


def test_dependencies_follow_domain_and_interface_boundaries() -> None:
    violations = []
    for path in SOURCE.rglob("*.py"):
        relative = path.relative_to(SOURCE).with_suffix("")
        owner = relative.parts[0]
        if owner in {"__init__", "__main__", "cli"}:
            continue
        assert owner in ALLOWED_DEPENDENCIES, f"declare the dependency boundary for {relative}"
        for dependency in imports(path):
            if dependency != PACKAGE and not dependency.startswith(PACKAGE + "."):
                if owner in {"domain", "routing", "verification"} and dependency.startswith(
                    "ortools"
                ):
                    violations.append(f"{relative} depends on the solver: {dependency}")
                continue
            local = dependency.removeprefix(PACKAGE).lstrip(".")
            area = local.split(".")[0]
            if area not in ALLOWED_DEPENDENCIES[owner]:
                violations.append(f"{relative} imports {dependency}")
            if (
                owner in {"application", "web"}
                and area == "optimization"
                and local != "optimization"
            ):
                violations.append(f"{relative} bypasses the public optimizer")
            if (
                relative.parts[:2] == ("optimization", "models")
                and area == "optimization"
                and not local.startswith("optimization.models")
            ):
                violations.append(f"{relative} depends on search orchestration: {dependency}")
            if relative.parts[:2] == ("optimization", "search") and local in {
                "optimization",
                "optimization.optimizer",
                "optimization.proof_certificate",
            }:
                violations.append(f"{relative} depends on its caller: {dependency}")
            if str(relative) == "application/courier_trip" and area not in {"domain", "routing"}:
                violations.append(f"courier-trip rules depend on IO or orchestration: {dependency}")
    assert not violations, "\n".join(violations)

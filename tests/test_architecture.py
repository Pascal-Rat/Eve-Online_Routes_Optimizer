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
    "eve": {"domain", "jsonio", "routing", "eve"},
    "optimization": {"domain", "jsonio", "routing", "optimization"},
    "application": {"domain", "jsonio", "routing", "eve", "optimization", "application"},
    "desktop": {"", "domain", "jsonio", "routing", "eve", "optimization", "application", "desktop"},
}


def test_runtime_dependencies_follow_the_application_boundaries() -> None:
    violations = []
    for path in SOURCE.rglob("*.py"):
        relative = path.relative_to(SOURCE).with_suffix("")
        owner = relative.parts[0]
        if owner not in ALLOWED_DEPENDENCIES:
            continue
        module = PACKAGE + "." + ".".join(relative.parts)
        package = module.rpartition(".")[0]
        for node in ast.walk(ast.parse(path.read_text())):
            dependencies = []
            if isinstance(node, ast.Import):
                dependencies = [alias.name for alias in node.names]
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
                dependencies = children or [target]
            else:
                continue
            for dependency in dependencies:
                if dependency != PACKAGE and not dependency.startswith(PACKAGE + "."):
                    continue
                area = dependency.removeprefix(PACKAGE).lstrip(".").split(".")[0]
                if area not in ALLOWED_DEPENDENCIES[owner]:
                    violations.append(f"{relative}:{node.lineno} imports {dependency}")
                if (
                    owner in {"application", "desktop"}
                    and area == "optimization"
                    and dependency != PACKAGE + ".optimization"
                ):
                    violations.append(f"{relative}:{node.lineno} bypasses the optimizer API")
    assert not violations, "\n".join(violations)

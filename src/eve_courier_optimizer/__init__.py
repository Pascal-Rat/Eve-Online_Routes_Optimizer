"""EVE Online courier route optimizer."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("eve-courier-route-optimizer")
except PackageNotFoundError:  # pragma: no cover - only for uninstalled source trees
    __version__ = "0+unknown"

__all__ = ["__version__"]


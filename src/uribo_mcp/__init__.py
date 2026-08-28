"""uriboMCP package."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("uribo-mcp")
except PackageNotFoundError:  # pragma: no cover - source checkout
    __version__ = "0.1.0"

__all__ = ["__version__"]


"""ERPNext API bridge with Safe Mode. pyproject.toml owns the package version."""

from importlib.metadata import PackageNotFoundError, version

__all__ = ["__version__"]

try:
    __version__ = version("erpnext-mcp")
except PackageNotFoundError:
    __version__ = "0+unknown"

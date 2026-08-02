"""Geer local coding-agent pipeline."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("geer")
except PackageNotFoundError:  # pragma: no cover - source tree without installation
    __version__ = "0.0.0"

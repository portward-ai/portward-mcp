"""Portward free: a local MCP server that lets you choose what the AI sees from your own data."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("portward-free")
except PackageNotFoundError:  # a source tree that was never installed
    __version__ = "0+unknown"

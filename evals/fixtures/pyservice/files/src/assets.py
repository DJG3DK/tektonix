"""Serving the shop's static files."""

from pathlib import Path

ASSET_ROOT = Path(__file__).resolve().parent.parent / "assets"


def read_asset(name):
    """The bytes of the asset called `name`, which may include sub-folders."""
    return (ASSET_ROOT / name).read_bytes()

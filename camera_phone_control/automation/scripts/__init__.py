"""Explicit registry: CLI names never become arbitrary module or file imports."""

from . import open_mae, settings_demo

SCRIPTS = {"settings_demo": settings_demo.run, "open_mae": open_mae.run}


def available_scripts():
    return tuple(sorted(SCRIPTS))

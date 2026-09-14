"""Name -> source module, for the CLI's --source flag and `audio fetch`."""

from __future__ import annotations

from . import freesound, jamendo

SOURCES = {"freesound": freesound, "jamendo": jamendo}

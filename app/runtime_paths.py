"""Canonical host-local runtime paths for Nexus."""
from __future__ import annotations

import os
from pathlib import Path


# Renamed Nexus -> Panel -> Nexus. Each prior environment variable and
# the prior on-disk directory stay honoured so the running service keeps its
# state until the runtime cutover migrates the directory.
STATE_ROOT = Path(
    os.environ.get("NEXUS_STATE_DIR")
    or os.environ.get("PANEL_STATE_DIR")
    or os.environ.get("FLEET_PANEL_STATE_DIR")
    or Path.home() / ".local" / "state" / "nexus"
)
GENERATED_STATE_DIR = STATE_ROOT / "generated"
EVENTS_DB = STATE_ROOT / "events.db"
JOBS_HISTORY_DB = STATE_ROOT / "jobs_history.db"
CONTROL_STATE_DIR = STATE_ROOT / "control"
GEMINI_STATE_DIR = STATE_ROOT / "gemini"

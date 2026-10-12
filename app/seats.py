"""
Canonical seat registry — the ONE ordered list of relay seats, consumed by
seatboard.py (tiles), herospath.py (transcript dirs / palette), and
detail_context.py (run-dir resolution). Add a seat here once and it is
first-class everywhere; do not re-declare SEATS/SEAT_CLASS locally elsewhere.
"""
from __future__ import annotations

# (seat, node, color, label) — display order for the seat-availability strip
# and the source colors/labels for every other Tier-1 consumer.
SEAT_INFO: list[tuple[str, str, str, str]] = [
    ("worker3", "charlie", "amber", "Worker3"),
    ("worker1", "delta", "cyan", "Worker1"),
    ("worker2", "alpha", "green", "Worker2"),
    ("localworker", "charlie", "emerald", "Localworker"),
]

# Seat ids only, in the same order — what from-{seat}/{runs,transcripts}
# consumers (herospath, detail_context) iterate over.
ALL_SEATS: tuple[str, ...] = tuple(seat for seat, _node, _color, _label in SEAT_INFO)

# seat -> palette class (matches the scan log / seatboard colors).
SEAT_CLASS: dict[str, str] = {seat: f"seat-{seat}" for seat in ALL_SEATS}

# User-facing worker cards are node-oriented. The first id in each root tuple is
# the current Tower source; later ids are read-only compatibility roots so old
# run history remains visible through the migration.
# (card id, physical node, color, display label, run/transcript source ids)
CARD_INFO: list[tuple[str, str, str, str, tuple[str, ...]]] = [
    ("charlie", "charlie", "amber", "charlie", ("charlie", "worker3")),
    ("delta", "delta", "cyan", "delta", ("delta", "worker1")),
    ("alpha", "alpha", "green", "alpha", ("alpha", "worker2")),
    ("localworker", "charlie", "emerald", "Localworker", ("localworker",)),
]

RUN_SOURCE_TO_CARD: dict[str, str] = {
    source: card
    for card, _node, _color, _label, sources in CARD_INFO
    for source in sources
}
RUN_SOURCE_IDS: tuple[str, ...] = tuple(RUN_SOURCE_TO_CARD)

# New node roots reuse the established historical color classes in Worker
# Activity; the compatibility roots retain their existing classes above.
SEAT_CLASS.update({
    "charlie": "seat-worker3",
    "delta": "seat-worker1",
    "alpha": "seat-worker2",
    "localworker": "seat-localworker",
})

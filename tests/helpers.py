"""Shared test helpers: a data dir on the tracks layout (SPEC §8.6)."""

from pathlib import Path

from app.datastore.layout import DataLayout
from app.tracks.registry import TrackRegistry

TRACK = "rfic-default"


def make_data(root: Path, *tracks: str) -> Path:
    """Registry + directory tree for `tracks` (default: one track `rfic-default`); the first one is active. -> root."""
    root = Path(root)
    tracks = tracks or (TRACK,)
    DataLayout(root).ensure()
    reg = TrackRegistry(root)
    reg.seed(tracks[0], tracks[0].replace("-", " ").title(), "rfic")
    for t in tracks[1:]:
        reg.create(t.replace("-", " ").title(), "rfic", track_id=t)
    if len(tracks) > 1:
        reg.activate(tracks[0])
    for t in tracks:
        DataLayout(root, t).ensure()
    return root

"""Temporary copy of data/ for acceptance runs (M6c): never exercise the real learner data again.

    from scripts.tempdata import temp_data
    with temp_data() as root:   # BLAHAJ_DATA_DIR points at the copy for the duration
        ...
The copy keeps settings.json (API key) and the node/framework/profile state; it skips build scratch, spikes, the library and the voice models
(point BLAHAJ_MODELS_DIR at the real download instead).
"""

import contextlib
import os
import shutil
import tempfile
from pathlib import Path

from app.settings import data_dir

SKIP = (".git", "builds", "_spike_archive", "spike", "library", "models", "*.db-wal", "*.db-shm")  # models: ~520 MB, use BLAHAJ_MODELS_DIR


@contextlib.contextmanager
def temp_data(skip: tuple[str, ...] = SKIP):
    src = data_dir()
    tmp = Path(tempfile.mkdtemp(prefix="blahaj-data-"))
    root = tmp / "data"
    shutil.copytree(src, root, ignore=shutil.ignore_patterns(*skip))
    old = os.environ.get("BLAHAJ_DATA_DIR")
    os.environ["BLAHAJ_DATA_DIR"] = str(root)
    try:
        yield root
    finally:
        if old is None:
            os.environ.pop("BLAHAJ_DATA_DIR", None)
        else:
            os.environ["BLAHAJ_DATA_DIR"] = old
        shutil.rmtree(tmp, ignore_errors=True)

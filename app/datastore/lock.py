"""One process-wide lock for the append-only logs (events.jsonl, misconceptions.jsonl).

Appends take it for the single write; the only operations that REWRITE a log (the tracks migration and track delete/reset, which
remove a track's lines) hold it for the whole read-filter-replace, so a concurrent append can never be lost.
"""

import threading

log_lock = threading.RLock()

"""Graph API data (SPEC §11 Graph screen): the active track's nodes with their parent edges, status, domain and tier."""

import json
from pathlib import Path

from app.builder.context import cache_rows
from app.lifecycle.outcomes import read_outcomes


def graph_view(root: Path, track: str) -> dict:
    """{track, nodes, edges}. A node is {id, dir, title, kind, domain, tier, status, provisional, outcome, size_est_min, subskills, parents};
    an edge {source, target} runs parent -> child (remediation and rescope-replacement children point at the node they came from).
    Edges to a parent id that is not a node of this track are dropped. Everything is a read of the derived cache and the node sessions."""
    rows = cache_rows(root, track, "SELECT id, dir, title, kind, domain, tier, status, provisional, size_est_min, subskills, parents FROM nodes WHERE track = ? ORDER BY id")
    outcomes = read_outcomes(root, track)
    ids = {r["id"] for r in rows}
    nodes, edges = [], []
    for r in rows:
        parents = [p for p in json.loads(r["parents"]) if isinstance(p, int) and not isinstance(p, bool) and p in ids and p != r["id"]]
        out = outcomes.get(r["id"])
        nodes.append({
            "id": r["id"], "dir": r["dir"], "title": r["title"], "kind": r["kind"], "domain": r["domain"], "tier": r["tier"], "status": r["status"] or "",
            "provisional": bool(r["provisional"]), "outcome": out.label if out else None, "size_est_min": r["size_est_min"],
            "subskills": json.loads(r["subskills"]), "parents": parents,
        })  # fmt: skip
        edges += [{"source": p, "target": r["id"]} for p in parents]
    return {"track": track, "nodes": nodes, "edges": edges}

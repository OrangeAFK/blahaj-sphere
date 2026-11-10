"""Read/write `node.md` (YAML frontmatter + Markdown brief, SPEC §5.1)."""

import re
from pathlib import Path

import yaml

_FM = re.compile(r"\A---[ \t]*\r?\n(.*?)\r?\n---[ \t]*(?:\r?\n|\Z)(.*)\Z", re.S)


class NodeFileError(ValueError):
    pass


def split_frontmatter(text: str) -> tuple[str, str]:
    m = _FM.match(text.lstrip("﻿"))
    if not m:
        raise NodeFileError("node.md must start with YAML frontmatter between '---' lines")
    return m.group(1), m.group(2)


def read_node_md(path: Path) -> tuple[dict, str]:
    """-> (frontmatter, body)."""
    raw, body = split_frontmatter(path.read_text(encoding="utf-8"))
    try:
        fm = yaml.safe_load(raw)
    except yaml.YAMLError as e:
        raise NodeFileError(f"node.md frontmatter is not valid YAML: {e}") from None
    if not isinstance(fm, dict):
        raise NodeFileError("node.md frontmatter must be a YAML mapping")
    return fm, body


def set_frontmatter_scalar(path: Path, key: str, value) -> None:
    """Set one top-level scalar in place, keeping the rest of the frontmatter text (comments, flow lists) as written."""
    text = path.read_text(encoding="utf-8")
    raw, body = split_frontmatter(text)
    line = f"{key}: {yaml.safe_dump(value, default_flow_style=True).strip().removesuffix('...').strip()}"
    pat = re.compile(rf"^{re.escape(key)}:.*$", re.M)
    raw = pat.sub(lambda _m: line, raw, count=1) if pat.search(raw) else raw + "\n" + line
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(f"---\n{raw}\n---\n{body}")

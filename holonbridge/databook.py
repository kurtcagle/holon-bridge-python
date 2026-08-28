"""DataBook assembly and extraction.

A DataBook is a Markdown file that is simultaneously readable prose, a typed
data container, and a self-describing artefact. Frontmatter is plain YAML
delimited by ``---`` and nothing else. Blocks are fenced and carry
``<!-- databook:id: ... -->`` / ``<!-- databook:label: ... -->`` comments
immediately above the fence.

The bridge produces DataBooks (``get_holon``) and consumes them (ingestion),
so both directions live here.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

try:  # PyYAML is optional; a minimal emitter covers the common case.
    import yaml
except ImportError:  # pragma: no cover
    yaml = None  # type: ignore[assignment]

_FRONTMATTER = re.compile(r"\A---\r?\n(.*?)\r?\n---\r?\n", re.DOTALL)
_BLOCK = re.compile(
    r"(?:<!--\s*databook:id:\s*(?P<id>[^\s>]+)\s*-->\s*\n)?"
    r"(?:<!--\s*databook:label:\s*(?P<label>[^\n]*?)\s*-->\s*\n)?"
    r"```(?P<lang>[A-Za-z0-9_+-]*)\r?\n(?P<body>.*?)\r?\n```",
    re.DOTALL,
)


@dataclass
class Block:
    """One fenced block inside a DataBook."""

    lang: str
    body: str
    id: str | None = None
    label: str | None = None

    def render(self) -> str:
        parts: list[str] = []
        if self.id:
            parts.append(f"<!-- databook:id: {self.id} -->")
        if self.label:
            parts.append(f"<!-- databook:label: {self.label} -->")
        parts.append(f"```{self.lang}\n{self.body.rstrip()}\n```")
        return "\n".join(parts)


@dataclass
class DataBook:
    """Frontmatter, prose, and typed blocks."""

    frontmatter: dict[str, Any] = field(default_factory=dict)
    body: str = ""
    blocks: list[Block] = field(default_factory=list)

    # --- rendering -------------------------------------------------------------

    def render(self) -> str:
        chunks = [f"---\n{_dump_yaml(self.frontmatter).rstrip()}\n---\n"]
        if self.body.strip():
            chunks.append(self.body.strip() + "\n")
        for block in self.blocks:
            chunks.append(block.render() + "\n")
        return "\n".join(chunks)

    # --- extraction ------------------------------------------------------------

    def block(self, block_id: str) -> Block:
        for block in self.blocks:
            if block.id == block_id:
                return block
        raise KeyError(f"no block with id {block_id!r}")

    def blocks_of(self, lang: str) -> list[Block]:
        return [b for b in self.blocks if b.lang == lang]

    def primary_graph_block(self) -> Block:
        """The block ingestion lands: the first ``turtle``, ``turtle12``, or
        ``json-ld`` fence. Raised rather than silently skipping to a
        non-RDF block or picking one that happens to come first for the
        wrong reason — a DataBook with no RDF payload is not something
        ingestion can act on.

        CHANGED 2026-08-28: added ``json-ld`` alongside ``turtle``/
        ``turtle12`` — create_holon and create_message both accept either
        serialisation. A matched ``json-ld`` block is converted to Turtle
        by the caller (see ``holonbridge.turtle.from_json_ld``) before it
        reaches the single GSP write path, which stays ``text/turtle``
        unconditionally either way.
        """
        for block in self.blocks:
            if block.lang in ("turtle", "turtle12", "json-ld"):
                return block
        raise ValueError("DataBook has no turtle, turtle12, or json-ld block to ingest")

    @property
    def named_graph(self) -> str | None:
        graph = self.frontmatter.get("graph")
        if isinstance(graph, dict):
            value = graph.get("named_graph")
            return str(value) if value else None
        return None

    @classmethod
    def parse(cls, text: str) -> "DataBook":
        frontmatter: dict[str, Any] = {}
        remainder = text

        match = _FRONTMATTER.match(text)
        if match:
            frontmatter = _load_yaml(match.group(1))
            remainder = text[match.end() :]

        blocks = [
            Block(
                lang=(m.group("lang") or "").strip(),
                body=m.group("body"),
                id=m.group("id"),
                label=m.group("label"),
            )
            for m in _BLOCK.finditer(remainder)
        ]
        prose = _BLOCK.sub("", remainder).strip()
        return cls(frontmatter=frontmatter, body=prose, blocks=blocks)


# --- YAML shims ---------------------------------------------------------------


def _dump_yaml(data: dict[str, Any], indent: int = 0) -> str:
    if yaml is not None:
        return yaml.safe_dump(data, sort_keys=False, allow_unicode=True)
    lines: list[str] = []
    pad = "  " * indent
    for key, value in data.items():
        if isinstance(value, dict):
            lines.append(f"{pad}{key}:")
            lines.append(_dump_yaml(value, indent + 1).rstrip())
        elif isinstance(value, list):
            lines.append(f"{pad}{key}:")
            lines.extend(f"{pad}  - {item}" for item in value)
        else:
            lines.append(f"{pad}{key}: {value}")
    return "\n".join(lines) + "\n"


def _load_yaml(raw: str) -> dict[str, Any]:
    if yaml is not None:
        loaded = yaml.safe_load(raw)
        return loaded if isinstance(loaded, dict) else {}
    # Flat fallback parser: good enough to read `id:` and one nested level.
    out: dict[str, Any] = {}
    current: dict[str, Any] | None = None
    for line in raw.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        value = value.strip()
        if line.startswith(("  ", "\t")) and current is not None:
            current[key.strip()] = value
        elif value:
            out[key.strip()] = value
            current = None
        else:
            current = {}
            out[key.strip()] = current
    return out

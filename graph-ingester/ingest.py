"""Vault -> Neo4j ingester.

Walks /data/vault recursively, parses YAML frontmatter + wikilinks, and
upserts nodes + edges into Neo4j via idempotent MERGE queries. Safe to
run repeatedly; node labels reflect folder structure, edge labels
reflect folder-pairing + "MENTIONS" for generic prose wikilinks.
"""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path
from typing import Iterable

import frontmatter
from neo4j import GraphDatabase, Driver

VAULT_PATH = Path(os.environ.get("OBSIDIAN_VAULT_PATH", "/data/vault"))
NEO4J_URI = os.environ["NEO4J_URI"]
NEO4J_USER = os.environ.get("NEO4J_USER", "neo4j")
NEO4J_PASSWORD = os.environ["NEO4J_PASSWORD"]

# Folder name (lowercased, trimmed of leading numeric prefix like "02 - ")
# to Neo4j label.
FOLDER_LABEL = {
    "people": "Person",
    "companies": "Company",
    "properties": "Property",
    "leads": "Lead",
    "tasks": "Task",
    "projects": "Project",
    "01 - daily": "Daily",
    "daily": "Daily",
    "daily logs": "DailyLog",
    "dashboards": "Dashboard",
    "claude memory": "Memory",
    "templates": "Template",
    "documents": "Document",
    "docs": "Doc",
}

# Wikilink regex: [[target]] or [[target|alias]]. Alias is display-only.
WIKILINK_RE = re.compile(r"\[\[([^\[\]\|]+?)(?:\|[^\[\]]*)?\]\]")


def folder_of(path: Path) -> str:
    """Top-level folder under the vault, or empty string for root files."""
    rel = path.relative_to(VAULT_PATH)
    parts = rel.parts
    return parts[0] if len(parts) > 1 else ""


def label_for(folder: str, fallback: str = "Note") -> str:
    key = folder.lower().lstrip()
    # strip a leading "NN - " numeric prefix if present (so "02 - Projects" -> "projects")
    m = re.match(r"^\d+\s*-\s*(.*)$", key)
    if m:
        key = m.group(1)
    return FOLDER_LABEL.get(key, fallback)


def node_name(path: Path) -> str:
    """Filename without .md extension, used as canonical node identity."""
    return path.stem


def walk_vault() -> Iterable[Path]:
    for p in VAULT_PATH.rglob("*.md"):
        # skip hidden / .obsidian / .trash dirs
        if any(part.startswith(".") for part in p.relative_to(VAULT_PATH).parts):
            continue
        yield p


def extract_wikilinks(content: str) -> list[str]:
    """Return de-duplicated wikilink targets (filename-only, no alias)."""
    seen: dict[str, None] = {}
    for target in WIKILINK_RE.findall(content):
        # Obsidian may include "folder/name" or "name#heading" - normalise to basename.
        target = target.split("#", 1)[0].split("^", 1)[0].strip()
        target = target.rsplit("/", 1)[-1]
        if target and target not in seen:
            seen[target] = None
    return list(seen)


def upsert_node(tx, name: str, label: str, props: dict) -> None:
    """Idempotent MERGE on name only, then assign label.

    Matching on name alone (no label in pattern) is critical: otherwise a
    previously-created Stub node (from a wikilink referencing this entity
    before its own file was walked) would NOT match `MERGE (n:Label {name})`,
    and we'd end up with two distinct nodes for the same entity. Two-phase
    approach instead: match-by-name, then promote via SET label + REMOVE Stub.
    """
    clean = {k: v for k, v in props.items() if v is not None and not isinstance(v, (dict, list))}
    tx.run(
        f"""
        MERGE (n {{name: $name}})
        SET n:{label}
        REMOVE n:Stub
        SET n += $props
        """,
        name=name, props=clean,
    )


def upsert_edge(tx, src_name: str, tgt_name: str) -> None:
    """Edge from an already-created source to a target node.

    Source is guaranteed to exist because ingest() calls upsert_node first
    for every file. Target is matched by name only; if no node with that
    name exists yet, create a Stub. A later ingest run (or later file walk)
    will promote the Stub to a proper label via upsert_node's SET/REMOVE.
    """
    tx.run(
        """
        MATCH (src {name: $src_name})
        MERGE (tgt {name: $tgt_name})
        ON CREATE SET tgt:Stub
        MERGE (src)-[:MENTIONS]->(tgt)
        """,
        src_name=src_name, tgt_name=tgt_name,
    )


def ingest(driver: Driver) -> tuple[int, int]:
    """Two-pass walk: all nodes first (so labels land before any wikilink
    would auto-create a Stub by that name), then edges. Materialises the
    file list once so pass 2 doesn't re-read."""
    files = 0
    edges = 0
    # Pass 1: materialise file list + upsert labeled nodes.
    parsed: list[tuple[str, str, str, dict]] = []  # (name, label, content, props)
    for path in walk_vault():
        try:
            post = frontmatter.loads(path.read_text(encoding="utf-8", errors="replace"))
        except Exception as e:
            print(f"[skip] {path}: {e}", file=sys.stderr)
            continue
        name = node_name(path)
        folder = folder_of(path)
        label = label_for(folder)
        props = {
            "path": str(path.relative_to(VAULT_PATH)),
            "folder": folder,
            **{k: v for k, v in post.metadata.items() if v is not None},
        }
        parsed.append((name, label, post.content, props))

    with driver.session() as session:
        for name, label, _content, props in parsed:
            session.execute_write(upsert_node, name, label, props)
            files += 1
        # Pass 2: edges. Source node is guaranteed to exist now.
        for name, _label, content, _props in parsed:
            for target in extract_wikilinks(content):
                session.execute_write(upsert_edge, name, target)
                edges += 1
    return files, edges


def main() -> None:
    driver = GraphDatabase.driver(NEO4J_URI, auth=(NEO4J_USER, NEO4J_PASSWORD))
    try:
        driver.verify_connectivity()
        files, edges = ingest(driver)
        print(f"[ingest] {files} nodes, {edges} edges from {VAULT_PATH}")
    finally:
        driver.close()


if __name__ == "__main__":
    main()

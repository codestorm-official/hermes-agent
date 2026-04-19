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


# YAML keys that never reference another entity - don't scan their values
# for edges. Everything else is fair game (assignee, area, eigentuemer,
# hausmeister, rolle, firma, mieter, kontakt, portfolio, ...).
SCALAR_YAML_KEYS = {
    'status', 'priority', 'deadline', 'date', 'type', 'id', 'mfiles_id',
    'email', 'url', 'phone', 'path', 'folder', 'tags', 'aliases',
    'created', 'modified', 'updated', 'version', 'draft', 'cssclass',
    'title', 'description', 'summary', 'icon', 'color', 'location',
    'standort', 'adresse',
}


def _unwrap_wikilink(v: str) -> str:
    """Hermes writes many YAML refs as proper wikilinks
    (eigentuemer: '[[XYZ GbR]]') so Obsidian still tracks them in the
    native graph view. Strip the brackets + alias before matching."""
    s = v.strip()
    if s.startswith('[[') and s.endswith(']]'):
        s = s[2:-2]
    # handle [[name|alias]] -> name
    if '|' in s:
        s = s.split('|', 1)[0]
    # handle [[name#heading]] -> name
    s = s.split('#', 1)[0].split('^', 1)[0]
    return s.strip()


def yaml_edges(tx, src_name: str, props: dict) -> int:
    """Scan YAML props; for every string value that matches an existing
    node's name, create a REFERS_TO edge tagged with the YAML key.

    Uses MATCH (not MERGE) for the target so we never create stubs from
    YAML - only materialise edges when both ends are real nodes. That
    keeps the graph honest: a YAML value `eigentuemer: "ACME GbR"` only
    produces an edge if we actually have a Companies/ACME GbR.md file.
    """
    made = 0
    for key, raw in props.items():
        if key in SCALAR_YAML_KEYS:
            continue
        values = raw if isinstance(raw, list) else [raw]
        for v in values:
            if not isinstance(v, str):
                continue
            tgt = _unwrap_wikilink(v)
            if not tgt or tgt == src_name:
                continue
            res = tx.run(
                """
                MATCH (src {name: $src_name})
                MATCH (tgt {name: $tgt_name})
                WHERE id(src) <> id(tgt)
                MERGE (src)-[r:REFERS_TO {via: $via}]->(tgt)
                RETURN count(r) AS n
                """,
                src_name=src_name, tgt_name=tgt, via=key,
            ).single()
            if res and res.get('n', 0) > 0:
                made += 1
    return made


def ingest(driver: Driver) -> tuple[int, int, int]:
    """Three-pass walk:
      1. Upsert all labeled nodes (so wikilink targets match existing
         labeled nodes instead of spawning Stubs).
      2. MENTIONS edges from [[wikilinks]] in the content.
      3. REFERS_TO edges from YAML props whose string values match
         existing node names (promotes "eigentuemer: X" style
         references to graph edges).
    """
    files = 0
    mention_edges = 0
    refer_edges = 0
    parsed: list[tuple[str, str, str, dict]] = []
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
        for name, _label, content, _props in parsed:
            for target in extract_wikilinks(content):
                session.execute_write(upsert_edge, name, target)
                mention_edges += 1
        # Pass 3 runs AFTER all wikilink edges so YAML-derived
        # REFERS_TO edges see the full node set.
        for name, _label, _content, props in parsed:
            refer_edges += session.execute_write(yaml_edges, name, props)
    return files, mention_edges, refer_edges


def record_run(driver: Driver, files: int, mentions: int, refers: int, duration_ms: int, error: str | None = None) -> None:
    """Writes a singleton IngestRun node so Mission Control can show
    'last ingest N seconds ago' without SSH'ing to the container."""
    with driver.session() as s:
        s.run(
            """
            MERGE (r:IngestRun {key: 'latest'})
            SET r.at = datetime(),
                r.files = $files,
                r.mentions = $mentions,
                r.refers = $refers,
                r.duration_ms = $duration_ms,
                r.error = $error
            """,
            files=files, mentions=mentions, refers=refers,
            duration_ms=duration_ms, error=error,
        )


def main() -> None:
    import time
    driver = GraphDatabase.driver(NEO4J_URI, auth=(NEO4J_USER, NEO4J_PASSWORD))
    t0 = time.time()
    err = None
    files = mentions = refers = 0
    try:
        driver.verify_connectivity()
        files, mentions, refers = ingest(driver)
        print(
            f"[ingest] {files} nodes, {mentions} MENTIONS, "
            f"{refers} REFERS_TO from {VAULT_PATH}"
        )
    except Exception as e:
        err = str(e)
        print(f"[ingest-error] {err}", file=sys.stderr)
        raise
    finally:
        duration_ms = int((time.time() - t0) * 1000)
        try:
            record_run(driver, files, mentions, refers, duration_ms, err)
        except Exception as log_err:
            print(f"[ingest-record-failed] {log_err}", file=sys.stderr)
        driver.close()


if __name__ == "__main__":
    main()

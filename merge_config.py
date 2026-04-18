"""Merge a git-tracked seed config into the persisted config.yaml.

Seed wins for every key except model.default and model.provider, which the
dashboard and `hermes model` / codex_login.py (respectively) own at runtime.
The merge runs once per container boot, right before the admin server starts.

Usage:
    python merge_config.py <seed_yaml> <target_yaml>
"""

import sys
from pathlib import Path

import yaml


def merge(seed_path: Path, target_path: Path) -> None:
    seed = yaml.safe_load(seed_path.read_text()) or {}
    existing: dict = {}
    if target_path.exists():
        existing = yaml.safe_load(target_path.read_text()) or {}

    merged = dict(seed)

    # Preserve runtime-owned fields from the existing file if set.
    # The model key can be either a dict ({default, provider, ...}) or a
    # bare string (when someone ran `hermes config set model <name>`).
    # Normalise to a dict before reading fields.
    existing_model_raw = existing.get("model")
    if isinstance(existing_model_raw, str):
        existing_default = existing_model_raw.strip() or None
        existing_provider = None
    elif isinstance(existing_model_raw, dict):
        existing_default = existing_model_raw.get("default")
        existing_provider = existing_model_raw.get("provider")
    else:
        existing_default = None
        existing_provider = None

    if existing_default or existing_provider:
        seed_model_raw = merged.get("model")
        merged_model = dict(seed_model_raw) if isinstance(seed_model_raw, dict) else {}
        if existing_default:
            merged_model["default"] = existing_default
        if existing_provider:
            merged_model["provider"] = existing_provider
        merged["model"] = merged_model

    target_path.parent.mkdir(parents=True, exist_ok=True)
    target_path.write_text(yaml.safe_dump(merged, sort_keys=False))


if __name__ == "__main__":
    if len(sys.argv) != 3:
        print(__doc__, file=sys.stderr)
        sys.exit(2)
    merge(Path(sys.argv[1]), Path(sys.argv[2]))

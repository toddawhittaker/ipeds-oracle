"""docs/SCHEMA.md and docs/DATASET.md, exposed as MCP resources.

This is not optional polish. The tools hand a caller the tables; these two
documents are the rules for reading them, and without the rules the numbers come
out wrong in ways nobody notices. The app's own system prompt is built from
SCHEMA.md for exactly that reason (app/prompt.py), and the award-level rollup
trap still shipped a confidently wrong headline once — a caller who gets the
tables without the rules will reproduce that bug, and it will not be visible
from this side.

A client that ignores resources is no worse off than one calling the tools
today; a client that reads them starts where the chat agent starts.
"""
from __future__ import annotations

from pathlib import Path

from app.config import get_settings

# `schema_md_path` is a setting because the file ships read-only inside the image
# at a fixed place (see app/startup_checks.py's one deliberate opt-out). DATASET.md
# sits beside it in the same docs/ directory and is derived from it rather than
# adding a second setting that could only ever disagree with the first.
SCHEMA_URI = "ipeds://docs/SCHEMA.md"
DATASET_URI = "ipeds://docs/DATASET.md"


def _paths() -> dict[str, Path]:
    schema = get_settings().schema_md_path
    return {SCHEMA_URI: schema, DATASET_URI: schema.parent / "DATASET.md"}


# uri -> (name, title, description), in the order a client should read them.
CATALOG: dict[str, tuple[str, str, str]] = {
    SCHEMA_URI: (
        "schema-guide", "IPEDS schema guide",
        "How this database is laid out and how to query it correctly: the "
        "unified table families, the join keys, the discovery queries, and the "
        "aggregation rules (award-level nesting, CIP rollups) that produce a "
        "wrong total when ignored. Read this before writing SQL."),
    DATASET_URI: (
        "dataset-guide", "IPEDS dataset guide",
        "What is actually in this deployment's data: the surveys and collection "
        "years loaded, how IPEDS labels and codes work, and the known gaps and "
        "caveats per survey."),
}


def read_resource(uri: str) -> str:
    """The document's text.

    Raises FileNotFoundError for an unknown URI, and lets a genuine read error
    propagate: a caller asking for the rules is better served by an error than
    by silence it will mistake for "there are no rules". This differs on purpose
    from app/prompt.py's `_schema_md`, which swallows the same failure because a
    missing file must not take down every chat turn.
    """
    path = _paths().get(uri)
    if path is None:
        raise FileNotFoundError(f"unknown resource URI: {uri}")
    return path.read_text(encoding="utf-8")

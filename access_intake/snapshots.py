"""Publish immutable export pairs through one atomically replaced manifest.

Durability uses POSIX file and directory fsync on a local filesystem. Readers
resolve the manifest once and keep that generation for both files.
"""

import hashlib
import json
import os
import re
from pathlib import Path
from typing import Callable
from uuid import uuid4


def _write(path: Path, data: bytes):
    with path.open("xb") as output:
        output.write(data)
        output.flush()
        os.fsync(output.fileno())


def _sync(directory: Path):
    descriptor = os.open(directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def export_generation(repository, directory, *, fault_hook: Callable | None = None):
    fault = fault_hook or (lambda stage, identity: None)
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    generation = uuid4().hex
    target = directory / generation
    target.mkdir()
    snapshot = repository.snapshot()
    files = {
        "extractions.jsonl": "".join(json.dumps(row, ensure_ascii=False, sort_keys=True, allow_nan=False) + "\n"
                                     for row in snapshot["extractions"]).encode("utf-8"),
        "queue.json": (json.dumps(snapshot["queue"], ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode("utf-8"),
    }
    for name, data in files.items():
        _write(target / name, data)
    _sync(target)
    _sync(directory)
    fault("after_generation", generation)
    manifest = {"format_version": 1, "generation": generation,
                "sha256": {name: hashlib.sha256(data).hexdigest() for name, data in files.items()}}
    temporary = directory / f".manifest-{generation}.json"
    _write(temporary, (json.dumps(manifest, sort_keys=True) + "\n").encode("utf-8"))
    os.replace(temporary, directory / "manifest.json")
    _sync(directory)
    fault("after_manifest", generation)
    return manifest


def read_snapshot(directory, *, fault_hook: Callable | None = None):
    fault = fault_hook or (lambda stage, identity: None)
    directory = Path(directory)
    manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
    if (not isinstance(manifest, dict) or manifest.get("format_version") != 1
            or not isinstance(manifest.get("generation"), str)
            or not re.fullmatch(r"[0-9a-f]{32}", manifest["generation"])
            or not isinstance(manifest.get("sha256"), dict)):
        raise ValueError("Invalid snapshot manifest")
    generation = manifest["generation"]
    contents = {}
    for name in ("extractions.jsonl", "queue.json"):
        raw = (directory / generation / name).read_bytes()
        if hashlib.sha256(raw).hexdigest() != manifest["sha256"].get(name):
            raise ValueError(f"Snapshot checksum mismatch for {name}")
        contents[name] = raw.decode("utf-8")
        if name == "extractions.jsonl":
            fault("after_extractions_read", generation)
    return {
        "extractions": [json.loads(line) for line in contents["extractions.jsonl"].splitlines()],
        "queue": json.loads(contents["queue.json"]),
    }

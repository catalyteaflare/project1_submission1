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


def _read_files(directory, *, manifest=None, fault_hook: Callable | None = None):
    fault = fault_hook or (lambda stage, identity: None)
    directory = Path(directory)
    if manifest is None:
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
        contents[name] = raw
        if name == "extractions.jsonl":
            fault("after_extractions_read", generation)
    return manifest, contents


def read_snapshot(directory, *, fault_hook: Callable | None = None):
    _, contents = _read_files(directory, fault_hook=fault_hook)
    return {"extractions": [json.loads(line) for line in contents["extractions.jsonl"].decode("utf-8").splitlines()],
            "queue": json.loads(contents["queue.json"].decode("utf-8"))}


def copy_generation(directory, destination, *, manifest=None):
    """Copy the exact verified bytes of one generation for offline handover.

    Supplying the export's manifest pins that export even if another operator
    publishes meanwhile. Fixed filenames are replaced individually; concurrent
    application readers must continue to use read_snapshot and the manifest.
    """
    manifest, contents = _read_files(directory, manifest=manifest)
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=True)
    contents["snapshot-manifest.json"] = (json.dumps(manifest, sort_keys=True) + "\n").encode("utf-8")
    staged = []
    try:
        for name, data in contents.items():
            temporary = destination / f".{name}-{uuid4().hex}.tmp"
            staged.append((temporary, destination / name))
            _write(temporary, data)
        for temporary, target in staged:
            os.replace(temporary, target)
        _sync(destination)
    finally:
        for temporary, _ in staged:
            temporary.unlink(missing_ok=True)
    return manifest

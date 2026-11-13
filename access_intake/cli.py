"""JSON operator commands over a shared, explicitly selected state database."""

import argparse
import json
import math
import sqlite3
import sys
from dataclasses import replace
from pathlib import Path

from .inputs import batch_paths
from .mock_config import DEFAULT_MOCK_MAP, MockConfiguration, provider_for_settings
from .policy import POLICY_VERSION
from .prompt import PROMPT_VERSION, SYSTEM_PROMPT
from .repository import ProcessingSettings, Repository
from .scheduler import Scheduler
from .validation import SCHEMA


def _parser():
    parser = argparse.ArgumentParser(prog="python3 -m access_intake")
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("intake", "queue", "progress", "history", "retry", "resume", "refresh", "export"):
        command = commands.add_parser(name)
        command.add_argument("--state", type=Path, required=True)
        if name in ("intake", "refresh"):
            command.add_argument("--provider", choices=("mock", "live"), default="mock" if name == "intake" else None)
            command.add_argument("--mock-map", type=Path)
            command.add_argument("--policy-version")
            command.add_argument("--prompt-file", type=Path)
        if name == "intake":
            command.add_argument("paths", nargs="+", type=Path)
        if name in ("intake", "retry", "resume", "refresh"):
            command.add_argument("--workers", type=int, default=4)
            command.add_argument("--timeout", type=float, default=60)
            command.add_argument("--max-calls", type=int, default=3)
            command.add_argument("--enqueue-only", action="store_true")
        if name in ("queue", "progress", "history"):
            command.add_argument("--limit", type=int, default=100)
            command.add_argument("--offset", type=int, default=0)
        if name == "history":
            command.add_argument("--document", required=True)
        if name == "retry":
            command.add_argument("--attempt", required=True)
        if name == "export":
            command.add_argument("--out", type=Path, required=True)
    return parser


def _settings(args):
    if args.provider == "live":
        from .http_provider import HttpProvider
        settings = ProcessingSettings.from_provider(HttpProvider.from_environment())
    else:
        settings = ProcessingSettings()
    if args.policy_version is not None:
        settings = replace(settings, policy_version=args.policy_version)
    if args.prompt_file is not None:
        settings = replace(settings, prompt=args.prompt_file.read_text(encoding="utf-8"))
    return settings


def _refresh(repo, args):
    explicit = _settings(args) if args.provider is not None or args.mock_map is not None else None
    mapping = MockConfiguration(args.mock_map or DEFAULT_MOCK_MAP) if args.mock_map or args.provider == "mock" else None
    prompt = args.prompt_file.read_text(encoding="utf-8") if args.prompt_file else None
    after = ""
    while True:
        rows = repo.latest_inputs(after_document=after)
        if not rows:
            break
        for row in rows:
            settings = explicit or ProcessingSettings(**row["settings"])
            if explicit is None and args.policy_version is None and prompt is None:
                settings = replace(settings, prompt=SYSTEM_PROMPT, schema=SCHEMA,
                                   extraction_version=PROMPT_VERSION, policy_version=POLICY_VERSION)
            if args.policy_version is not None:
                settings = replace(settings, policy_version=args.policy_version)
            if prompt is not None:
                settings = replace(settings, prompt=prompt)
            if mapping is not None:
                if settings.provider != "mock":
                    raise ValueError("--mock-map requires a mock provider")
                settings = mapping.settings_for(row["document_id"], row["source_bytes"], settings)
            repo.schedule_source(row["document_id"], row["source_bytes"], settings=settings,
                                 expected_latest=row["attempt_id"])
        after = rows[-1]["document_id"]


def main(argv=None, *, provider_factory=None, hook=None, scheduler_options=None):
    args = _parser().parse_args(argv)
    try:
        if hasattr(args, "limit") and (not 1 <= args.limit <= 1000 or args.offset < 0):
            raise ValueError("Use --limit 1–1000 and a nonnegative --offset")
        if hasattr(args, "workers") and (not 1 <= args.workers <= 32 or not math.isfinite(args.timeout)
                                         or args.timeout <= 0 or not 1 <= args.max_calls <= 3):
            raise ValueError("Use 1–32 workers, a finite positive timeout and 1–3 provider calls")
        if args.command != "intake" and not args.state.is_file():
            raise ValueError("State database does not exist; select the intake state path")
        with Repository(args.state) as repo:
            if args.command == "queue":
                result = repo.queue_page(limit=args.limit, offset=args.offset)
            elif args.command == "progress":
                result = repo.progress_page(limit=args.limit, offset=args.offset)
            elif args.command == "history":
                result = repo.history_page(args.document, limit=args.limit, offset=args.offset)
            elif args.command == "export":
                from .snapshots import export_generation
                result = export_generation(repo, args.out)
            else:
                if args.command == "intake":
                    paths = batch_paths(p for path in args.paths for p in (sorted(path.glob("*.json")) if path.is_dir() else [path]))
                    if not paths:
                        raise ValueError("No JSON exports found in the selected input")
                    settings = _settings(args)
                    mapping = MockConfiguration(args.mock_map or DEFAULT_MOCK_MAP) if args.provider == "mock" else None
                    for path in paths:
                        raw = path.read_bytes()
                        producing = mapping.settings_for(path, raw, settings) if mapping is not None else settings
                        repo.schedule_source(path.name, raw, settings=producing)
                elif args.command == "retry":
                    repo.enqueue_retry(args.attempt)
                elif args.command == "refresh":
                    _refresh(repo, args)
                metrics = {"calls": 0, "max_active": 0}
                if not args.enqueue_only:
                    metrics = Scheduler(repo, provider_factory=provider_factory or provider_for_settings,
                                        workers=args.workers, timeout=args.timeout, max_calls=args.max_calls,
                                        hook=hook, **(scheduler_options or {})).run()
                result = {"scheduler": metrics, "progress": repo.progress_page(limit=20)}
        print(json.dumps(result, ensure_ascii=False, allow_nan=False))
        return 0
    except KeyboardInterrupt:
        return 130
    except (ValueError, OSError, KeyError, sqlite3.Error) as error:
        print(json.dumps({"error": str(error)}, ensure_ascii=False), file=sys.stderr)
        return 2

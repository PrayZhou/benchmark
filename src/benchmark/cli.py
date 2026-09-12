from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import yaml

from .index import DEFAULT_INDEX_PATH, RunIndex
from .logging_config import configure_logging
from .orchestrator import run_tasks
from .replay import write_replay
from .schemas import ProviderConfig, RunConfig, TaskSpec
from .tasks import discover_tasks, load_task_dir

COMMANDS = {"run", "replay", "regrade", "index", "compare"}
# Options the top-level parser owns; seeing one first must not trigger the
# backward-compatible "implicit run" rewrite.
GLOBAL_FLAGS = {"-h", "--help", "--log-level", "--log-json"}


def _load(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def _collect_tasks(raw: dict, config_dir: Path) -> list[TaskSpec]:
    """Resolve tasks from directories first, then inline definitions.

    ``task_dirs`` / ``tasks_root`` point at ``tasks/<name>/`` directories, which
    own instruction and verifier. Inline ``tasks`` entries remain supported for
    quick experiments.
    """
    tasks: list[TaskSpec] = []
    for entry in raw.get("task_dirs", []) or []:
        tasks.append(load_task_dir(_resolve(entry, config_dir)))
    if raw.get("tasks_root"):
        tasks.extend(discover_tasks(_resolve(raw["tasks_root"], config_dir)))
    return tasks


def _resolve(entry: str, config_dir: Path) -> Path:
    path = Path(entry)
    return path if path.is_absolute() else config_dir / path


def _common_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--log-level", default="WARNING",
                        help="benchmark log level (DEBUG, INFO, WARNING, ERROR)")
    common.add_argument("--log-json", action="store_true", help="emit one JSON object per log record")
    return common


def build_parser() -> argparse.ArgumentParser:
    common = _common_parser()
    parser = argparse.ArgumentParser(description="Run Agent Benchmark tasks", parents=[common])
    sub = parser.add_subparsers(dest="command")

    run = sub.add_parser("run", parents=[common])
    run.add_argument("config", type=Path)
    run.add_argument("--output", type=Path, default=Path("reports"))
    run.add_argument("--provider")
    run.add_argument("--model")
    run.add_argument("--base-url")
    run.add_argument("--environment")
    run.add_argument("--task-dir", action="append", type=Path, default=[],
                     help="load a tasks/<name>/ directory (repeatable)")
    run.add_argument("--tasks-root", type=Path, help="load every task directory under this root")
    run.add_argument("--runs-per-task", type=int)
    run.add_argument("--concurrency", type=int)
    run.add_argument("--retries", type=int)
    run.add_argument("--index-db", type=Path,
                     help="after the run, ingest it into this cross-run SQLite index")

    replay = sub.add_parser("replay", parents=[common])
    replay.add_argument("run_dir", type=Path)
    replay.add_argument("trial_id")
    replay.add_argument("--no-execute", action="store_true",
                        help="list the recorded actions instead of re-executing them")

    regrade = sub.add_parser("regrade", parents=[common])
    regrade.add_argument("run_dir", type=Path)
    regrade.add_argument("trial_id")
    # A distinct dest is required: the subparsers action already owns
    # ``dest="command"``, and colliding with it would overwrite the subcommand
    # name (breaking command dispatch). REMAINDER keeps the verifier argv
    # intact, since ``-lc`` / ``-c`` would otherwise be parsed as options.
    # ``--command`` must therefore be the last option on the line.
    regrade.add_argument("--command", dest="verifier_command", nargs=argparse.REMAINDER, required=True,
                         help="verifier argv, e.g. --command bash -lc 'test -f x' (must come last)")

    index = sub.add_parser("index", parents=[common], help="ingest run directories into a SQLite index")
    index.add_argument("run_dirs", type=Path, nargs="+")
    index.add_argument("--db", type=Path, default=DEFAULT_INDEX_PATH)

    compare = sub.add_parser("compare", parents=[common], help="compare indexed runs")
    compare.add_argument("run_ids", nargs="*", help="run ids to compare (default: every indexed run)")
    compare.add_argument("--db", type=Path, default=DEFAULT_INDEX_PATH)
    compare.add_argument("--no-tasks", action="store_true", help="omit the per-task matrix")
    compare.add_argument("--dataset", help="only compare runs with this dataset label")
    compare.add_argument("--violations", action="store_true",
                         help="only compare runs whose trials recorded a violation")

    return parser


def main() -> None:
    parser = build_parser()
    argv = sys.argv[1:]
    # Backward-compatible invocation: benchmark CONFIG --output DIR
    if argv and argv[0] not in COMMANDS and argv[0] not in GLOBAL_FLAGS:
        argv = ["run", *argv]
    args = parser.parse_args(argv)
    configure_logging(getattr(args, "log_level", "WARNING"), json_format=getattr(args, "log_json", False))

    if args.command == "replay":
        print(write_replay(args.run_dir, args.trial_id, execute=not args.no_execute))
        return
    if args.command == "regrade":
        from .adapters.graders.command import CommandGrader
        from .regrade import write_regrade
        print(write_regrade(args.run_dir, args.trial_id,
                            CommandGrader(args.verifier_command, isolated=False)))
        return
    if args.command == "index":
        indexed = RunIndex(args.db).index_runs(args.run_dirs)
        print(json.dumps({"db": str(args.db), "indexed": indexed}, indent=2))
        return
    if args.command == "compare":
        payload = RunIndex(args.db).compare(args.run_ids or None, include_tasks=not args.no_tasks,
                                            dataset=args.dataset, violations_only=args.violations)
        print(json.dumps(payload, indent=2, default=str))
        return
    if args.command is None:
        parser.error("use `benchmark run CONFIG` (or `benchmark replay RUN_DIR TRIAL_ID`)")

    raw = _load(args.config)
    defaults = ProviderConfig.model_validate(raw.get("provider", {}))
    if args.provider:
        defaults.provider = args.provider
    if args.model:
        defaults.model = args.model
    if args.base_url:
        defaults.base_url = args.base_url
    run_cfg = RunConfig.model_validate(raw.get("run", {}))
    if args.runs_per_task is not None:
        run_cfg.runs_per_task = args.runs_per_task
    if args.concurrency is not None:
        run_cfg.concurrency = args.concurrency
    if args.retries is not None:
        run_cfg.retry.max_attempts = args.retries + 1
    tasks = _collect_tasks(raw, args.config.parent)
    for directory in args.task_dir:
        tasks.append(load_task_dir(directory))
    if args.tasks_root:
        tasks.extend(discover_tasks(args.tasks_root))
    for item in raw.get("tasks", []):
        item = dict(item)
        tasks.append(TaskSpec.model_validate(item))
    if not tasks:
        parser.error("no tasks resolved: set task_dirs/tasks_root/tasks in the config or pass --task-dir")
    for task in tasks:
        if task.provider.provider == "mock" and task.provider.model is None:
            task.provider = defaults.model_copy()
        if args.environment:
            task.environment_type = args.environment
    results = run_tasks(tasks, Path.cwd(), args.output, run_cfg)
    payload = {"completed": len(results), "report": str(args.output / "report.json")}
    if args.index_db:
        payload["indexed"] = RunIndex(args.index_db).index_run(args.output)
    print(json.dumps(payload, indent=2))

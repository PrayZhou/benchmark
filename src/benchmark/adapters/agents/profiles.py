"""Documented, ready-to-copy invocation profiles for common coding CLIs.

Each entry is a plain mapping of keyword arguments accepted by
:class:`~benchmark.adapters.agents.cli.CLIAgent` (``command``, ``args``,
``prompt_mode``, ``prompt_flag``, ...). The intent is that a user who wants to
benchmark ``claude`` or ``codex`` can start from a known invocation instead of
reverse-engineering one, then override any key in their own task config:

.. code-block:: yaml

    agent: cli
    environment_type: local
    cli:
      command: claude
      args: ["{instruction}"]
      prompt_mode: argv
      prompt_flag: "-p"

**These profiles have not been verified against the real CLIs in this
environment.** No coding CLI is installed on the development host and network
egress is blocked, so nothing here has been run end to end. They encode the
documented non-interactive invocations (``claude -p`` print mode, ``codex
exec``) and should be treated as a starting point to confirm against the
installed CLI and its version, not as a guarantee.

The values are copied into a task's ``cli:`` block, so a task can always
override a field without editing this module.
"""
from __future__ import annotations

from typing import Any

PROFILES: dict[str, dict[str, Any]] = {
    # ``claude -p "<prompt>"`` runs Claude Code once in print/non-interactive
    # mode and exits. The prompt is passed as a positional argument.
    "claude": {
        "command": "claude",
        "args": ["{instruction}"],
        "prompt_mode": "argv",
        "prompt_flag": "-p",
    },
    # ``codex exec "<prompt>"`` runs the OpenAI Codex CLI non-interactively;
    # ``exec`` is a subcommand, so it lives in ``args`` rather than a flag.
    "codex": {
        "command": "codex",
        "args": ["exec", "{instruction}"],
        "prompt_mode": "argv",
    },
}

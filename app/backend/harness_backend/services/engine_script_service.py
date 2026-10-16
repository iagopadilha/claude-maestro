"""Runs an engine CLI script (`delivery`, `loop_summary`) against a folder, without a shell, waiting for the end with a timeout.

It is always the USER acting through the panel: `LOOP_HEADLESS` never reaches the script environment.
"""
import os
import sys
from dataclasses import dataclass
from typing import Protocol

from ..infrastructure.process_runner import TIMEOUT_CODE

HEADLESS_MODE_VARIABLE = "LOOP_HEADLESS"


@dataclass
class EngineScriptResult:
    code: object
    output: str
    error: str
    timed_out: bool


class IEngineScriptService(Protocol):
    def run_engine_script_and_wait(self, script_name, target_dir, args, timeout_seconds, extra_env=None) -> EngineScriptResult: ...


class EngineScriptService:
    def __init__(self, config, process_runner):
        self.config = config
        self.process_runner = process_runner

    def build_user_env(self, extra_env=None):
        env = dict(os.environ)
        env.pop(HEADLESS_MODE_VARIABLE, None)
        env.update(extra_env or {})
        return env

    def run_engine_script_and_wait(self, script_name, target_dir, args, timeout_seconds, extra_env=None):
        """`python <script> <target_dir> <args...>` with cwd = target_dir. The script path is resolved on every call
        (`config.engine_script_path`), so test overrides apply without rebuilding the module."""
        argv = [sys.executable, self.config.engine_script_path(script_name), target_dir] + list(args)
        code, output, error = self.process_runner.run_and_wait(argv, target_dir, timeout_seconds, env=self.build_user_env(extra_env))
        timed_out = code == TIMEOUT_CODE
        return EngineScriptResult(None if timed_out else code, output or "", error or "", timed_out)

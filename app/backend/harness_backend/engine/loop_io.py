#!/usr/bin/env python3
"""Small file, JSON, git and process helpers shared by the engine scripts (`run_loop.py` and the modules split out of it).

Every function here is moved as it was (no behavior change); `run_loop.py` re-exports them, so `run_loop.git`, `run_loop.save_meta`... still work.
"""
import os
import sys

ENGINE_DIR = os.path.dirname(os.path.abspath(__file__))
if ENGINE_DIR not in sys.path:
    sys.path.insert(0, ENGINE_DIR)
import json
import subprocess

import loop_state  # noqa: E402  (typed state: loop folder paths)

IS_WIN = os.name == "nt"


def run(args, cwd=None, timeout=120):
    try:
        out = subprocess.run(args, cwd=cwd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout)
        return out.returncode, out.stdout.strip(), out.stderr.strip()
    except (OSError, subprocess.SubprocessError) as error:
        return 1, "", str(error)


def git(cwd, *args):
    return run(["git", "-C", cwd, *args])


def read(path):
    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            return handle.read()
    except OSError:
        return ""


def load_json(path):
    try:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return {}


APP_OWNED_META_KEYS = ("delivery", "max_cost_usd")  # written by the app (start, raise-limit) in the worktree meta: the engine never drops them


DELIVERY_PENDING_NAME = "delivery-pending.json"


def save_meta(path, meta):
    """Saves meta.json keeping what the app wrote there meanwhile: the disk value of the app-owned keys wins over the in-memory copy."""
    on_disk = load_json(path)
    for key in APP_OWNED_META_KEYS:
        if key in on_disk:
            meta[key] = on_disk[key]
    save_json(path, meta)


def merge_pending_delivery(repo, meta):
    """The app wrote the delivery choices before the worktree existed (<repo>/.specs/project/loop/internal/delivery-pending.json): they
    become meta["delivery"] and the pending file is deleted."""
    path = os.path.join(loop_state.internal_dir(repo), DELIVERY_PENDING_NAME)
    pending = load_json(path)
    if isinstance(pending, dict) and pending:
        meta["delivery"] = dict(meta.get("delivery") or {}, **pending)
    try:
        os.remove(path)
    except OSError:
        pass


def save_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        json.dump(data, handle, ensure_ascii=False, indent=2)


def pid_alive(pid):
    if pid <= 0:
        return False
    if IS_WIN:
        _, out, _ = run(["tasklist", "/FI", "PID eq %d" % pid, "/NH"])
        return str(pid) in out
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def kill_tree(pid):
    if IS_WIN:
        run(["taskkill", "/PID", str(pid), "/T", "/F"])
    else:
        try:
            os.killpg(os.getpgid(pid), 9)
        except OSError:
            pass


def worktree_of(repo):
    return os.path.join(os.path.dirname(repo), os.path.basename(repo) + "-worktree-loop")

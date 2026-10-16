#!/usr/bin/env python3
"""Demo stand-in for the engine scripts the panel can trigger (`run_loop.py`, `delivery.py`, `loop_summary.py`).

Usage (by the app): python demo_engine_stub.py <project folder> [flags...]
  --check            prints a PROBLEM line: the demo loop is simulated, so a real loop is never "ready" here
  --answer <text>    records the answer for the demo animator (`.specs/project/loop/internal/demo-answer.txt`) and exits 0
  anything else      prints a demo notice and exits 1 (nothing is pushed, delivered or started)
Never runs `claude`, git or the real engine. Standard library only.
"""
import os
import sys

NOTICE = "demo mode: this loop is simulated by `harness.py demo`; nothing runs, pushes or spends here. Open a real repository to run a loop."
ANSWER_FILE_PARTS = (".specs", "project", "loop", "internal", "demo-answer.txt")
EXIT_OK = 0
EXIT_REFUSED = 1


def read_flag(args, name):
    if name in args and args.index(name) + 1 < len(args):
        return args[args.index(name) + 1]
    return None


def main(argv):
    target = argv[0] if argv else ""
    args = argv[1:]
    if "--check" in args:
        print("[check] PROBLEM: " + NOTICE)
        return EXIT_REFUSED
    answer = read_flag(args, "--answer")
    if answer is not None and target and os.path.isdir(target):
        path = os.path.join(target, *ANSWER_FILE_PARTS)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(answer)
        print("demo mode: answer recorded; the demo loop continues on its own.")
        return EXIT_OK
    print(NOTICE)
    return EXIT_REFUSED


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

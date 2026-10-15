#!/usr/bin/env python3
"""Loads the plugin's shared settings (`.env` file in <plugin root>/shared/) without any library.

Use in any script:
    import envload            # with <plugin root>/shared on sys.path
    envload.load()            # fills os.environ WITHOUT overriding real environment variables
    value = envload.get("LOOP_NOTIFY_TO")

File lookup order: variable LOOP_ENV_FILE, then `<this folder>/.env` (= <plugin root>/shared/.env).
Model: shared/.env.example. Format: `KEY=value`, one per line; comments only on whole lines starting with `#`; a value in
single or double quotes is read literally (quote it when it holds # & % { } ? ^). The `.env` is a secret: never commit it.
"""
import os

HERE = os.path.dirname(os.path.abspath(__file__))


def plugin_root():
    """Plugin root: env CLAUDE_PLUGIN_ROOT when set, otherwise the parent of this shared/ folder."""
    return os.path.abspath(os.environ.get("CLAUDE_PLUGIN_ROOT") or os.path.dirname(HERE))


def candidates():
    explicit = os.environ.get("LOOP_ENV_FILE")
    paths = [explicit] if explicit else []
    paths.append(os.path.join(HERE, ".env"))
    return paths


def parse(text):
    values = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        if key:
            values[key] = value
    return values


def load():
    """Returns the path of the .env used (or ''), filling os.environ only where the variable is not set yet."""
    for path in candidates():
        if path and os.path.isfile(path):
            try:
                with open(path, encoding="utf-8-sig") as handle:
                    for key, value in parse(handle.read()).items():
                        os.environ.setdefault(key, value)
                return path
            except OSError:
                continue
    return ""


def get(name, default=""):
    load()
    return os.environ.get(name, default)


def get_list(name):
    return [item.strip() for item in get(name).split(",") if item.strip()]


if __name__ == "__main__":
    used = load()
    print("file:", used or "(no .env found)")
    for key in sorted(k for k in os.environ if k.startswith("LOOP_")):
        secret = any(word in key for word in ("TOKEN", "SECRET", "PASSWORD", "KEY", "WEBHOOK"))
        print(" ", key, "=", "<set>" if secret and os.environ[key] else os.environ[key])

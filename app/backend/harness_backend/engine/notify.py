#!/usr/bin/env python3
"""Loop and orchestrator notifications: console, toast, file, email (SMTP or HTTP provider) and webhook. Stdlib only.

Configuration (.env read by harness_backend/config/envload.py: LOOP_ENV_FILE, then ~/.loop-harness/.env, then
<plugin root>/shared/.env; a real environment variable wins):
  LOOP_NOTIFY_CHANNELS   console,toast,file,email,webhook   (a channel without configuration is skipped, never breaks the loop)
  LOOP_NOTIFY_TO         email recipients (comma-separated)
  LOOP_EMAIL_PROVIDER    smtp (default) | http: who sends the email (providers in email_senders.py)
  LOOP_SMTP_HOST / LOOP_SMTP_PORT / LOOP_SMTP_USER / LOOP_SMTP_PASSWORD / LOOP_SMTP_FROM / LOOP_SMTP_SECURITY (starttls|ssl|none)
  LOOP_EMAIL_HTTP_URL / LOOP_EMAIL_HTTP_TOKEN / LOOP_EMAIL_HTTP_FROM   (provider http: POST JSON {to, subject, html, from})
  LOOP_WEBHOOK_URL       URL that receives a POST with JSON {subject, body, event, project}

Usage:
  python notify.py --test [--channels email]                # sends a test notice through the channels (or only those listed)
  python notify.py --subject "..." --body-file msg.txt      # standalone notice (normal orchestrator, in a chat conversation)
  python notify.py --subject "..." --body "text"

STANDARD MESSAGE (every channel): the REASON the loop stopped plus one line that sends you to the panel to continue (answer, resume or
approve there). LOOP_NOTIFY_CONTINUE=commands brings back the long form, a "How to continue" section with the ways to answer:
  1) Loop via the script: run the `run_loop.py ... --answer "..."` command on the PC (highlighted: dark block in the email,
     line marked with >> in plain text);
  2) Normal conversation (Claude chat with /orchestrator): do NOT run the script; answer straight in the chat, the orchestrator
     operates normally and carries on;
  3) Panel (only when the loop started the panel; ctx["monitor_url"]): answer on the local page. The URL also shows in the
     header of EVERY channel (text, HTML, short) and in the toast. A standalone chat notice (--subject/--body) does not change.

Each channel is a function `channel_<name>(subject, body, ctx, work)` returning (ok, detail), registered in CHANNELS with the
message format it accepts ("full" = plain text, "html" = structured email, "short" = size-limited text) and the variables it
requires. A new service = one function + one line in CHANNELS. The body never carries file contents or credentials: only what
comes from LOOP.json, the ROADMAP and the git log, cut to a fixed size.
"""
import argparse
import datetime
import html
import importlib.util
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from app_paths import envload  # noqa: E402  (app .env: LOOP_ENV_FILE > ~/.loop-harness/.env > <plugin>/shared/.env)
import email_senders  # noqa: E402
import loop_state  # noqa: E402

IS_WIN = os.name == "nt"
MAX_BODY = 6000
MAX_SHORT = 950  # short channels (toast and similar) keep the text under ~1000 characters
TITLES = {
    "awaiting_user": "waiting for your decision",
    "error": "stopped with an error",
    "done": "done",
    "test": "test notice",
    "custom": "notice",
}
APP_HARNESS = os.path.abspath(os.path.join(HERE, "..", "..", "..", "harness.py"))
CHAT_NOTE = ("If you are in a NORMAL CONVERSATION (Claude chat with /orchestrator): do NOT run the script. "
             "Answer straight in the chat; the orchestrator operates normally and carries on.")


def clip(text, size):
    text = str(text or "").strip()
    return text if len(text) <= size else text[: size - 3] + "..."


# ---------------------------------------------------------------- common parts of the message
def command_base(ctx):
    return 'python "%s" "%s"%s' % (ctx.get("script", "<run_loop.py>"), ctx.get("repo", "<repo>"), ctx.get("flags", ""))


def numbered(options, limit=6):
    """Numbered options: "1) try again | 2) skip and continue"."""
    return " | ".join("%d) %s" % (number, option) for number, option in enumerate(options[:limit], 1))


def continue_in_panel():
    """The default: a notice says WHY the loop stopped and sends the reader to the panel to continue. LOOP_NOTIFY_CONTINUE=commands
    brings back the long form (the resume commands, the chat way and the panel)."""
    return (envload.get("LOOP_NOTIFY_CONTINUE") or "panel").strip().lower() != "commands"


def needs_continue_block(ctx):
    """Panel mode: only a stop that waits for the user (a question or an error) says how to continue; a test or a finish does not."""
    return ctx.get("event") in ("awaiting_user", "error")


def panel_instruction(ctx):
    """One sentence: the panel address when it is up, otherwise the command that opens it."""
    if ctx.get("monitor_url"):
        return "Open the panel to continue (answer, resume or approve there): " + ctx["monitor_url"]
    return 'Open the panel to continue (answer, resume or approve there): python "%s" serve --project "%s"' % (APP_HARNESS, ctx.get("repo", "<repo>"))


def command_set(ctx):
    """Resume commands via the script: TWO (by option number and with free text). Empty when the event needs no action."""
    base = command_base(ctx)
    event = ctx.get("event")
    if event == "awaiting_user":
        lines = []
        if ctx.get("options"):
            lines.append('%s --option 1' % base)
        return lines + ['%s --answer "WRITE YOUR MESSAGE HERE"' % base]
    if event == "test":
        return ['%s --answer "WRITE YOUR MESSAGE HERE"' % base]
    if event == "error":
        return [base]
    return []


def subject_of(ctx):
    if ctx.get("event") == "custom":
        return ctx.get("subject") or "[loop] notice"
    return "[loop] %s: %s" % (ctx.get("project", "project"), TITLES.get(ctx.get("event", "custom"), ctx.get("event")))


def meta_line(ctx):
    header = ["Project: %s" % ctx.get("project", "project")]
    if ctx.get("branch"):
        header.append("Branch: %s" % ctx["branch"])
    if ctx.get("turn"):
        header.append("Turn %s" % ctx["turn"])
    if ctx.get("cost") is not None:
        header.append("Cost: US$ %.2f" % float(ctx["cost"]))
    header.append("When: %s" % datetime.datetime.now().strftime("%Y-%m-%d %H:%M"))
    return header


# ---------------------------------------------------------------- "full" format: plain text (file, console, webhook)
def build(ctx):
    event = ctx.get("event", "custom")
    subject = subject_of(ctx)
    if event == "custom":
        return subject, clip("%s\n\nHOW TO CONTINUE\nOrigin: normal conversation. %s" % (ctx.get("extra_body") or "", CHAT_NOTE), MAX_BODY)
    out = [" | ".join(meta_line(ctx))]
    if ctx.get("monitor_url"):
        out.append("Panel: " + ctx["monitor_url"])
    if ctx.get("reason") and event != "awaiting_user":
        out += ["", "REASON", clip(ctx["reason"], 1500)]
    if ctx.get("features"):
        out += ["", "DONE"] + ["- %s  %s" % (slug, status) for slug, status in ctx["features"][:15]]
    if ctx.get("commits"):
        out += ["", "COMMITS"] + ["- " + clip(line, 120) for line in ctx["commits"][:8]]
    if event == "awaiting_user":
        out += ["", "NEEDS YOU", clip(ctx.get("reason") or "(no text: see the summary)", 1500)]
        if ctx.get("options"):
            out.append("Options: " + numbered([str(o) for o in ctx["options"]]))
    if ctx.get("to_check"):
        out += ["", "TO CHECK"] + ["- " + clip(item, 200) for item in ctx["to_check"][:8]]
    if ctx.get("next") and event == "done":
        out += ["", "NEXT STEP", clip(ctx["next"], 400)]
    if event == "done" and ctx.get("push"):
        out += ["", "Branch push: " + clip(ctx["push"], 200)]
    if ctx.get("pr"):
        out += ["", "PR: " + clip(ctx["pr"], 300) + ("  (%s)" % ctx["pr_status"] if ctx.get("pr_status") else "")]
    if ctx.get("validate") and event == "done":
        out += ["", "FOR YOU TO VALIDATE (decided without stopping you)"] + ["- " + clip(item, 200) for item in ctx["validate"][:8]]
    commands = command_set(ctx)
    if continue_in_panel():
        if needs_continue_block(ctx):
            out += ["", "HOW TO CONTINUE", panel_instruction(ctx)]
    elif commands:
        out += ["", "HOW TO CONTINUE",
                "1) Loop via the script (the case of this notice): run one of the commands marked with >> on your PC. The first answers by option "
                "NUMBER (change the 1); the second takes free text (change the text in quotes)."]
        out += [">> " + line for line in commands]
        out += ["", "2) " + CHAT_NOTE]
        if ctx.get("monitor_url"):
            out += ["", "3) Answer in the panel: " + ctx["monitor_url"]]
    if ctx.get("graph") and event != "test":
        out += ["", "PROGRESS", clip(ctx["graph"], 1800)]
    if ctx.get("summary"):
        out += ["", "Full summary: " + ctx["summary"]]
    if event == "test":
        out += ["", "This is a loop TEST notice. Nothing needs to be done."]
    return subject, clip("\n".join(out), MAX_BODY)


# ---------------------------------------------------------------- "html" format: structured email
def build_html(ctx):
    e = html.escape
    event = ctx.get("event", "custom")
    subject = subject_of(ctx)
    heading = lambda text: '<h3 style="margin:18px 0 6px;font-size:15px;color:#111827">%s</h3>' % e(text)
    para = lambda text: '<p style="margin:0 0 8px;line-height:1.45">%s</p>' % e(text)
    bullets = lambda items: '<ul style="margin:0 0 8px;padding-left:20px">%s</ul>' % "".join("<li>%s</li>" % e(i) for i in items)
    code = lambda lines: (
        '<pre style="background:#0f172a;color:#e2e8f0;padding:12px 14px;border-radius:6px;font-family:Consolas,Menlo,monospace;'
        'font-size:13px;line-height:1.5;white-space:pre-wrap;word-break:break-all;margin:6px 0 10px">%s</pre>' % e("\n".join(lines))
    )
    out = ['<div style="font-family:Segoe UI,Arial,sans-serif;font-size:14px;color:#1f2937;max-width:680px">',
           '<h2 style="margin:0 0 4px;font-size:18px">%s</h2>' % e(subject),
           '<p style="margin:0 0 12px;color:#6b7280">%s</p>' % e(" | ".join(meta_line(ctx)))]
    monitor_link = '<a href="%s">%s</a>' % (e(ctx["monitor_url"]), e(ctx["monitor_url"])) if ctx.get("monitor_url") else ""
    if monitor_link and event != "custom":
        out.append('<p style="margin:0 0 10px"><b>Panel:</b> %s</p>' % monitor_link)
    if event == "custom":
        out.append('<div style="white-space:pre-wrap;line-height:1.45">%s</div>' % e(ctx.get("extra_body") or ""))
        out += [heading("How to continue"), para("Origin: normal conversation. " + CHAT_NOTE), "</div>"]
        return subject, "".join(out)
    if event == "test":
        out.append(para("This is a loop TEST notice. Nothing needs to be done."))
    if ctx.get("reason") and event not in ("awaiting_user", "test"):
        out += [heading("Reason"), para(clip(ctx["reason"], 1500))]
    if ctx.get("features"):
        out += [heading("Done"), bullets(["%s: %s" % (slug, status) for slug, status in ctx["features"][:15]])]
    if ctx.get("commits"):
        out += [heading("Commits"), bullets([clip(line, 120) for line in ctx["commits"][:8]])]
    if event == "awaiting_user":
        out += [heading("Needs you"), para(clip(ctx.get("reason") or "(no text: see the summary)", 1500))]
        if ctx.get("options"):
            out.append(para("Options: " + numbered([str(o) for o in ctx["options"]])))
    if ctx.get("to_check"):
        out += [heading("To check"), bullets([clip(item, 200) for item in ctx["to_check"][:8]])]
    if event == "done":
        if ctx.get("next"):
            out += [heading("Next step"), para(clip(ctx["next"], 400))]
        if ctx.get("push"):
            out.append(para("Branch push: " + clip(ctx["push"], 200)))
        if ctx.get("validate"):
            out += [heading("For you to validate (decided without stopping you)"), bullets([clip(item, 200) for item in ctx["validate"][:8]])]
    if ctx.get("pr"):
        out.append('<p style="margin:10px 0 8px"><b>PR:</b> <a href="%s">%s</a>%s</p>' % (
            e(ctx["pr"]), e(clip(ctx["pr"], 300)), e(" (%s)" % ctx["pr_status"]) if ctx.get("pr_status") else ""))
    commands = command_set(ctx)
    if continue_in_panel():
        if needs_continue_block(ctx):
            out += [heading("How to continue"), para(panel_instruction(ctx))]
    elif commands:
        out += [heading("How to continue"),
                '<p style="margin:0 0 4px"><b>1) Loop via the script</b> (the case of this notice): run one of the commands on your PC. The first answers '
                'by <b>option number</b> (change the 1); the second takes free text. The script records the answer and the loop carries on by itself.</p>', code(commands),
                '<p style="margin:0 0 8px"><b>2) Normal conversation (Claude chat with /orchestrator):</b> do not run the script. '
                'Answer straight in the chat; the orchestrator operates normally and carries on.</p>']
        if monitor_link:
            out.append('<p style="margin:0 0 8px"><b>3) Answer in the panel:</b> %s</p>' % monitor_link)
        if ctx.get("worktree") and event == "awaiting_user":
            out += [para("Want to talk before deciding? Open Claude inside the loop worktree:"),
                    code(['cd "%s"' % ctx["worktree"], "claude", "/orchestrator"])]
    if ctx.get("graph") and event != "test":
        out += [heading("Progress"), '<pre style="background:#f3f4f6;padding:10px 12px;border-radius:6px;font-family:Consolas,Menlo,monospace;font-size:12.5px;line-height:1.45;white-space:pre-wrap">%s</pre>' % e(clip(ctx["graph"], 1800))]
    if ctx.get("summary"):
        out.append('<p style="margin:14px 0 0;color:#6b7280">Full summary: %s</p>' % e(ctx["summary"]))
    out.append("</div>")
    return subject, "".join(out)


# ---------------------------------------------------------------- "short" format: size-limited channels
def build_short(ctx):
    """Short normal text + the command in a monospaced block (```). Up to ~950 characters."""
    event = ctx.get("event", "custom")
    title = subject_of(ctx)
    if event == "custom":
        body = "*%s*\n%s\n\n*HOW TO CONTINUE*\nOrigin: normal conversation. Answer straight in the Claude chat; no script." % (title, clip(ctx.get("extra_body"), 600))
        return title, clip(body, MAX_SHORT)
    done = [f for f in (ctx.get("features") or []) if f[1] in ("verified", "blocked")]
    total = len(ctx.get("features") or [])
    meta = [x for x in (ctx.get("branch"), ("turn %s" % ctx["turn"]) if ctx.get("turn") else "",
                        ("US$ %.2f" % float(ctx["cost"])) if ctx.get("cost") is not None else "") if x]

    def compose(question_size, with_check):
        lines = ["*%s*" % title]
        if meta:
            lines.append(" | ".join(meta))
        if ctx.get("monitor_url"):
            lines.append("Panel: " + ctx["monitor_url"])
        if total:
            lines.append("Progress: %d of %d features closed" % (len(done), total))
        if with_check and ctx.get("graph_short") and event in ("awaiting_user", "error"):
            lines.append(clip(ctx["graph_short"], 200))
        if event == "awaiting_user":
            lines += ["", "*Needs you:*", clip(ctx.get("reason") or "see the summary", question_size)]
            if ctx.get("options"):
                lines.append("Options: " + numbered([clip(o, max(question_size // 2, 40)) for o in ctx["options"]], 4))
            if with_check and ctx.get("to_check"):
                lines.append("To check: " + clip("; ".join(ctx["to_check"][:3]), 140))
        elif event == "error":
            lines += ["", "*Reason:*", clip(ctx.get("reason"), question_size)]
        elif event == "done":
            lines += ["", "All done."] + (["Push: " + clip(ctx["push"], 120)] if ctx.get("push") else [])
            if ctx.get("pr"):
                lines.append("PR: " + clip(ctx["pr"], 170))
            if ctx.get("validate"):
                lines.append("To validate: " + clip("; ".join(ctx["validate"][:3]), 150))
        elif event == "test":
            lines += ["", "Loop notification test. Nothing to do."]
        if continue_in_panel():
            if needs_continue_block(ctx):
                lines += ["", "*HOW TO CONTINUE*", panel_instruction(ctx)]
        elif event in ("awaiting_user", "error", "test"):
            answer = ' --option 1' if (ctx.get("options") and event == "awaiting_user") else ' --answer "YOUR MESSAGE"'
            command = command_base(ctx) + ('' if event == "error" else answer)
            lines += ["", "*HOW TO CONTINUE*",
                      "1) _Loop via the script_ (this notice): run on the PC" + (" (change the 1 to the option number):" if "--option" in command else ":"),
                      "```\n" + command + "\n```",
                      "2) _Normal conversation in the Claude chat_: answer straight in the chat, no script. /orchestrator carries on normally."]
            if ctx.get("monitor_url"):
                lines.append("3) _Panel_: answer at " + ctx["monitor_url"])
        return "\n".join(lines)

    body = compose(260, True)
    if len(body) > MAX_SHORT:
        body = compose(120, False)
    if len(body) > MAX_SHORT:
        body = compose(40, False)
    return title, body[:1000]


# ---------------------------------------------------------------- local channels
def channel_console(subject, body, ctx, work):
    print("\n*** %s ***\n%s\n" % (subject, body))
    return True, "ok"


def channel_file(subject, body, ctx, work):
    if not work:
        return False, "no work folder"
    target = loop_state.loop_path(work, "NOTICE.txt")
    os.makedirs(os.path.dirname(target), exist_ok=True)
    with open(target, "w", encoding="utf-8") as handle:
        handle.write("%s\n%s\n\n%s\n" % (datetime.datetime.now().isoformat(timespec="seconds"), subject, body))
    return True, "NOTICE.txt"


def channel_toast(subject, body, ctx, work):
    if not IS_WIN:
        return False, "toast is Windows-only"
    safe = lambda text: str(text).replace("'", "").replace("\n", " ")[:200]
    first = body.splitlines()[0] if body else ""
    if ctx.get("monitor_url"):
        first = "Panel: " + ctx["monitor_url"]
    script = (
        "[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] | Out-Null;"
        "$x = [Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent([Windows.UI.Notifications.ToastTemplateType]::ToastText02);"
        "$n = $x.GetElementsByTagName('text');"
        "$n.Item(0).AppendChild($x.CreateTextNode('%s')) | Out-Null;"
        "$n.Item(1).AppendChild($x.CreateTextNode('%s')) | Out-Null;"
        "$id = '{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\\WindowsPowerShell\\v1.0\\powershell.exe';"
        "[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($id).Show([Windows.UI.Notifications.ToastNotification]::new($x))"
    ) % (safe(subject), safe(first))
    try:
        done = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script], capture_output=True, text=True, timeout=30)
        if done.returncode != 0:
            return False, "powershell exited with code %s: %s" % (done.returncode, (done.stderr or "").strip()[:100])
        return True, "ok"
    except (OSError, subprocess.SubprocessError) as error:
        return False, str(error)[:100]


# ---------------------------------------------------------------- external services
def channel_email(subject, body, ctx, work):
    """Email as structured HTML with the command in a code block; the provider (SMTP or HTTP) is in email_senders.py."""
    sender = email_senders.build_email_sender()
    if sender is None:
        return False, "unknown LOOP_EMAIL_PROVIDER: %s" % email_senders.chosen_provider()
    return sender.send(email_senders.recipients(), subject, body)


def channel_webhook(subject, body, ctx, work):
    """Webhook: POST JSON {subject, body, event, project} to LOOP_WEBHOOK_URL."""
    url = envload.get("LOOP_WEBHOOK_URL")
    payload = {"subject": subject, "body": body, "event": ctx.get("event", "custom"), "project": ctx.get("project", "")}
    request = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"), method="POST", headers={"Content-Type": "application/json"})
    last = ""
    for _ in range(3):
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return 200 <= response.status < 300, "HTTP %d" % response.status
        except urllib.error.HTTPError as error:
            return False, "HTTP %d %s" % (error.code, error.read().decode("utf-8", "replace")[:200])  # server answered: retrying will not help
        except (urllib.error.URLError, OSError) as error:
            last = str(error)[:150]
            time.sleep(3)
    return False, "no response from the webhook: " + last


# name -> (function, message format, required variables: a list, or a function returning one)
CHANNELS = {
    "console": (channel_console, "full", []),
    "toast": (channel_toast, "short", []),
    "file": (channel_file, "full", []),
    "email": (channel_email, "html", email_senders.required_variables),
    "webhook": (channel_webhook, "full", ["LOOP_WEBHOOK_URL"]),
}


LOCAL_EXTENSIONS_DIR = os.path.join(HERE, "local_ext")


def load_local_extensions():
    """Loads every `engine/local_ext/*.py` (a folder git ignores, so a private channel or e-mail provider of one machine never
    reaches the repository). Each module may define `register(channels, email_providers)` to add entries to CHANNELS and to
    email_senders.PROVIDERS. A broken extension is skipped; it never breaks the loop. Returns the names loaded."""
    loaded = []
    if not os.path.isdir(LOCAL_EXTENSIONS_DIR):
        return loaded
    for name in sorted(os.listdir(LOCAL_EXTENSIONS_DIR)):
        if not name.endswith(".py") or name.startswith("_"):
            continue
        try:
            spec = importlib.util.spec_from_file_location("local_ext_" + name[:-3], os.path.join(LOCAL_EXTENSIONS_DIR, name))
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            module.register(CHANNELS, email_senders.PROVIDERS)
            loaded.append(name)
        except Exception as error:  # noqa: BLE001  (an extension of one machine must not stop the notices)
            print("[notify] local extension %s skipped: %s" % (name, str(error)[:150]), file=sys.stderr)
    return loaded


load_local_extensions()


def missing_config(name):
    """Variables the channel is missing to work (empty list = configured)."""
    entry = CHANNELS.get(name)
    required = entry[2] if entry else []
    required = required() if callable(required) else required
    return [var for var in required if not envload.get(var.split(" ")[0])]


def dispatch(ctx, work="", only=None):
    """Sends through the configured channels. Never raises. Returns {channel: 'ok: ...' | 'FAILED: ...' | 'SKIPPED: ...'}."""
    envload.load()
    built = {"full": build(ctx), "html": build_html(ctx), "short": build_short(ctx)}
    names = only or envload.get_list("LOOP_NOTIFY_CHANNELS") or ["console", "toast", "file"]
    results = {}
    for name in names:
        entry = CHANNELS.get(name)
        if not entry:
            results[name] = "FAILED: unknown channel"
            continue
        handler, style, _ = entry
        missing = missing_config(name)
        if missing:
            results[name] = "SKIPPED: not configured (%s)" % ", ".join(missing)
            continue
        subject, body = built[style]
        try:
            ok, detail = handler(subject, body, ctx, work)
        except Exception as error:  # a notice never breaks the loop
            ok, detail = False, "exception: %s" % str(error)[:150]
        results[name] = ("ok: " if ok else "FAILED: ") + detail
    return results


def main():
    parser = argparse.ArgumentParser(description="Loop notices (console, toast, file, email, webhook)")
    parser.add_argument("--test", action="store_true", help="sends a test notice through the configured channels")
    parser.add_argument("--subject", default="")
    parser.add_argument("--body", default="")
    parser.add_argument("--body-file", default="")
    parser.add_argument("--channels", default="", help="overrides LOOP_NOTIFY_CHANNELS (e.g. email,console)")
    opts = parser.parse_args()
    envload.load()
    only = [c.strip() for c in opts.channels.split(",") if c.strip()] or None
    if opts.test:
        ctx = {"event": "test", "project": "test", "branch": "loop/example", "turn": 1, "cost": 0.0,
               "script": os.path.join(HERE, "run_loop.py"), "repo": "/path/to/project", "flags": " --wave 1",
               "worktree": "/path/to/project-worktree-loop",
               "features": [("example-feature", "verified"), ("other-feature", "pending")]}
    elif opts.subject and (opts.body or opts.body_file):
        text = opts.body
        if opts.body_file:
            with open(opts.body_file, encoding="utf-8", errors="replace") as handle:
                text = handle.read()
        ctx = {"event": "custom", "subject": opts.subject, "project": "", "extra_body": clip(text, 3000)}
    else:
        parser.print_help()
        return 2
    results = dispatch(ctx, "", only)
    for name, status in results.items():
        print("%-9s %s" % (name, status))
    return 0 if all(status.startswith("ok") for status in results.values()) else 1


if __name__ == "__main__":
    sys.exit(main())

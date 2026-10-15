#!/usr/bin/env python3
"""Loop and orchestrator notifications: console, toast, file, email (SMTP) and webhook. Stdlib only.

Configuration (centralized in <plugin root>/shared/.env, read by shared/envload.py; a real environment variable wins):
  LOOP_NOTIFY_CHANNELS   console,toast,file,email,webhook   (a channel without configuration is skipped, never breaks the loop)
  LOOP_NOTIFY_TO         email recipients (comma-separated)
  LOOP_SMTP_HOST / LOOP_SMTP_PORT / LOOP_SMTP_USER / LOOP_SMTP_PASSWORD / LOOP_SMTP_FROM / LOOP_SMTP_SECURITY (starttls|ssl|none)
  LOOP_WEBHOOK_URL       URL that receives a POST with JSON {subject, body, event, project}

Usage:
  python notify.py --test [--channels email]                # sends a test notice through the channels (or only those listed)
  python notify.py --subject "..." --body-file msg.txt      # standalone notice (normal orchestrator, in a chat conversation)
  python notify.py --subject "..." --body "text"

STANDARD MESSAGE (every channel): normal text + a "How to continue" section with BOTH ways, always:
  1) Loop via the script: run the `run_loop.py ... --answer "..."` command on the PC (highlighted: dark block in the email,
     line marked with >> in plain text);
  2) Normal conversation (Claude chat with /orchestrator): do NOT run the script; answer straight in the chat, the orchestrator
     operates normally and carries on.

Each channel is a function `channel_<name>(subject, body, ctx, work)` returning (ok, detail), registered in CHANNELS with the
message format it accepts ("full" = plain text, "html" = structured email) and the variables it requires. A new service = one
function + one line in CHANNELS. The body never carries file contents or credentials: only what comes from LOOP.json, the ROADMAP and
the git log, cut to a fixed size.
"""
import argparse
import datetime
import html
import json
import os
import smtplib
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.request
from email.mime.text import MIMEText

HERE = os.path.dirname(os.path.abspath(__file__))
PLUGIN_ROOT = os.path.abspath(os.environ.get("CLAUDE_PLUGIN_ROOT") or os.path.join(HERE, "..", "..", ".."))
sys.path.insert(0, os.path.join(PLUGIN_ROOT, "shared"))
import envload  # noqa: E402

IS_WIN = os.name == "nt"
MAX_BODY = 6000
TITLES = {
    "awaiting_user": "waiting for your decision",
    "error": "stopped with an error",
    "done": "done",
    "test": "test notice",
    "custom": "notice",
}
CHAT_NOTE = ("If you are in a NORMAL CONVERSATION (Claude chat with /orchestrator): do NOT run the script. "
             "Answer straight in the chat; the orchestrator operates normally and carries on.")


def clip(text, size):
    text = str(text or "").strip()
    return text if len(text) <= size else text[: size - 3] + "..."


# ---------------------------------------------------------------- common parts of the message
def command_base(ctx):
    return 'python "%s" "%s"%s' % (ctx.get("script", "<run_loop.py>"), ctx.get("repo", "<repo>"), ctx.get("flags", ""))


def numbered(options, limit=6):
    """Numbered options: "1) try again | 2) skip and carry on"."""
    return " | ".join("%d) %s" % (number, option) for number, option in enumerate(options[:limit], 1))


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


# ---------------------------------------------------------------- "full" format: plain text (file, console, webhook, toast)
def build(ctx):
    event = ctx.get("event", "custom")
    subject = subject_of(ctx)
    if event == "custom":
        return subject, clip("%s\n\nHOW TO CONTINUE\nOrigin: normal conversation. %s" % (ctx.get("extra_body") or "", CHAT_NOTE), MAX_BODY)
    out = [" | ".join(meta_line(ctx))]
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
    if commands:
        out += ["", "HOW TO CONTINUE",
                "1) Loop via the script (the case of this notice): run one of the commands marked with >> on your PC. The first answers by option "
                "NUMBER (change the 1); the second takes free text (change the text in quotes)."]
        out += [">> " + line for line in commands]
        out += ["", "2) " + CHAT_NOTE]
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
    if commands:
        out += [heading("How to continue"),
                '<p style="margin:0 0 4px"><b>1) Loop via the script</b> (the case of this notice): run one of the commands on your PC. The first answers '
                'by <b>option number</b> (change the 1); the second takes free text. The script records the answer and the loop carries on by itself.</p>', code(commands),
                '<p style="margin:0 0 8px"><b>2) Normal conversation (Claude chat with /orchestrator):</b> do not run the script. '
                'Answer straight in the chat; the orchestrator operates normally and carries on.</p>']
        if ctx.get("worktree") and event == "awaiting_user":
            out += [para("Want to talk before deciding? Open Claude inside the loop worktree:"),
                    code(['cd "%s"' % ctx["worktree"], "claude", "/orchestrator"])]
    if ctx.get("graph") and event != "test":
        out += [heading("Progress"), '<pre style="background:#f3f4f6;padding:10px 12px;border-radius:6px;font-family:Consolas,Menlo,monospace;font-size:12.5px;line-height:1.45;white-space:pre-wrap">%s</pre>' % e(clip(ctx["graph"], 1800))]
    if ctx.get("summary"):
        out.append('<p style="margin:14px 0 0;color:#6b7280">Full summary: %s</p>' % e(ctx["summary"]))
    out.append("</div>")
    return subject, "".join(out)


# ---------------------------------------------------------------- local channels
def channel_console(subject, body, ctx, work):
    print("\n*** %s ***\n%s\n" % (subject, body))
    return True, "ok"


def channel_file(subject, body, ctx, work):
    if not work:
        return False, "no work folder"
    folder = os.path.join(work, ".specs", "loop-logs")
    os.makedirs(folder, exist_ok=True)
    with open(os.path.join(folder, "NOTICE.txt"), "w", encoding="utf-8") as handle:
        handle.write("%s\n%s\n\n%s\n" % (datetime.datetime.now().isoformat(timespec="seconds"), subject, body))
    return True, "NOTICE.txt"


def channel_toast(subject, body, ctx, work):
    if not IS_WIN:
        return False, "toast is Windows-only"
    safe = lambda text: str(text).replace("'", "").replace("\n", " ")[:200]
    first = body.splitlines()[0] if body else ""
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
    """Email through a generic SMTP server (LOOP_SMTP_*), structured HTML with the command in a code block."""
    to = envload.get_list("LOOP_NOTIFY_TO")
    host = envload.get("LOOP_SMTP_HOST")
    port = int(envload.get("LOOP_SMTP_PORT") or 587)
    user, password = envload.get("LOOP_SMTP_USER"), envload.get("LOOP_SMTP_PASSWORD")
    sender = envload.get("LOOP_SMTP_FROM")
    security = (envload.get("LOOP_SMTP_SECURITY") or "starttls").lower()
    message = MIMEText(body, "html", "utf-8")
    message["Subject"], message["From"], message["To"] = subject, sender, ", ".join(to)
    last = ""
    for _ in range(3):
        try:
            if security == "ssl":
                server = smtplib.SMTP_SSL(host, port, timeout=30, context=ssl.create_default_context())
            else:
                server = smtplib.SMTP(host, port, timeout=30)
            with server:
                if security == "starttls":
                    server.starttls(context=ssl.create_default_context())
                if user:
                    server.login(user, password)
                server.sendmail(sender, to, message.as_string())
            return True, "sent to %s" % ", ".join(to)
        except smtplib.SMTPAuthenticationError as error:
            return False, "SMTP authentication failed (%s)" % error.smtp_code  # retrying will not help
        except (smtplib.SMTPException, OSError) as error:
            last = str(error)[:150].replace(password, "***") if password else str(error)[:150]
            time.sleep(3)
    return False, "no response from the SMTP server: " + last


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


# name -> (function, message format, required variables)
CHANNELS = {
    "console": (channel_console, "full", []),
    "toast": (channel_toast, "full", []),
    "file": (channel_file, "full", []),
    "email": (channel_email, "html", ["LOOP_SMTP_HOST", "LOOP_SMTP_FROM", "LOOP_NOTIFY_TO"]),
    "webhook": (channel_webhook, "full", ["LOOP_WEBHOOK_URL"]),
}


def missing_config(name):
    """Variables the channel is missing to work (empty list = configured)."""
    entry = CHANNELS.get(name)
    return [var for var in (entry[2] if entry else []) if not envload.get(var)]


def dispatch(ctx, work="", only=None):
    """Sends through the configured channels. Never raises. Returns {channel: 'ok: ...' | 'FAILED: ...' | 'SKIPPED: ...'}."""
    envload.load()
    built = {"full": build(ctx), "html": build_html(ctx)}
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

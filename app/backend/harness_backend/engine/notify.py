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
import re
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
    "awaiting_user": "Aguardando sua decisão",
    "error": "Execução interrompida com erro",
    "paused": "Execução pausada: limite de custo",
    "done": "Execução concluída",
    "test": "Teste de notificação",
    "custom": "Aviso",
}
APP_HARNESS = os.path.abspath(os.path.join(HERE, "..", "..", "..", "harness.py"))
CHAT_NOTE = ("Se você está numa CONVERSA NORMAL (chat do Claude com /orchestrator): NÃO rode o script. "
             "Responda direto no chat; o orquestrador segue normalmente.")


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
    return ctx.get("event") in ("awaiting_user", "error", "paused")


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
    if event in ("error", "paused"):
        return [base]
    return []


def subject_of(ctx):
    if ctx.get("event") == "custom":
        return ctx.get("subject") or "[loop] aviso"
    return "[Claude-Maestro] %s" % TITLES.get(ctx.get("event", "custom"), ctx.get("event"))


# ---------------------------------------------------------------- the notice, as data (portuguese, one structure for every channel)
FEATURE_GROUPS = (("finalizadas", ("verified",)), ("com especificação aprovada", ("specced", "approved")), ("em execução", ("building",)),
                  ("em especificação", ("speccing",)), ("pendentes", ("pending",)), ("bloqueadas", ("blocked",)))
DIRTY_TREE = re.compile(r"dirty tree outside \.specs/ (?:in repo (\S+) )?before turn (\d+)[^:]*:\s*(.*?)(?:\.\s*In the panel use.*)?$", re.S)
TURN_TIMEOUT = re.compile(r"turn (\d+) exceeded (\d+) min")
COST_CAP = re.compile(r"total cost cap exceeded: US\$ ([\d.]+) > ([\d.]+)")  # old projects: the cap hit was an error
COST_CAP_PAUSE = re.compile(r"cost cap reached: US\$ ([\d.]+) of ([\d.]+)")
CRITICAL_DECISION = "critical decision: "
PORCELAIN = re.compile(r"^\s*[A-Z?!]{1,2}\s+(.*)$")


def loop_id(ctx):
    return str(ctx.get("branch") or "").split("/", 1)[-1] or "-"


def feature_counts(ctx):
    """(closed, total, [(label, count)]) of the features by state; empty groups are left out."""
    statuses = [status for _, status in ctx.get("features") or []]
    groups = [(label, sum(1 for s in statuses if s in names)) for label, names in FEATURE_GROUPS]
    return sum(1 for s in statuses if s == "verified"), len(statuses), [(label, n) for label, n in groups if n]


def explain_reason(reason):
    """(paragraph, bullets): the stop reason in plain portuguese; what is not recognised is shown as it came."""
    text = str(reason or "").strip()
    dirty = DIRTY_TREE.search(text)
    if dirty:
        files = [PORCELAIN.sub(r"\1", row.strip()) for row in dirty.group(3).split(";") if row.strip()]
        where = " no repositório %s" % dirty.group(1) if dirty.group(1) else ""
        return ("A execução parou no turno %s porque foram detectadas alterações não salvas (dirty tree) fora da pasta .specs/%s. Como esses arquivos "
                "estão fora dos caminhos das features em andamento, o sistema não fez o reset automático. No painel, escolha commitar ou descartar." % (dirty.group(2), where),
                ["Arquivos com alterações não commitadas:"] + files)
    timeout = TURN_TIMEOUT.search(text)
    if timeout:
        return "O turno %s passou do limite de %s minutos e foi encerrado. Aumente o tempo do turno ou divida a feature." % (timeout.group(1), timeout.group(2)), []
    reached = COST_CAP_PAUSE.search(text)
    if reached:
        return ("O loop foi pausado: o custo total (US$ %s) atingiu o limite de US$ %s. Nada foi perdido; aumente o limite no painel para continuar."
                % (reached.group(1), reached.group(2)), [])
    cap = COST_CAP.search(text)
    if cap:
        return "O custo total (US$ %s) passou do limite configurado (US$ %s). Aumente o limite se quiser continuar." % (cap.group(1), cap.group(2)), []
    return clip(text, 1500), []


def compose_notice(ctx):
    """{title, session, progress, sections, how} for the event; the three formats only draw it."""
    event = ctx.get("event", "custom")
    closed, total, groups = feature_counts(ctx)
    session = [("ID do loop", loop_id(ctx))]
    if ctx.get("turn"):
        session.append(("Turno", str(ctx["turn"])))
    if ctx.get("cost") is not None:
        session.append(("Custo atual", "US$ %.2f" % float(ctx["cost"])))
    session.append(("Quando", datetime.datetime.now().strftime("%d/%m/%Y %H:%M")))
    if ctx.get("monitor_url") and not needs_continue_block(ctx):  # a stop that needs you carries the link in "Como continuar"
        session.append(("Painel", ctx["monitor_url"]))
    progress = ("Status das features (%d de %d concluídas)" % (closed, total), ["%d %s" % (n, label) for label, n in groups]) if total and event != "test" else None
    notice = {"title": subject_of(ctx), "session": session, "sections": [], "how": None, "progress": progress}
    sections = notice["sections"]
    if event == "test":
        sections.append(("Teste", "Este é um aviso de TESTE do loop. Nada precisa ser feito.", []))
    elif event in ("error", "paused"):
        paragraph, bullets = explain_reason(ctx.get("reason"))
        sections.append(("Motivo da parada", paragraph, bullets))
    elif event == "awaiting_user" and str(ctx.get("reason") or "").startswith(CRITICAL_DECISION):
        options = [str(o) for o in ctx.get("options") or []]
        sections.append(("Decisão crítica (critical decision) precisa da sua resposta", clip(ctx["reason"], 1500), ["Opção %d: %s" % (n, o) for n, o in enumerate(options[:6], 1)]))
    elif event == "awaiting_user":
        options = [str(o) for o in ctx.get("options") or []]
        sections.append(("Decisão necessária", clip(ctx.get("reason") or "(sem texto: veja o resumo)", 1500), ["Opção %d: %s" % (n, o) for n, o in enumerate(options[:6], 1)]))
    elif event == "done":
        lines = []
        if ctx.get("push"):
            lines.append("Push da branch: " + clip(ctx["push"], 200))
        if ctx.get("pr"):
            lines.append("PR: " + clip(ctx["pr"], 300) + (" (%s)" % ctx["pr_status"] if ctx.get("pr_status") else ""))
        sections.append(("Resultado", "Todas as features foram concluídas ou bloqueadas.", lines))
        if ctx.get("validate"):
            sections.append(("Para você validar (decidido sem parar você)", None, [clip(i, 200) for i in ctx["validate"][:8]]))
        if ctx.get("commits"):
            sections.append(("Commits", None, [clip(line, 120) for line in ctx["commits"][:8]]))
    if ctx.get("to_check") and event in ("awaiting_user", "error", "paused"):
        sections.append(("Conferir antes de responder", None, [clip(i, 200) for i in ctx["to_check"][:8]]))
    if needs_continue_block(ctx):
        notice["how"] = ("Para resolver, responder, retomar ou aprovar, acesse o painel de controle:" if ctx.get("monitor_url")
                         else "Para continuar, acesse o painel de controle abrindo-o com o comando:")
    return notice


def panel_value(ctx):
    return ctx.get("monitor_url") or 'python "%s" serve --project "%s"' % (APP_HARNESS, ctx.get("repo", "<repo>"))


# ---------------------------------------------------------------- "full" format: plain text (file, console, webhook)
def build(ctx):
    event = ctx.get("event", "custom")
    subject = subject_of(ctx)
    if event == "custom":
        return subject, clip("%s\n\nCOMO CONTINUAR\nOrigem: conversa normal. %s" % (ctx.get("extra_body") or "", CHAT_NOTE), MAX_BODY)
    n = compose_notice(ctx)
    out = [subject, "", "Detalhes da sessão"] + ["- %s: %s" % pair for pair in n["session"]]
    if n["progress"]:
        out += ["", n["progress"][0]] + ["- " + line for line in n["progress"][1]]
    for heading, paragraph, items in n["sections"]:
        out += ["", heading]
        if paragraph:
            out.append(paragraph)
        out += [i if i.endswith(":") else "- " + i for i in items]
    if ctx.get("graph") and event != "test":
        out += ["", "Andamento", clip(ctx["graph"], 1800)]
    commands = command_set(ctx)
    if continue_in_panel():
        if n["how"]:
            out += ["", "Como continuar", n["how"], panel_value(ctx)]
    elif commands:
        out += ["", "Como continuar",
                "1) Loop pelo script: rode um dos comandos marcados com >> no seu PC. O primeiro responde pelo NÚMERO da opção; o segundo aceita texto livre."]
        out += [">> " + line for line in commands]
        out += ["", "2) " + CHAT_NOTE]
        if ctx.get("monitor_url"):
            out += ["", "3) Responder no painel: " + ctx["monitor_url"]]
    if ctx.get("summary"):
        out += ["", "Resumo completo: " + ctx["summary"]]
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
    out = ['<div style="font-family:Segoe UI,Arial,sans-serif;font-size:14px;color:#1f2937;max-width:680px">', '<h2 style="margin:0 0 8px;font-size:18px">%s</h2>' % e(subject)]
    if event == "custom":
        out.append('<div style="white-space:pre-wrap;line-height:1.45">%s</div>' % e(ctx.get("extra_body") or ""))
        out += [heading("Como continuar"), para("Origem: conversa normal. " + CHAT_NOTE), "</div>"]
        return subject, "".join(out)
    n = compose_notice(ctx)
    out += [heading("Detalhes da sessão"), bullets(["%s: %s" % pair for pair in n["session"]])]
    if n["progress"]:
        out += [heading(n["progress"][0]), bullets(n["progress"][1])]
    for title, paragraph, items in n["sections"]:
        out.append(heading(title))
        if paragraph:
            out.append(para(paragraph))
        labels = [i for i in items if i.endswith(":")]
        out += [para(i) for i in labels]
        if len(labels) != len(items):
            out.append(bullets([i for i in items if not i.endswith(":")]))
    if ctx.get("graph") and event != "test":
        out += [heading("Andamento"), '<pre style="background:#f3f4f6;padding:10px 12px;border-radius:6px;font-family:Consolas,Menlo,monospace;font-size:12.5px;line-height:1.45;white-space:pre-wrap">%s</pre>' % e(clip(ctx["graph"], 1800))]
    commands = command_set(ctx)
    if continue_in_panel():
        if n["how"]:
            link = '<p style="margin:0 0 8px"><a href="%s">%s</a></p>' % (e(ctx["monitor_url"]), e(ctx["monitor_url"])) if ctx.get("monitor_url") else ""
            out += [heading("Como continuar"), para(n["how"]), link or code([panel_value(ctx)])]
    elif commands:
        out += [heading("Como continuar"),
                '<p style="margin:0 0 4px"><b>1) Loop pelo script:</b> rode um dos comandos no seu PC. O primeiro responde pelo <b>número da opção</b>; o segundo aceita texto livre.</p>', code(commands),
                '<p style="margin:0 0 8px"><b>2) Conversa normal (chat do Claude com /orchestrator):</b> não rode o script; responda direto no chat.</p>']
        if ctx.get("monitor_url"):
            out.append('<p style="margin:0 0 8px"><b>3) Responder no painel:</b> <a href="%s">%s</a></p>' % (e(ctx["monitor_url"]), e(ctx["monitor_url"])))
    if ctx.get("summary"):
        out.append('<p style="margin:14px 0 0;color:#6b7280">Resumo completo: %s</p>' % e(ctx["summary"]))
    out.append("</div>")
    return subject, "".join(out)


# ---------------------------------------------------------------- "short" format: size-limited channels (WhatsApp, toast)
def build_short(ctx):
    """Short text, WhatsApp style (*bold*), up to ~950 characters; the panel link always survives."""
    event = ctx.get("event", "custom")
    title = subject_of(ctx)
    if event == "custom":
        body = "*%s*\n%s\n\n*COMO CONTINUAR*\nOrigem: conversa normal. Responda direto no chat do Claude; sem script." % (title, clip(ctx.get("extra_body"), 600))
        return title, clip(body, MAX_SHORT)
    n = compose_notice(ctx)
    closed, total, _ = feature_counts(ctx)

    def compose(size, with_extra):
        lines = ["*%s*" % title, " | ".join("%s %s" % pair for pair in n["session"] if pair[0] != "Quando")]
        if total and event != "test":
            lines.append("Features: %d de %d concluídas" % (closed, total))
        for heading, paragraph, items in n["sections"]:
            lines += ["", "*%s:*" % heading]
            if paragraph:
                lines.append(clip(paragraph, size))
            shown = items[: (4 if with_extra else 2)]
            lines += [clip(i, max(size // 2, 40)) for i in shown]
            if len(items) > len(shown):
                lines.append("(+%d)" % (len(items) - len(shown)))
        if with_extra and ctx.get("graph_short") and event in ("awaiting_user", "error"):
            lines += ["", clip(ctx["graph_short"], 200)]
        if continue_in_panel():
            if n["how"]:
                lines += ["", "*Como continuar*", "Acesse o painel: " + panel_value(ctx)]
        elif event in ("awaiting_user", "error", "test"):
            answer = " --option 1" if (ctx.get("options") and event == "awaiting_user") else ' --answer "SUA MENSAGEM"'
            command = command_base(ctx) + ("" if event == "error" else answer)
            lines += ["", "*Como continuar*", "1) _Loop pelo script_ (rode no PC" + (", troque o 1 pelo número da opção):" if "--option" in command else "):"),
                      "```\n" + command + "\n```", "2) _Conversa normal no chat do Claude_: responda direto no chat, sem script."]
            if ctx.get("monitor_url"):
                lines.append("3) _Painel_: responda em " + ctx["monitor_url"])
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

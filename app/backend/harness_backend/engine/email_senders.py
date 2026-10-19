"""Email providers behind the `email` notification channel (engine/notify.py). Stdlib only.

`channel_email` builds the message and hands it to one `IEmailSender`; which one is chosen by LOOP_EMAIL_PROVIDER:
  smtp (default)  generic SMTP server: LOOP_SMTP_HOST / PORT / USER / PASSWORD / FROM / SECURITY
  http            a mail service reached by POST:
                  LOOP_EMAIL_HTTP_URL, optional LOOP_EMAIL_HTTP_TOKEN (sent as `Authorization: Bearer <token>`) and
                  LOOP_EMAIL_HTTP_FROM. Body: JSON {to: [..], subject, html, from?}.
Recipients are always LOOP_NOTIFY_TO. A new provider = one class with `send` + one entry in PROVIDERS.
"""
import json
import smtplib
import ssl
import time
import urllib.error
import urllib.request
from typing import Protocol

from app_paths import envload

DEFAULT_PROVIDER = "smtp"
DEFAULT_SMTP_PORT = 587
REQUEST_TIMEOUT_SECONDS = 30
SEND_ATTEMPTS = 3
RETRY_WAIT_SECONDS = 3
ERROR_DETAIL_LIMIT = 150
SERVER_ANSWER_LIMIT = 200


class IEmailSender(Protocol):
    def send(self, recipients, subject, html_body) -> tuple: ...  # (ok, detail)


class SmtpEmailSender:
    def send(self, recipients, subject, html_body):
        from email.mime.text import MIMEText
        host = envload.get("LOOP_SMTP_HOST")
        port = int(envload.get("LOOP_SMTP_PORT") or DEFAULT_SMTP_PORT)
        user, password = envload.get("LOOP_SMTP_USER"), envload.get("LOOP_SMTP_PASSWORD")
        sender = envload.get("LOOP_SMTP_FROM")
        security = (envload.get("LOOP_SMTP_SECURITY") or "starttls").lower()
        message = MIMEText(html_body, "html", "utf-8")
        message["Subject"], message["From"], message["To"] = subject, sender, ", ".join(recipients)
        last = ""
        for _ in range(SEND_ATTEMPTS):
            try:
                if security == "ssl":
                    server = smtplib.SMTP_SSL(host, port, timeout=REQUEST_TIMEOUT_SECONDS, context=ssl.create_default_context())
                else:
                    server = smtplib.SMTP(host, port, timeout=REQUEST_TIMEOUT_SECONDS)
                with server:
                    if security == "starttls":
                        server.starttls(context=ssl.create_default_context())
                    if user:
                        server.login(user, password)
                    server.sendmail(sender, recipients, message.as_string())
                return True, "sent to %s" % ", ".join(recipients)
            except smtplib.SMTPAuthenticationError as error:
                return False, "SMTP authentication failed (%s)" % error.smtp_code  # retrying will not help
            except (smtplib.SMTPException, OSError) as error:
                last = str(error)[:ERROR_DETAIL_LIMIT].replace(password, "***") if password else str(error)[:ERROR_DETAIL_LIMIT]
                time.sleep(RETRY_WAIT_SECONDS)
        return False, "no response from the SMTP server: " + last


class HttpEmailSender:
    def send(self, recipients, subject, html_body):
        url = envload.get("LOOP_EMAIL_HTTP_URL")
        token = envload.get("LOOP_EMAIL_HTTP_TOKEN")
        sender = envload.get("LOOP_EMAIL_HTTP_FROM")
        payload = {"to": list(recipients), "subject": subject, "html": html_body}
        if sender:
            payload["from"] = sender
        headers = {"Content-Type": "application/json"}
        if token:
            headers["Authorization"] = "Bearer " + token
        request = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"), method="POST", headers=headers)
        last = ""
        for _ in range(SEND_ATTEMPTS):
            try:
                with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
                    return 200 <= response.status < 300, "HTTP %d, sent to %s" % (response.status, ", ".join(recipients))
            except urllib.error.HTTPError as error:
                answer = error.read().decode("utf-8", "replace")[:SERVER_ANSWER_LIMIT]
                return False, "HTTP %d %s" % (error.code, answer.replace(token, "***") if token else answer)  # answered: no retry
            except (urllib.error.URLError, OSError) as error:
                last = str(error)[:ERROR_DETAIL_LIMIT]
                time.sleep(RETRY_WAIT_SECONDS)
        return False, "no response from the email service: " + last


# provider -> (sender class, variables it needs: a list besides LOOP_NOTIFY_TO, or a function returning the whole list,
#              function returning the recipients or None for LOOP_NOTIFY_TO).
# Local extensions (engine/local_ext/, never committed) add providers here: see notify.load_local_extensions().
PROVIDERS = {
    "smtp": (SmtpEmailSender, ["LOOP_SMTP_HOST", "LOOP_SMTP_FROM"], None),
    "http": (HttpEmailSender, ["LOOP_EMAIL_HTTP_URL"], None),
}


def chosen_provider():
    return (envload.get("LOOP_EMAIL_PROVIDER") or DEFAULT_PROVIDER).strip().lower()


def required_variables():
    """Variables the chosen provider needs (an unknown provider needs LOOP_EMAIL_PROVIDER to be fixed)."""
    entry = PROVIDERS.get(chosen_provider())
    if entry is None:
        return ["LOOP_NOTIFY_TO", "LOOP_EMAIL_PROVIDER (%s)" % "|".join(PROVIDERS)]
    return entry[1]() if callable(entry[1]) else ["LOOP_NOTIFY_TO"] + entry[1]


def recipients():
    entry = PROVIDERS.get(chosen_provider())
    return entry[2]() if entry and entry[2] else envload.get_list("LOOP_NOTIFY_TO")


def build_email_sender():
    entry = PROVIDERS.get(chosen_provider())
    return entry[0]() if entry else None

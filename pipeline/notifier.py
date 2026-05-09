"""
notifier.py
Invia notifiche email al termine dei task automatici del venerdi'.

Utilizzo:
  python -m pipeline.notifier --subject "Testo" --body "Corpo" [--error]
"""

from __future__ import annotations

import argparse
import smtplib
import ssl
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from datetime import datetime


# ---------------------------------------------------------------------------
# Configurazione — compilata da run_friday.ps1 / run_scrape.ps1
# ---------------------------------------------------------------------------
SMTP_HOST = "smtp.gmail.com"
SMTP_PORT = 587
FROM_ADDR = "serena.sciannamea@glovoapp.com"
TO_ADDR   = "serena.sciannamea@glovoapp.com"


def send_email(
    subject: str,
    body: str,
    app_password: str,
    is_error: bool = False,
) -> None:
    """Invia una email di notifica via Gmail SMTP."""
    icon    = "❌" if is_error else "✅"
    subject = f"{icon} PromoParity — {subject}"

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = FROM_ADDR
    msg["To"]      = TO_ADDR

    # Versione plain text
    msg.attach(MIMEText(body, "plain", "utf-8"))

    # Versione HTML (piu' leggibile su mobile)
    color  = "#ef4444" if is_error else "#22c55e"
    border = "#fca5a5" if is_error else "#86efac"
    html = f"""
    <div style="font-family:sans-serif;max-width:600px;margin:0 auto">
      <div style="background:{color};color:white;padding:16px 20px;border-radius:8px 8px 0 0">
        <h2 style="margin:0;font-size:18px">{icon} PromoParity — {subject.split(' — ',1)[-1]}</h2>
      </div>
      <div style="border:1px solid {border};padding:20px;border-radius:0 0 8px 8px;white-space:pre-wrap;font-size:14px;line-height:1.6">
{body}
      </div>
      <p style="color:#94a3b8;font-size:12px;margin-top:8px">
        Inviato automaticamente da PromoParity · {datetime.now().strftime('%d/%m/%Y %H:%M')}
      </p>
    </div>
    """
    msg.attach(MIMEText(html, "html", "utf-8"))

    context = ssl.create_default_context()
    with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
        server.ehlo()
        server.starttls(context=context)
        server.login(FROM_ADDR, app_password)
        server.sendmail(FROM_ADDR, TO_ADDR, msg.as_string())


def read_last_log_lines(log_path: Path, n: int = 30) -> str:
    """Legge le ultime N righe del log."""
    if not log_path.exists():
        return "(log non trovato)"
    lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
    return "\n".join(lines[-n:])


def main() -> None:
    parser = argparse.ArgumentParser(description="Invia notifica email PromoParity")
    parser.add_argument("--subject",      required=True)
    parser.add_argument("--body",         default="")
    parser.add_argument("--app-password", required=True)
    parser.add_argument("--log",          default="", help="Path al file di log (ultime righe allegate)")
    parser.add_argument("--error",        action="store_true", help="Segnala come errore")
    args = parser.parse_args()

    body = args.body
    if args.log:
        log_excerpt = read_last_log_lines(Path(args.log), n=40)
        body += f"\n\n{'='*50}\nUltime righe del log:\n{'='*50}\n{log_excerpt}"

    send_email(
        subject      = args.subject,
        body         = body,
        app_password = args.app_password,
        is_error     = args.error,
    )
    print(f"[notifier] Email inviata: {args.subject}")


if __name__ == "__main__":
    main()

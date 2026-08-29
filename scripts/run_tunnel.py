"""
Runs a Cloudflare Quick Tunnel pointed at the local manga tracker (localhost:8080)
and emails the generated public URL whenever a new one is issued (Quick Tunnel
URLs are random and change every time this process restarts).

Usage: venv\\Scripts\\python.exe scripts\\run_tunnel.py
Meant to be launched by a Windows Scheduled Task at logon so it restarts
automatically (with a fresh URL + a fresh email) after a reboot or crash.
"""
import os
import re
import shutil
import smtplib
import subprocess
import sys
from email.message import EmailMessage
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(PROJECT_ROOT / ".env")

URL_RE = re.compile(r"https://[a-zA-Z0-9-]+\.trycloudflare\.com")
CURRENT_URL_FILE = PROJECT_ROOT / "logs" / "tunnel_current_url.txt"

FALLBACK_CLOUDFLARED = r"C:\Program Files (x86)\cloudflared\cloudflared.exe"


def find_cloudflared():
    found = shutil.which("cloudflared")
    if found:
        return found
    if os.path.exists(FALLBACK_CLOUDFLARED):
        return FALLBACK_CLOUDFLARED
    raise RuntimeError("cloudflared.exe not found on PATH or in the default install location.")


def send_url_email(url):
    address = os.environ.get("NOTIFY_GMAIL_ADDRESS")
    app_password = os.environ.get("NOTIFY_GMAIL_APP_PASSWORD")
    recipient = os.environ.get("NOTIFY_GMAIL_TO", address)

    if not address or not app_password:
        print("[run_tunnel] NOTIFY_GMAIL_ADDRESS/NOTIFY_GMAIL_APP_PASSWORD not set in .env "
              "-- skipping email, tunnel will keep running.")
        return

    msg = EmailMessage()
    msg["Subject"] = "Manga Tracker - new remote access URL"
    msg["From"] = address
    msg["To"] = recipient
    msg.set_content(
        f"Your Cloudflare Quick Tunnel restarted and has a new public URL:\n\n{url}\n\n"
        "This changes every time the tunnel process restarts (reboot/crash)."
    )

    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as smtp:
            smtp.login(address, app_password)
            smtp.send_message(msg)
        print(f"[run_tunnel] Emailed new URL to {recipient}")
    except Exception as e:
        print(f"[run_tunnel] Failed to send notification email: {e}")


def main():
    sys.stdout.reconfigure(line_buffering=True)
    cloudflared = find_cloudflared()
    print(f"[run_tunnel] Using cloudflared at: {cloudflared}")
    print("[run_tunnel] Starting Quick Tunnel -> http://localhost:8080 ...")

    proc = subprocess.Popen(
        [cloudflared, "tunnel", "--url", "http://localhost:8080"],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )

    url_found = False
    for line in proc.stdout:
        print(line, end="")
        if not url_found:
            match = URL_RE.search(line)
            if match:
                url_found = True
                url = match.group(0)
                print(f"\n[run_tunnel] Public URL: {url}\n")
                CURRENT_URL_FILE.parent.mkdir(exist_ok=True)
                CURRENT_URL_FILE.write_text(url, encoding="utf-8")
                send_url_email(url)

    proc.wait()
    print(f"[run_tunnel] cloudflared exited with code {proc.returncode}")
    sys.exit(proc.returncode)


if __name__ == "__main__":
    main()

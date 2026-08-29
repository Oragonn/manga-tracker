go into the venv
manga-tracker\venv\Scripts\Activate.ps1

Start the server
python -m backend.main

Check backup status
bashpython -c "from backend.backup_manager import BackupManager; import json; bm = BackupManager(); print(json.dumps(bm.get_backup_stats(), indent=2, default=str))"

Create manual backup
bashpython -c "from backend.backup_manager import BackupManager; bm = BackupManager(); bm.create_backup()"

Restore from backup
python -c "from backend.backup_manager import BackupManager; bm = BackupManager(); bm.restore_backup('tracker_backup_20241213_180000.db.gz')"

## Remote access setup

The app requires a `.env` file (copy `.env.example` to `.env`) with two secrets before it will start:

```
python -c "import secrets; print(secrets.token_hex(32))"          # -> SECRET_KEY
python -c "from werkzeug.security import generate_password_hash; print(generate_password_hash('your-password-here'))"   # -> AUTH_PASSWORD_HASH
```

Paste the two outputs into `.env` as `SECRET_KEY=...` and `AUTH_PASSWORD_HASH=...`. Devices on the home LAN (192.168.0.0/16 / 10.0.0.0/8 / 172.16.0.0/12 by default, see `LAN_CIDR_RANGES` in `.env.example`) are never prompted to log in; everything else needs the password from `/login`.

To reach the app from outside the house, without opening a port on your router, without installing anything on the devices you'll browse from, and without owning a domain, this uses Cloudflare's free **Quick Tunnel** (`cloudflared` only runs on this machine, no Cloudflare account needed). The tradeoff: the public URL is randomly generated and changes every time the tunnel process restarts (reboot, crash), unlike a paid/domain-backed named tunnel which would keep a fixed address.

1. `cloudflared` is installed (via `winget install Cloudflare.cloudflared`).
2. `scripts/run_tunnel.py` launches `cloudflared tunnel --url http://localhost:8080`, watches its output for the generated `https://<random>.trycloudflare.com` URL, writes it to `logs/tunnel_current_url.txt`, and emails it to you (see `NOTIFY_GMAIL_*` in `.env.example` — needs a [Gmail App Password](https://myaccount.google.com/apppasswords), not your real password) so you have the current URL even while away from home.
3. To have it start automatically on login/reboot, create a Windows Scheduled Task:
   - Trigger: "At log on"
   - Action: `C:\Users\<you>\Desktop\manga-tracker\venv\Scripts\python.exe` with arguments `scripts\run_tunnel.py` and "Start in" set to the project folder
   - On the task's Settings tab, enable "If the task fails, restart every: 1 minute" so it recovers automatically if `cloudflared` ever drops.
4. Anyone hitting the printed URL gets the same login gate as everyone off the LAN — the Quick Tunnel doesn't bypass any of the auth work above.

Nothing above requires a paid plan, and no tunnel credentials belong in this repo.
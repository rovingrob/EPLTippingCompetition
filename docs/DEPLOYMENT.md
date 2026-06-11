# Deployment

This setup assumes Cloudflare Tunnel forwards your public hostname to `http://127.0.0.1:8000` on the server. The FastAPI app should bind only to loopback; do not expose port `8000` directly to the internet.

## Install

From the repo:

```bash
chmod +x deploy/install-systemd.sh
./deploy/install-systemd.sh
```

The installer:

- copies this repo to `/opt/world-cup-tipping` by default
- runs `uv sync --frozen`
- creates `/var/lib/world-cup-tipping/data`
- copies existing JSON data into that data directory if it is missing there
- creates `/etc/world-cup-tipping.env` with generated secrets if it does not exist
- installs `world-cup-tipping.service`
- installs `world-cup-tipping-cron.service` and `world-cup-tipping-cron.timer`
- reloads systemd without starting anything

The `/opt/world-cup-tipping` default matters on Fedora and other SELinux-enforcing systems. System services are often blocked from executing virtualenv scripts directly under `/home`, even when Unix permissions look correct.

Start the app and timer when you are ready:

```bash
sudo systemctl enable --now world-cup-tipping.service world-cup-tipping-cron.timer
```

If you previously installed a unit that points at `/home/.../world_cup_tipping/.venv/bin/uvicorn` and it fails with `status=203/EXEC`, rerun the installer from the repo, then restart:

```bash
./deploy/install-systemd.sh
sudo systemctl restart world-cup-tipping.service
```

Check status:

```bash
sudo systemctl status world-cup-tipping.service
sudo systemctl status world-cup-tipping-cron.timer
curl http://127.0.0.1:8000/tipping/healthz
```

Read logs:

```bash
sudo journalctl -u world-cup-tipping.service -f
sudo journalctl -u world-cup-tipping-cron.service -f
```

## Secrets

The admin login token is stored in `/etc/world-cup-tipping.env`.

Production must set all of these values:

```text
ADMIN_TOKEN=<long random admin login token>
ADMIN_COOKIE_SECRET=<different long random cookie secret>
ADMIN_COOKIE_SECURE=true
```

Never reuse `ADMIN_TOKEN` as `ADMIN_COOKIE_SECRET`, and never commit
`/etc/world-cup-tipping.env`, `.env`, or generated secrets.

```bash
sudo awk -F= '/^ADMIN_TOKEN=/{print $2}' /etc/world-cup-tipping.env
```

For production, keep `ADMIN_COOKIE_SECURE=true` so the encrypted admin cookie is only sent over HTTPS. If you test directly over plain local HTTP, temporarily set it to `false` and restart the service.

After editing the env file:

```bash
sudo systemctl restart world-cup-tipping.service
```

## Cloudflare Tunnel

Use `deploy/cloudflared-tunnel.example.yml` as the shape for your tunnel ingress:

```yaml
ingress:
  - hostname: tipping.example.com
    service: http://127.0.0.1:8000
  - service: http_status:404
```

Keep your server firewall closed to public port `8000`. Binding uvicorn to `127.0.0.1` is the main protection; a firewall rule is a useful backup.

## Admin Protection

Use Cloudflare Access for `/tipping/admin*` in front of the app. The app still
requires `ADMIN_TOKEN`, but Access gives you an outer identity layer before the
login page is reachable. Treat this as a production requirement, not an
optional hardening step.

Recommended production env values:

```text
ADMIN_COOKIE_SECURE=true
WCT_ENABLE_HSTS=true
WCT_ALLOWED_HOSTS=tipping.example.com,localhost,127.0.0.1
```

Set `WCT_ALLOWED_HOSTS` only after confirming the exact hostname Cloudflare sends to the origin. If it is wrong, FastAPI will reject requests with `400 Invalid host header`.

## Cron

The timer runs the workflow every 4 hours and checks fixtures 24 hours in advance:

```bash
python -m world_cup_tipping.cron run-due --data-dir /var/lib/world-cup-tipping/data --lookahead-hours 24
```

It records predictions for fixtures in the configured lookahead window and scores completed fixtures, including retrospective completed matches.

# Deployment

This deployment uses one FastAPI worker, JSON state, systemd timers, and a
Cloudflare Tunnel forwarding the public hostname to
`http://127.0.0.1:8000`. Do not expose port 8000 directly.

## Install

Install `uv`, clone the repository, then run:

```bash
chmod +x deploy/install-systemd.sh
./deploy/install-systemd.sh
```

By default the installer:

- copies the repository to `/opt/epl-tipping`;
- installs locked dependencies;
- creates `/var/lib/epl-tipping/data` and seeds missing JSON files;
- creates `/etc/epl-tipping.env` with generated admin secrets when absent;
- installs the app service and the due-workflow and simulation-worker timers;
- reloads systemd without starting the units.

The `/opt` location avoids common virtualenv execution restrictions on
SELinux-enforcing hosts.

Edit `/etc/epl-tipping.env` before starting the services. At minimum, set a
valid football-data.org token and the exact public hostname:

```text
FOOTBALL_DATA_TOKEN=<server-only API token>
TIPPING_COMPETITION_CODE=PL
TIPPING_SEASON=2026
TIPPING_COMPETITION_TIMEZONE=Australia/Sydney
TIPPING_ALLOWED_HOSTS=tipping.example.com,localhost,127.0.0.1
```

`TIPPING_COMPETITION_TIMEZONE` controls competition-wide rules such as the
public simulation daily limit. Fixture timestamps and Today-page date grouping
use each visitor's browser timezone.

Start the app and timers:

```bash
sudo systemctl enable --now epl-tipping.service epl-tipping-cron.timer epl-tipping-simulation.timer
```

Check status and health:

```bash
sudo systemctl status epl-tipping.service
sudo systemctl status epl-tipping-cron.timer
sudo systemctl status epl-tipping-simulation.timer
curl http://127.0.0.1:8000/tipping/healthz
```

Read logs:

```bash
sudo journalctl -u epl-tipping.service -f
sudo journalctl -u epl-tipping-cron.service
sudo journalctl -u epl-tipping-simulation.service
```

## Secrets

Production requires:

```text
FOOTBALL_DATA_TOKEN=<football-data.org API token>
ADMIN_TOKEN=<long random admin login token>
ADMIN_COOKIE_SECRET=<different long random cookie secret>
ADMIN_COOKIE_SECURE=true
```

Never reuse the admin token as the cookie secret. Keep
`/etc/epl-tipping.env`, local `.env` files, and generated secrets outside git.
The installer preserves an existing environment file when redeployed and never
copies a repository-root `.env` into `/opt/epl-tipping`. Once the API token is in
`/etc/epl-tipping.env`, the local `.env` is not needed by the deployed service.

After editing it, restart the app; the next timer runs will use the new values:

```bash
sudo systemctl restart epl-tipping.service
```

## Cloudflare Tunnel and admin protection

Use `deploy/cloudflared-tunnel.example.yml` as the ingress shape and keep the
host firewall closed to public port 8000. Protect `/tipping/admin*` with
Cloudflare Access in addition to the application's admin login.

Recommended settings after HTTPS is working end to end:

```text
ADMIN_COOKIE_SECURE=true
TIPPING_ENABLE_HSTS=true
TIPPING_ALLOWED_HOSTS=tipping.example.com,localhost,127.0.0.1
```

An incorrect allow-list causes `400 Invalid host header`, so verify the hostname
Cloudflare forwards to the origin before enabling it.

## Scheduled workflows

`epl-tipping-cron.timer` runs every four hours. Each invocation requests the
configured season once, collects tips for scheduled fixtures from 24 hours until
30 minutes before kickoff, and scores completed fixtures. Source failures are
recorded while cached fixture processing continues.

`epl-tipping-simulation.timer` polls every minute and processes at most one
durably queued full-season simulation per invocation. The worker uses five
concurrent contestant calls, a 15-second timeout, and one retry.

Manual equivalents are:

```bash
/opt/epl-tipping/.venv/bin/python -m epl_tipping.cron run-due --data-dir /var/lib/epl-tipping/data
/opt/epl-tipping/.venv/bin/python -m epl_tipping.cron process-simulation --data-dir /var/lib/epl-tipping/data
```

## Updating

Run the installer again from the updated checkout. Existing environment and
runtime JSON files are preserved because seed files are copied only when the
destination file does not yet exist.

```bash
./deploy/install-systemd.sh
sudo systemctl restart epl-tipping.service
```

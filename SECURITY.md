# Security Policy

## Supported Versions

This project is an MVP. Security fixes are expected to land on the current main
branch only.

## Reporting A Vulnerability

If you find a vulnerability, please report it privately to the project
maintainer before opening a public issue. Include:

- affected version or commit
- steps to reproduce
- impact
- any suggested mitigation

Do not include live admin tokens, cookie secrets, contestant endpoint secrets,
or personal data in public issues, logs, screenshots, or pull requests.

## Production Requirements

Production deployments must set:

- `ADMIN_TOKEN`: a long random admin login token
- `ADMIN_COOKIE_SECRET`: a different long random secret for encrypted cookies
- `ADMIN_COOKIE_SECURE=true`: send admin cookies only over HTTPS
- `TIPPING_ALLOWED_HOSTS`: the public hostnames Cloudflare sends to the origin
- `TIPPING_ENABLE_HSTS=true`: once HTTPS is working end to end
- `FOOTBALL_DATA_TOKEN`: the server-only football-data.org API token

The FastAPI app should bind only to `127.0.0.1`. Put Cloudflare Tunnel in front
of it, and protect `/tipping/admin*` with Cloudflare Access.

## Known Risk Areas

- Admin endpoint validation makes server-side requests to contestant URLs.
  Treat admin access as trusted and keep Cloudflare Access enabled.
- The admin-only API tester sends browser requests to contestant endpoints.
  Contestant endpoints should use their own rate limits and CORS rules.
- Persistent state lives in JSON files. Keep `data/registry.json`,
  `data/predictions.json`, `data/scores.json`, `data/run_log.json`, and
  `data/season_simulations.json` free of private data before publishing.
- Never expose `FOOTBALL_DATA_TOKEN` to templates, public responses, logs, or
  contestant endpoints. The token belongs only in a protected deployment
  environment file.
- Contestant endpoints are untrusted remote services. Keep call timeouts,
  concurrency limits, and the single-worker JSON storage deployment model in
  place.

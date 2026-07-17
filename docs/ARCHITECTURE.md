# Architecture

The service runs one configured Premier League season at a time. Its four data
flows share a small, atomic JSON store protected by a cross-process lock.

## Fixture synchronisation

Every four hours, the due workflow requests
`GET /v4/competitions/PL/matches?season=2026` from football-data.org. Source
match IDs are the stable identity; kickoff, matchday, status, teams, and scores
remain mutable. Missing source rows are reported rather than locally deleted.
Score corrections invalidate derived score rows so they can be rebuilt.

Kickoffs remain UTC in storage and API payloads. Browsers render each
`time[data-utc]` value in their local timezone. The Today page sends that IANA
timezone back as `tz`, allowing the server to select fixtures using the user's
calendar date rather than the host or competition timezone.

## Live prediction and scoring

Scheduled, resolved fixtures enter the prediction window 24 hours before
kickoff and leave it 30 minutes before kickoff. Contestant calls are bounded and
retried once. The first valid home/away score is retained. Completed fixture
results are scored idempotently at 0, 1, or 1.5 points.

Public views redact predictions until the lock time. Admin pages can inspect
them earlier, explicitly reopen a stored prediction, and create or clear result
overrides.

## Full-season simulations

The web request writes a durable `queued` run and returns immediately. A systemd
timer invokes the worker, which transitions one run through `running` to
`completed` or `failed`. It combines actual completed scores with contestant
predictions for all remaining resolved fixtures and builds a table ordered by
points, goal difference, goals scored, then team name. Public requests are
limited by the competition's `Australia/Sydney` calendar day. Stale running jobs are failed
after their activity lease expires, and incomplete simulations retain explicit
omission details instead of silently presenting partial coverage as complete.

## Operational boundary

The current JSON design assumes a single app worker on one host. Source API and
contestant calls happen outside the storage lock; mutations use atomic file
replacement. A database and a persistent worker process are natural future
changes if concurrency or deployment topology grows.

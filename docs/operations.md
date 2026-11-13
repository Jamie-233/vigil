# Operations

Vigil monitors real services from a Mac mini on a home connection. This file
records what it watches, why, and what happens while it runs: incidents,
false alarms, and every change to thresholds or code that running it forced.

Template: [`deploy/targets.example.yml`](../deploy/targets.example.yml) · Install: `bash deploy/macos/install.sh` (runs from the menu bar; start at login is opt-in). The real target list stays on the monitoring host (`~/.vigil/vigil.yml`) and is not published.

## What is monitored

| Target | Check | Why |
|---|---|---|
| personal website | HTTP 200, TLS expiry, DNS | served through a CDN |
| attendance web app | HTTP 200, TLS expiry, DNS | no health endpoint; hostname depends on a third-party DNS provider |
| university portal | HTTP 200 every 5 min | external dependency; front page only, never logged in |
| public resolvers | ICMP | control targets, to tell a local uplink failure from a service failure |

## Known limitations

- One vantage point. A home outage or a Mac that goes to sleep shows up as
  missing data or as every target failing at once.
- Control targets are not yet used to suppress alerts; they are only there to
  read the dashboard correctly.

## Log

| Date | Event | Cause | Change |
|---|---|---|---|
| 2026-11-13 | Website shown DOWN right after the first start, then HTTP 200 in 659 ms on the next probe | The first probe timed out (10 s). The startup rule let a single sample set the state, so one failure meant DOWN, and it then needed 2 successes to recover | At startup, DOWN now needs `fail_after` consecutive failures like any other time. Why the first connection was slow is not known yet |
| 2026-11-13 | Website: 4 of 9 probes timed out (10 s) in the first hour; successful ones took 0.7–3.0 s. Never 3 failures in a row, so it stayed UP and no alert fired | DNS points to a mainland China CDN node; probes run from Japan. Not confirmed yet | None to Vigil. Hysteresis worked as intended; the site itself needs a closer CDN node |

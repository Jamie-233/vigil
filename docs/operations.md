# Operations

Vigil monitors real services from a Mac mini on a home connection. This file
records what it watches, why, and what happens while it runs: incidents,
false alarms, and every change to thresholds or code that running it forced.

Template: [`deploy/targets.example.yml`](../deploy/targets.example.yml) · Install: `bash deploy/macos/install.sh`. The real target list stays on the monitoring host (`~/.vigil/vigil.yml`) and is not published.

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

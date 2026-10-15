# jules-cert-sweeper

A Cloudflare Worker (cron, every 5 min) that watches Jules sessions for the
`johnyoonh/hanuri-missions` repo and triggers a **one-probe** GitHub Actions
certification check when a session reaches a terminal state — replacing the
old design that parked a billed Actions runner for up to 30 min of polling
per PR event (that step burned ~248 Actions minutes in a single 2.4 h window
on 2026-10-15 and zeroed the month's quota).

## How it works

1. Every 5 min the Worker lists open PRs labeled `jules:run` without a
   terminal label (`jules:certified`, `jules:changes-required`,
   `jules:needs-user`).
2. It extracts the Jules session id from the router's state comment
   (`<!-- github-agent-router: … Session: \`sessions/xxx\``) and polls the
   Jules API once per PR (seconds, no runner).
3. On a terminal session state (`COMPLETED`/`FAILED`/`CANCELLED`), it fires
   `repository_dispatch` (`jules-cert-sweep`, payload `{pr_number}`).
4. The companion workflow `.github/workflows/jules-cert-sweep.yml` (on the
   `fix/jules-router-minute-burn` branch, PR #254) synthesizes a
   `pull_request` event from live PR state and runs **one** ~60 s
   `github-agent-router watch` probe, which records the verdict with the real
   router logic (labels, handoff issues). Cost per certification: ~1 min of
   Actions instead of up to 30.
5. The Worker re-dispatches at most hourly until the terminal labels land —
   so if Actions minutes are exhausted, the verdict is recorded automatically
   once minutes return. Sessions tracked > 6 h are dropped as stale.

## Why a fixed 5-min tick, not backoff

Measured from 116 Jules Router PR runs (2026-10-01 → 15, pre-quota):

| time-to-terminal | p50 | p80 | p90 | p95 | p99 | > 30 min |
|---|---|---|---|---|---|---|
| minutes | 16 | 24 | 26 | 29 | 46 | ≥ 25% |

Simulated detection lag (20k trials against that distribution):

| schedule | p50 lag | p90 lag | p96 lag | p99 lag | P(lag ≤ 10 min) |
|---|---|---|---|---|---|
| **fixed 5-min tick** | 2.5 min | 4.7 min | 4.9 min | **5.0 min** | **100%** |
| backoff 5,10,20,40,80 | 9.9 min | 32.4 min | 37.2 min | 39.6 min | 54% |

Backoff only makes sense when each check is expensive. Here checks are free
(Worker + one Jules API call), while 25% of sessions run past 30 min —
exactly where backoff intervals are sparsest. So: no backoff, fixed tick.
The 90% / 96% / 99% ("2σ / 3σ") targets are all met deterministically
(p99 lag = 5.0 min).

## Deploy (John's steps — ~5 min)

Prereqs: `wrangler` installed and logged in (`wrangler login`), this directory
as cwd.

```bash
# 1. Create the KV namespace for per-PR tracking state, then paste the
#    returned id into wrangler.toml (replace REPLACE_WITH_KV_NAMESPACE_ID).
wrangler kv:namespace create SESS

# 2. Secrets — values are entered interactively, never pasted in chat.
#    JULES_API_KEY: Jules API key (same value as the repo's JULES_A_API_KEY;
#    find it at jules.google.com → API keys).
wrangler secret put JULES_API_KEY
#    GITHUB_TOKEN: fine-grained PAT scoped to johnyoonh/hanuri-missions with
#    Pull requests: Read, Issues: Read, Actions: Read and write.
#    (GitHub → Settings → Developer settings → Personal access tokens →
#    Fine-grained tokens → Generate new token.)
wrangler secret put GITHUB_TOKEN

# 3. Deploy and watch the first ticks.
wrangler deploy
wrangler tail
```

Manual test without waiting for cron: `curl
"https://jules-cert-sweeper.<your-subdomain>.workers.dev/sweep?dry=1"`
returns a JSON summary without dispatching anything.

## Cost

~288 Worker invocations/day + a few thousand KV ops/day — far inside the
free tier (100k req/day). Actions cost is ~1 min only when a session actually
reaches a terminal state (the one-probe verdict recording), versus up to
30 min/event before.

## Files

- `worker.js` — the sweeper (no dependencies, no build step).
- `wrangler.toml` — Worker config; cron `*/5 * * * *`.
- Companion Actions workflow: `.github/workflows/jules-cert-sweep.yml` on
  the hanuri-missions `fix/jules-router-minute-burn` branch (PR #254).

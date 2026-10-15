# Jules certification advisory sweeper

This preserves the existing `jules-cert-sweeper` Worker, `SESS` KV binding,
`jules-cert-sweep` dispatch contract and five-minute cron cadence. The Worker is
an advisory ticker, not a Jules mutator, verifier, merger, or authorization source.

The protocol-2 runtime emits an HMAC-signed envelope of exact canonical bytes,
bound to the original GitHub comment ID, repository, PR and sticky executor. The
Worker validates that envelope before reading the session. Old comments lacking
an envelope need one authenticated protocol-2 router event; do not delete their
canonical state. The router still independently authenticates every dispatch,
fetches live PR/session state, and acquires the shared non-cancelling mutation lock.

Completed/blocked verdicts stop polling only when the PR head and base still
match. A stale `jules:certified` label cannot hide a changed base. Pending probes
are bounded, pagination is explicit, requests time out, and each invocation caps
API work and dispatches. `MAX_DISPATCHES` defaults to 5 (maximum 20). The Worker
never accepts public `/sweep` requests; `/health` is the only HTTP endpoint.
`dry_run` is an exported local-test option and makes no storage or GitHub writes.

A dispatch reservation is written before POST. A lost response or a workflow that
never runs leaves the reservation visible instead of repeatedly spending Actions
minutes. Reconcile the matching PR, Actions run and source revisions before
removing that one reservation to retry. Do not flush the whole namespace. KV is
**eventually consistent**, so this ledger is not a distributed/exactly-once lock;
duplicate hints remain safe because the router serializes and authenticates the
actual operations. Configure just one ticker per target; do not also schedule the
Python sweeper for the same repository.

## Rollout

Configure repository scope, `SESS` KV namespace, and credentials:

- **Repository Targeting (`wrangler.toml` vars)**:
  - `ALLOW_REPOS`: Set to `"*"` (or `"all"`) to discover and sweep all active repositories accessible to the token, or provide a comma-separated list (`"org/repo1, org/repo2"`) for granular inclusion.
  - `DISALLOW_REPOS`: Provide an explicit comma-separated list of repositories to exclude/skip (e.g. `"org/archive-repo, org/private-notes"`).
  - `GITHUB_REPO`: Supported as single-target fallback.
  - `ALERT_EMAIL`: Address `[CI ALERT]` emails go to (Resend).

- **Secrets (`wrangler secret put`)**:
  - `GITHUB_TOKEN`: Token with repository metadata, pull-request/issue read access, and **Contents: write** for `repository_dispatch`.
  - `JULES_A_API_KEY`: API key for Jules executor account `a`.
  - `JULES_B_API_KEY`: API key for Jules executor account `b` (for overflow or multi-account routing).
  - *(Legacy `JULES_API_KEY` is also supported as fallback).*
  - `RESEND_API_KEY`: Resend API key for CI failure alerts (see Failure watch).
    Also confirm `hanurimissions.org` shows Verified under Resend -> Domains.

### Secret Provisioning via Bitwarden CLI (`bwa`)

```bash
cd sweeper

# 1. Provision KV namespace (paste returned ID into wrangler.toml)
npx -y wrangler kv:namespace create SESS

# 2. Pipe secrets directly from Bitwarden without terminal exposure
bwa get JulesA JULES_A_API_KEY | npx -y wrangler secret put JULES_A_API_KEY
bwa get JulesB JULES_B_API_KEY | npx -y wrangler secret put JULES_B_API_KEY
bwa get GithubToken GITHUB_TOKEN_jules-sweep | npx -y wrangler secret put GITHUB_TOKEN
bwa get ResendKey RESEND_API_KEY | npx -y wrangler secret put RESEND_API_KEY

# 3. Deploy
npx -y wrangler deploy
```

Never copy secret values into code or chat. Inspect the existing deployment and
bindings, install the protocol-2 target workflow, run `npm test` in this directory,
and perform a credentialed dry run in an isolated environment before activating
the cron. No deployed Worker, namespace, keys, schedule or runtime health is claimed
by committing these files. The ordinary app CI `gate` remains independent.

## Failure watch

The same five-minute tick also runs `failureWatch` (separate from `sweep`,
with its own API budget), because GitHub emails on EVERY failed run — pure
noise at agent speed, or when the minutes quota is exhausted and everything
fails in seconds. It decides what deserves an interruption:

- **Ignored:** `cancelled` / `skipped` / `action_required` runs.
- **Validity gate:** a failure counts as a *code failure* only if some job
  actually ran >= 60 s. Faster deaths are *infra deaths* (quota exhaustion,
  missing secrets, YAML errors), tracked separately.
- **Catastrophic:** any code failure on a `push` to `main` -> alert immediately.
- **Repeated:** >= 3 consecutive code failures for the same (workflow,
  branch) -> alert once; a success resets the streak.
- **Infra streak:** >= 10 consecutive sub-minute deaths for the same
  (workflow, branch) -> alert once ("check quota/secrets/YAML").

Alerts go via Resend with a `[CI ALERT]` subject so they sail past the Gmail
filter below. Repositories are resolved with the same allow/disallow rules,
checked through a rotating window (25 repos/tick) to bound API spend. A fresh
deploy watermarks to the newest run and only processes new runs — no alert
storm on history. `dry` mode (exported test option) is fully read-only.
Covered by `worker.test.js` ("Failure watch tests").

## Daily digest

A second cron trigger (`0 12 * * *`, 08:00 EDT) sends one summary email per
day instead of per-run pings. The `scheduled` handler routes that cron to
`sendDigest`, which:

- Lists each fleet repo's runs from the last 24h (`created=>` filter, one
  API call per repo) and counts them per workflow.
- Flags sub-minute failures as likely quota/infra, using run duration as a
  proxy for the failure watch's 60 s jobs check.
- Lists open failure streaks and the alerts the failure watch sent in the
  last 24h (from the `fail:` KV keys, via `alerted_code_at` /
  `alerted_infra_at` timestamps).
- Sends a heartbeat ("no completed runs") on quiet days, so a missing digest
  is itself a signal the Worker is down.

Subject: `[CI DIGEST] 2026-10-16 — 58 runs, 4 failed, 1 alert sent`.
Covered by `worker.test.js` ("Digest tests"). This is the summary channel:
with the digest plus `[CI ALERT]`s live, GitHub's own per-run failure
emails can be switched off entirely (GitHub Settings -> Notifications ->
Actions) — but only *after* the Worker is deployed and the first digest
arrives, or failures go into a black hole.

## Gmail filter (silence GitHub's own failure emails)

GitHub's failure emails come from `notifications@github.com` with subjects
like `[owner/repo] PR run failed: CI - ...`. In Gmail: gear icon -> See all
settings -> Filters and Blocked Addresses -> Create a new filter. From:
`notifications@github.com`, Subject: `run failed` -> Create filter. Check
**Skip the Inbox (Archive it)**, **Apply the label** -> new label
`GitHub Actions`, **Never mark it as important** (optionally **Mark as
read**), and tick "Also apply filter to matching conversations" to clear the
backlog. The `[CI ALERT]` emails from Resend use a different sender and are
unaffected.

Reference: Cloudflare Workers scheduled handlers, Web Crypto HMAC, and Workers KV
consistency documentation; GitHub repository-dispatch REST permissions.


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

Keep the existing `GITHUB_REPO`, `SESS` namespace and secret bindings. The current
checked-in namespace ID is a placeholder, not a deployed resource. Supply:

- `JULES_API_KEY` for the selected sticky executor (`JULES_OWNER`, default `a`).
- `GITHUB_TOKEN` with repository metadata, pull-request/issue read access and
  **Contents: write** for `repository_dispatch`. It does not need rule administration.

Never copy secret values into code or chat. Inspect the existing deployment and
bindings, install the protocol-2 target workflow, run `npm test` in this directory,
and perform a credentialed dry run in an isolated environment before activating
the cron. No deployed Worker, namespace, keys, schedule or runtime health is claimed
by committing these files. The ordinary app CI `gate` remains independent.

Reference: Cloudflare Workers scheduled handlers, Web Crypto HMAC, and Workers KV
consistency documentation; GitHub repository-dispatch REST permissions.

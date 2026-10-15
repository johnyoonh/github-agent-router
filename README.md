# GitHub Agent Router

Sticky GitHub-to-Jules routing with authenticated state, one-shot PR certification,
and revision-bound native merge decisions. Python 3.11+; no runtime dependencies.

## Delivery boundary

ChatGPT/Codex produce a `chatgpt/` source PR. Jules independently checks the exact
source head and current base and returns behavioral/adversarial evidence. Any
repair PR is integrated back into the original source PR by git-fleet after a
combined-candidate test; the resulting source head is recertified.

`git-fleet verify-pr` supplies a signed receipt for the actual proposed merge tree.
The router checks that receipt, the signed Jules verdict, current GitHub required
checks, reviews, protection and revisions. Only the native PR merge endpoint is
used. A 403/404 is a permission/resource failure, never a signal to rewrite main
with the Git Data API. Merging and production/device validation remain separate.

## Commands

```sh
github-agent-router --version
github-agent-router plan OWNER/REPO --policy /trusted/agent-routing.json
github-agent-router check OWNER/REPO --policy /trusted/agent-routing.json
github-agent-router provision OWNER/REPO --policy /trusted/agent-routing.json --router-ref REVIEWED_FULL_SHA --dry-run
github-agent-router probe
github-agent-router sweep OWNER/REPO --state /trusted/router-sweep.json --dry-run
github-agent-router merge-evaluate OWNER/REPO --pr 12 --receipt /evidence/pr12.json --policy /trusted/delivery-policy.json
```

`probe` processes one authenticated event and exits. It does not sleep while Jules
works. An iteration can succeed while certification is **pending** or **blocked**;
its execution result is not a merge gate. The separate `agent-certification`
commit status remains pending/failed until the matching revision is certified.
The existing repository `gate` check is never fabricated or replaced.

The `watch` command is now a deprecated compatibility alias for one-shot PR
processing; timeout/interval are accepted but ignored. New callers must use
`probe`. Do not use successful workflow completion as Jules certification.

## Security and coordination

Per-PR queued serialization covers normal events and external sweep dispatches.
Active mutations are not cancelled; signed pending-operation reservations make
uncertain external writes recoverable without blind retries. Sweeps use the
actual authenticated event sender and live PR state, never a fabricated owner.
Bookkeeping comments are filtered before runner allocation. An external bounded
sweeper stores backoff and dispatch reservations rather than parking an Actions
runner for polling.

Private repositories default to account A, public repositories to B. Existing
permitted ownership remains sticky. PR certification does not silently transfer
accounts or use overflow after an external operation. Legacy issue routing keeps
its existing policy behavior. Inventory planning/provisioning still uses explicit
repository selection and `repos/agent-routing.json`; credentials are never copied
into PRs or repository files.

Read [operations and migration](docs/OPERATIONS.md), the
[certification protocol](docs/CERTIFICATION_LOOP.md), and
[merge policy, exceptions and runner rollout](docs/DELIVERY.md).

## Development

```sh
python -m pip install .
python -m unittest discover -s tests -p 'test_*.py' -v
python -W error -m compileall -q src tests
```

Synthetic API tests are not live Jules/GitHub or device evidence. Roll out a
reviewed immutable runtime pin and perform credentialed target smoke tests before
requiring `agent-certification` in a ruleset. Never silently weaken protection,
turn an unavailable check green, or declare a production deployment from unit tests.

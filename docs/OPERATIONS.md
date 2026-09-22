# Safe routing and inventory provisioning (0.2)

This runbook supersedes the original README's direct-to-default-branch
provisioning and implicit overflow examples. No command requires `gh`.

## Policy and rollout

The primary account is A for private repositories and B for public repositories.
A private repository becomes B-primary only with an explicit `to-be-public` policy
or `home: b` override. Directory roots and duplicate clones never infer intent.
Overflow is disabled by default. `allowed_owners` is a separate permission boundary:
sticky work outside that list stops instead of transferring accounts. Existing
permitted ownership remains sticky even if the primary changes.

`plan`, `check`, `setup-labels`, and `provision` accept explicit repositories or
`--manifest path/to/repos-manifest.tsv --policy path/to/agent-routing.json`.
The shared policy lives in `johnyoonh/dotfiles/repos/agent-routing.json`.
The legacy TSV remains unchanged. Overrides do not silently enroll repositories;
use the manifest, explicit arguments, or `additional_repositories`.

```sh
github-agent-router --version
github-agent-router plan --manifest repos/repos-manifest.tsv --policy repos/agent-routing.json
github-agent-router check --manifest repos/repos-manifest.tsv --policy repos/agent-routing.json
github-agent-router setup-labels --manifest repos/repos-manifest.tsv --policy repos/agent-routing.json --dry-run
github-agent-router setup-labels --manifest repos/repos-manifest.tsv --policy repos/agent-routing.json
github-agent-router provision OWNER/REPO --policy repos/agent-routing.json --router-ref REVIEWED_FULL_COMMIT_SHA --dry-run
github-agent-router provision OWNER/REPO --policy repos/agent-routing.json --router-ref REVIEWED_FULL_COMMIT_SHA
```

`GITHUB_TOKEN` must have the appropriate repository read/write permissions for the
requested command. Never put tokens into the manifest, policy, PR, or shell
arguments. Supply credential values through a trusted credential-owning process.
Batch output is JSON with independent results; any blocked target produces a
nonzero exit status after the other targets have been processed.

`plan` is read-only and identifies the actual default branch and repository ID.
`check` verifies repository API access, selected Jules source access, and expected
Actions secret *metadata*. It cannot read stored secret values or prove they equal
the locally tested keys. A live target workflow smoke test remains necessary.

`setup-labels` reconciles all five standard labels, including color and description,
without changing unrelated labels. `provision` requires locally testable permitted
Jules credentials and expected secret metadata before applying configuration. It
uses a dedicated `chatgpt/` branch and a PR against the actual default branch;
repeated identical runs reuse the work. It never merges that PR automatically,
changes visibility, distributes secrets, force-pushes, or overwrites an unmanaged
workflow. An existing human-owned workflow needs an explicit reviewed migration.
Changes to the task branch outside the managed workflow stop provisioning.

## Workflow distribution and credentials

Both the reusable workflow reference and its checked-out source are pinned to the
same reviewed full SHA. Cross-repository private callers additionally pass a
`ROUTER_READ_TOKEN` secret with read access to the router repository. Their normal
`GITHUB_TOKEN` remains scoped to the target; the checkout credential is not persisted.
The source repository must allow the private caller to use its reusable workflow.

Public targets cannot call this private reusable workflow. Labels and policy
planning still work, but workflow provisioning deliberately stops before writes.
Do not make the router public or copy private code into public repositories as a
workaround. An approved public-safe distribution or a separately authorized central
dispatcher is a remaining architecture decision.

Generated workflows pass only policy-permitted Jules keys. Secret presence is not
proof of source authorization, stored value validity, or reusable-workflow access.
This tool does not grant those permissions or copy credential values.

## State, authentication, concurrency, and recovery

Every routed event requires current repository write permission. `/jules` also
requires its comment author to match the event sender. Fork PR heads are not treated
as branches of the target source. Router bookkeeping labels do not start new work.

State is HMAC-authenticated using a domain-separated key derived from the selected
Jules key, and is bound to repository, issue number, executor, session, round, and
operation history and canonical comment ID. Only the Actions bot or an API-verified repository writer may
supply it. User copies of old signed comments cannot replay an earlier session.
The verified comment ID is updated directly; paginated reads cover long discussions.
Unsigned legacy bot state and rotated-key signature failures halt routing rather
than starting a duplicate session. Preserve the old comment and reconcile it using
the original account and session; do not remove owner labels to bypass this gate.

The reusable workflow serializes all events for the same repository/issue and does
not cancel active runs. Its bounded queue retains up to 100 waiting runs; overflow
requires operator reconciliation. `AGENT_ROUTER_SERIALIZED=true` asserts that the
caller provides this lock. The Python lock additionally serializes local threads;
it is NOT a distributed or multi-process lock. Standalone/multi-host executors must
supply equivalent serialization before setting that variable. Unserialized live
execution fails before writes. Dry runs do not need that grant and make no writes.

Before session creation or messaging, a signed pending operation is persisted.
After a confirmed result, state is saved before labels. Retries after label failure
repair labels without repeating the external operation. Uncertain creation is
reconciled through the original account's session list using the saved operation
marker and source. Exactly one matching session is recovered, never recreated.
Zero/multiple matches, or an uncertain send, stop for reconciliation. Absence from
an API list is not proof that a timed-out creation failed. No blind POST retry or
cross-account overflow is allowed after a creation reservation.

Unknown Jules states preserve owner and round, add `jules:needs-user`, and do not
send instructions or create work. Event history is retained (up to 256 events);
reaching the bound stops instead of dropping deduplication evidence.

## Validation and remaining gates

Run `python -m unittest discover -s tests -p 'test_*.py'` and
`python -W error -m compileall -q src tests`. Build/install the local wheel and smoke
test all CLI help entries. The suite includes stateful service doubles for concurrent
calls, uncertain writes, recovery, tampering, and idempotent provisioning. These are
not live Jules or GitHub mutation tests.

No new cloud CI is required. Existing repository checks still gate PR delivery.
Do not infer deployment or merge approval from a completed Jules session. Full
revision-bound merge evaluation, scheduled drift/session reconciliation, automated
legacy-state/key rotation, and public-safe runtime distribution remain separate
work. Never bypass branch protection or conflate a mocked check with live evidence.

References: GitHub Actions reusable-workflow access matrix, concurrency syntax,
repository collaborator permissions, and Google's Jules Sessions REST reference.

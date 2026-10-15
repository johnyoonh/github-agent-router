# Revision-bound certification and original-PR repair loop

## Roles and state

ChatGPT/OpenCLI owns the source `chatgpt/` PR. The router is the GitHub/Jules control
plane. Jules independently evaluates behavior and adversarial cases. Git Fleet
constructs and tests an isolated candidate and integrates explicitly verified
repair PRs back into the source branch. Local deployment adapters own actual
promotion, health checks, rollback and device/release evidence.

A same-repository source PR is eligible by `chatgpt/` prefix, explicit policy or
`jules:run`/sticky ownership labels. Jules branches and fork heads do not recursively
start certification. Writers are authorized against current repository access.
Labels are display state, never authoritative evidence.

## Exact verification record

The newest Jules agent message must contain exactly one marker:

```text
<!-- github-agent-router:verification:{"verdict":"certified","summary":"evidence summary","head_sha":"FULL_SOURCE_SHA","base_sha":"FULL_BASE_SHA","merge_tree_sha":"FULL_TESTED_TREE_SHA","tests":["command/result"],"red_team":["adversarial case/result"],"requests":[]} -->
```

`certified` requires nonempty behavioral and adversarial evidence, exact head/base,
the actual tested integration tree, and no open requests. `changes_required`
reports an unresolved defect/repair PR; `needs_evidence` requests bounded local
proof; `needs_user` identifies a material decision. Terminal Jules completion,
an old marker or a separate repair's success is not certification.

The canonical comment is HMAC-authenticated, bound to repository, PR, original
comment ID, account, session, revision and operation history. Copied comments
cannot replay an old canonical state. Duplicate canonical records, invalid
signatures, rotated keys and uncertain sends stop for reconciliation.

## One event, one iteration

The reusable workflow runs `probe` once under a shared per-PR queued lock. Pending
is normal; no billed runner waits for 30 minutes. The standalone `sweep` command
can run on an external trusted ticker, storing exponential backoff and a durable
dispatch reservation before POST. Dispatches must use the same PR lock as routing.
A lost dispatch response is surfaced, not blindly retried.

Every positive `agent-certification` status is attached to the evaluated head.
Base/head movement invalidates merge readiness even when an older status is green.
The merge evaluator additionally checks the base and merge tree against signed
Jules state and Fleet evidence. It never trusts a bare label or workflow success.

Active previous revisions are allowed to finish without duplicate sessions. Once
terminal, the current revision starts a new sticky round. Malformed/failed rounds
are bounded by policy; one feedback nudge is allowed before an explicit handoff.
Authenticated `/jules` follow-ups are deduplicated and sent only once; uncertain
send outcomes require reconciliation rather than a duplicate message.

## Repairs and local evidence

A repair absent from the original source head must return `changes_required`, not
certify its own branch. Test the combined candidate with Fleet's `--include-pr`,
then use `integrate-fix` to update only the original `chatgpt/` branch. Verify and
certify its new head again. No repair author or agent gets an automatic safety
exemption.

Blocked outcomes produce one source-linked durable `chatgpt:handoff` issue through
the existing deduplicating issue helper. Read current local AGENTS.md, collect only
the requested redacted evidence, and return it to the source PR. Preserve canonical
OpenCLI provenance on the source PR; never guess a conversation or copy private
instructions into public GitHub artifacts.

## Rollout

Use one reviewed protocol-2 pin for every entrypoint serving a target. Retire the
old hand-written sweep workflow in the same migration PR; it must not race or
impersonate the repository owner. Old pinned readers cannot understand expanded
state and will fail closed. Existing signed legacy state is retained and needs a
new exact-revision review before certification; never delete it to force a new
session. Issue-only legacy routing remains unchanged.

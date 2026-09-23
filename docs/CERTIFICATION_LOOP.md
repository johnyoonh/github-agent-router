# PR certification and ChatGPT handoff loop

This document defines the verification state machine for a ChatGPT-authored pull
request. The router is the GitHub/Jules control plane. It does not run local Git
reconciliation or drive the ChatGPT UI itself.

## Roles

- **ChatGPT / ChatGPT OpenCLI** creates or repairs a scoped `chatgpt/<slug>`
  branch and PR.
- **github-agent-router** recognizes same-repository `chatgpt/` PRs, tags them
  `agent:chatgpt` + `jules:run`, keeps Jules ownership sticky, and converts
  Jules results into merge-gate labels/state.
- **Jules** is an independent adversarial verifier. It should add missing focused
  tests, red-team realistic failure modes, and emit a structured verdict.
- **git-fleet** only reconciles the local checkout when local evidence is needed.
  It is not the reasoning/orchestration agent.
- **chatgpt-opencli** is the intended local handoff transport. When a canonical
  conversation is known it may continue that conversation; otherwise it must
  create a fresh repository-project handoff instead of guessing a session.
- **The user** is asked only for a material product, security, scope, or data-loss
  decision that cannot safely be inferred.

## Ingress

A same-repository PR whose head begins with `chatgpt/` is eligible even when it
was not manually labeled first. After writer and fork checks pass, the router
adds:

- `agent:chatgpt`
- `jules:run`

Jules-authored branches/PRs remain excluded from recursive review.

## Certification protocol

The review prompt asks Jules to derive behavioral success criteria, run existing
relevant checks, add missing focused regression tests, and probe relevant
edge/error, state-transition, concurrency, security, compatibility, and
data-loss cases.

A terminal review round is **not** certification merely because the Jules session
is `COMPLETED`. The newest Jules `agentMessaged.agentMessage` activity must
contain exactly one marker:

```text
<!-- github-agent-router:verification:{"verdict":"certified","summary":"...","tests":["..."],"red_team":["..."],"requests":[]} -->
```

Allowed verdicts:

| Verdict | Meaning |
|---|---|
| `certified` | At least one behavioral test/check and one relevant adversarial probe are recorded and no known merge blocker remains. |
| `changes_required` | Jules reproduced a real shortcoming. The source PR is blocked and handed back for repair. |
| `needs_evidence` | The repository alone cannot provide decisive evidence. Exact bounded local evidence is requested. |
| `needs_user` | A material decision truly requires the user. |

Malformed/missing terminal records never become certification. A missing record
can trigger another bounded Jules round; after the autonomous cap it becomes an
inconclusive/blocked handoff.

The source PR derives these labels:

- `jules:certified`
- `jules:changes-required`
- `agent:blocked`
- `jules:needs-user`
- `chatgpt:handoff`

The reusable workflow also runs `github-agent-router watch` for routed PRs. The
watch is bounded (default 1800 seconds). It succeeds only after a certified
verdict; explicit blocked verdicts or an unsettled timeout fail the workflow.
Repositories may make that workflow check required in their ruleset after live
validation.

Normal CI remains independent and must still pass. Jules certification does not
bypass branch protection, reviews, or other required checks.

## Avoiding unnecessary Jules stalls

If Jules enters `AWAITING_USER_FEEDBACK`, the router sends one bounded nudge:

1. answer from code/tests/docs when possible;
2. use a conservative non-destructive assumption when it is safe;
3. return `needs_evidence` instead of waiting when only local logs/evidence are
   unavailable;
4. remain blocked on the user only for a material decision.

If Jules still waits after that single nudge, the block is surfaced rather than
looped indefinitely.

## Durable handoff issue

`changes_required`, `needs_evidence`, `needs_user`, malformed terminal
verification, and exhausted/failed verification can create one deduplicated
`chatgpt:handoff` issue per source PR/issue. Reconciliation updates the existing
handoff issue instead of creating duplicates.

The issue contains:

- source PR/issue;
- Jules session URL/state;
- verdict and bounded evidence request;
- optional ChatGPT continuation provenance;
- a local consumer contract.

It intentionally does **not** embed local logs, credentials, or private
`AGENTS.md` text.

### Optional chatgpt-opencli provenance

A PR producer may include this machine-readable marker in the PR body:

```text
<!-- chatgpt-opencli:origin:{"conversation":"https://chatgpt.com/c/...","job_id":"...","project":"repo-name","agents_sha":"..."} -->
```

The router accepts only canonical `https://chatgpt.com/c/` or
`https://chatgpt.com/share/` conversation URLs. Absence of this marker is
normal: the handoff consumer must create a fresh project-scoped ChatGPT handoff
rather than guessing the original chat.

`agents_sha` is provenance only. The current applicable `AGENTS.md` must still
be read locally at handoff time. If chatgpt-opencli synchronizes repository
instructions into ChatGPT Project Instructions, that synchronization must happen
before the continuation prompt is submitted. Private instruction text must not
be copied into GitHub issues.

## Local evidence consumer contract

The local watcher/consumer is intentionally outside this repository. A compliant
consumer should:

1. claim one open `chatgpt:handoff` issue;
2. run `fleet sync --only owner/repo`;
3. read the current applicable `AGENTS.md`;
4. synchronize ChatGPT Project Instructions when that opencli capability is
   enabled;
5. collect only the exact requested bounded evidence, with credentials/private
   payloads redacted;
6. continue the canonical ChatGPT conversation when provenance exists, otherwise
   open a fresh project-scoped handoff;
7. return a repair/evidence result to the source PR;
8. use a `/jules ...` source comment or branch update to start the next sticky
   verification round.

An explicit `/jules` follow-up after a blocked terminal round should create a
new sticky Jules round rather than trying to message a terminal session.

## End-to-end satisfaction

The machine-readable scenario matrix is
[`tests/e2e_satisfaction.json`](../tests/e2e_satisfaction.json). It separates
offline deterministic checks from credentialed live evidence.

A production rollout is satisfactory only when the applicable live scenarios
have real evidence for:

1. ChatGPT PR ingress/tagging;
2. a certified happy path;
3. a seeded behavioral defect discovered and handed back before later
   certification;
4. local-evidence collection through git-fleet + chatgpt-opencli;
5. a genuine material-user block surfaced without an infinite feedback loop;
6. duplicate/replay safety;
7. changed `AGENTS.md` being reflected in project instructions before a local
   ChatGPT continuation.

Unit tests can validate the state machine and idempotency, but they cannot claim
that ChatGPT.com, Jules credentials, the Mac runner, browser authentication, or
Project Instructions synchronization worked live.

# Native merge evaluation and explicit infrastructure exceptions

## Trusted executor policy

Copy `examples/delivery-policy.json` outside the repository and explicitly select
repositories and human approvers. Do not read merge policy, evidence signing keys,
or executable check plans from untrusted PR content. Use a non-bypass GitHub merge
identity for unattended protected-branch merges. The checker conservatively blocks
unsupported/unknown protections and special review policies it cannot prove.

```sh
github-agent-router merge-evaluate OWNER/REPO --pr 12 --receipt /evidence/pr12.json --policy /trusted/delivery-policy.json
```

`--execute` performs one fresh evaluation and a native PR merge using the expected
head SHA. It never updates main through Git Data, forces a push, or substitutes a
passing status for missing CI. Read-only evaluation is the default. A denied merge
is an error, not a request to try another write API. The resulting merge tree is
read back; any mismatch requires revalidation before promotion.

The decision requires an open ready same-repository PR, known supported protection,
resolved reviews, exact matching head/base/tree, current signed Jules certification,
a recent trusted Fleet receipt, and successful required checks from their required
source apps. Pending, missing, skipped, neutral and ordinary failed checks block.
An elevated bypass-capable identity is not silently used even when the checks are
green; use a separate non-bypass identity.

## Outage proof is not test proof

An unavailable CI classification requires scheduler-owned billing evidence tied to
an actual GitHub Actions check/job that never acquired a runner or executed steps.
A test printing a billing phrase does not qualify. Red checks alone do not qualify.
A positive outage classification still requires local verification and independent
Jules evidence for the exact current integration.

Only an allowlisted human's fresh, unedited comment on that exact source PR may
authorize the named infrastructure checks to be waived. The executor never posts
its own approval. Use a human approval identity whose credentials are not exposed
to autonomous agents: a GitHub `User` record alone cannot prove human presence.

The comment is `/fleet-approve-exception ` followed by one JSON object containing
repository, pr, head_sha, base_sha, merge_tree_sha, the SHA-256 of the canonical
signed receipt, a reason, `waive_checks` (exact outage check names), and an ISO-8601
`expires_at`. It must expire within 15 minutes of comment creation. Any edit,
revision/tree/receipt change, unauthorized author, ordinary test failure, missing
review, or unknown protection invalidates the exception. This documentation does
not create an approval.

Evaluate with `--approval-comment-id ID` and supply `--execute` only in an authorized
trusted executor. GitHub must independently permit the native merge; the router
does not grant bypass access or change rulesets. A 403/404 ends that attempt.

## Ruleset tightening and runner activation

For the Hanuri incident, narrow the existing RepositoryRole actor 5 on ruleset
25110167 from `always` to `pull_request`, preserving every other rule. This is an
administration operation, not a merge fallback. The connector used to prepare this
implementation cannot apply ruleset administration writes; run the included
`scripts/tighten_ruleset.py` with an authorized local GitHub CLI credential, inspect
its plan and use its expected digest before applying. Its backup/readback and
fail-on-drift behavior do not eliminate GitHub API's lack of an atomic ruleset CAS;
avoid concurrent admin edits during the operation.

Self-hosted runner support is opt-in, not registration. Use disposable isolated
workers without personal/cloud credentials and keep fork code off trusted workers.
Setting a runner-label variable is not proof of runner health, isolation, billing
availability, or passing CI. Do not change required-check identity to manufacture
a green gate. Fleet's workflow accepts `CI_RUNNER_LABELS_JSON`; leave it unset
until a credentialed runner smoke test passes.

```sh
python scripts/tighten_ruleset.py johnyoonh/hanuri-missions --ruleset-id 25110167 --actor-id 5
# After reviewing its digest and actor-only plan:
python scripts/tighten_ruleset.py johnyoonh/hanuri-missions --ruleset-id 25110167 --actor-id 5 --apply --expected-digest REVIEWED_DIGEST --backup /trusted/hanuri-ruleset-backup.json
```

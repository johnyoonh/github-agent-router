# GitHub Agent Router

Sticky GitHub-to-Jules routing with authenticated state and inventory-driven,
PR-based repository provisioning. Requires Python 3.11+; no runtime dependencies.

## Routing policy

Private repositories are A-primary; public repositories are B-primary. A private
repository intended for publication needs an explicit `to-be-public` policy or
`home: b` override. Root directories never establish publication intent.
Overflow is disabled by default. Permitted existing ownership stays sticky;
policy conflicts stop rather than silently transferring a session.

The shared inventory and policy are maintained in `johnyoonh/dotfiles`:
`repos/repos-manifest.tsv` and `repos/agent-routing.json`.

## Commands

```sh
github-agent-router --version
github-agent-router plan OWNER/REPO
github-agent-router check OWNER/REPO
github-agent-router setup-labels OWNER/REPO --dry-run
github-agent-router setup-labels OWNER/REPO
github-agent-router provision OWNER/REPO --router-ref REVIEWED_FULL_COMMIT_SHA --dry-run
github-agent-router provision OWNER/REPO --router-ref REVIEWED_FULL_COMMIT_SHA
```

Batch commands accept `--manifest` and `--policy`, deduplicate repositories, and
report per-target JSON results. Failures do not stop independent targets but do
produce a nonzero batch exit. No command requires `gh`.

Provisioning reconciles the five standard labels and opens/reuses a scoped
`chatgpt/` PR against the actual default branch. It preserves unmanaged files and
unrelated changes. Both the reusable workflow and executed source are pinned to
one reviewed full SHA. Provisioning does not merge, change repository visibility,
copy secrets, or enable unattended schedules.

## Security and migration

The runtime authorizes event actors, binds HMAC-authenticated state to its original
comment and repository/issue, rejects fork-head confusion, and enforces read-only
dry runs. Serialized execution and durable pending operations prevent blind
retries after uncertain external writes. State is saved before labels, so a label
failure can be repaired without repeating session creation or messaging.

**Unsigned legacy state and key rotation require reconciliation.** They halt
rather than create duplicate work. Existing callers must supply `router_ref` and
provide serialization. The included reusable workflow supplies per-issue queued
serialization and never cancels active runs.

**Public workflow activation remains gated.** A public repository cannot call this
private reusable workflow. Planning and labels are supported; activation requires
an explicitly approved public-safe distribution or central dispatcher. Private
cross-repository callers need reusable-workflow access and `ROUTER_READ_TOKEN`.
Credential metadata is not proof that stored secret values are valid.

Read [operations, credentials, recovery, and validation](docs/OPERATIONS.md) before
migrating existing callers. [The original 0.1 README](docs/LEGACY-0.1.md) is retained
for historical context only; its direct-write, floating-ref, and implicit-overflow
examples are superseded. Generate target workflows through `provision` rather
than copying the old example.

## Development

```sh
python -m pip install .
python -m unittest discover -s tests -p 'test_*.py'
python -W error -m compileall -q src tests
```

Tests use synthetic, stateful service doubles; they are not live GitHub/Jules
integration evidence. Deployment needs credential-backed target smoke tests.
Full SHA-bound merge evaluation, scheduled status/drift reconciliation, automatic
legacy-state migration, and public runtime distribution remain separate work.

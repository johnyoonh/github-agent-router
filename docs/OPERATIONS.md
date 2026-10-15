# Operations, credential boundaries and migration

## Existing inventory contract

`plan`, `check`, `setup-labels`, and `provision` accept explicit repositories or an
explicit inventory/policy. Repositories are not enrolled by directory discovery.
The user inventory remains in johnyoonh/dotfiles: `repos/repos-manifest.tsv` and
`repos/agent-routing.json`. A private repository only becomes B-primary through an
explicit policy; keys, paths or duplicate checkouts do not infer publication intent.

Provisioning uses a dedicated `chatgpt/` branch and PR, validates permitted Jules
source access and required secret metadata, preserves unmanaged files, and pins
runtime and reusable workflow to the same full SHA. It does not merge the PR,
change visibility, copy secret values, enable schedules, or overwrite human-owned
workflows. Stored-secret metadata cannot prove the values or live access work.
Public target distribution remains explicitly gated rather than inferred from
repository visibility; a separately reviewed migration is needed.

## Protocol-2 deployment

1. Review and test the router revision.
2. Update each target's managed workflow through a PR. Remove its legacy companion
   `jules-cert-sweep.yml` when dispatch handling moves into the managed workflow.
3. Use one immutable protocol-2 pin for route/probe/dispatch. Do not run old readers
   against expanded signed state. Preserve and reconcile old canonical state.
4. Smoke-test real credentials and source access, one ordinary event, one pending
   probe, a terminal dispatch, a seeded defect/handoff, a repair and new-head
   certification. Check that the ordinary CI gate remains independent.
5. Only then consider adding `agent-certification` to required checks. Keep the
   original CI/app-bound gate and reviews. Missing or unknown proof stays blocked.

## Locks and external-operation recovery

The reusable workflow shares one concurrency group per repository/issue/PR,
including dispatch payload PR numbers. It uses `cancel-in-progress: false` and
`queue: max`. No long poll is inside the lock. A job timeout is still an uncertain
interruption; a durable pending reservation makes it a reconciliation case rather
than permission to retry a POST.

`AGENT_ROUTER_SERIALIZED=true` means the caller actually provides this exclusion.
It is not a lock by itself. Standalone/multi-host executors must provide equivalent
serialization. A local sweeper checkpoint lease protects its own scheduler only;
it sends dispatches whose actual state mutation happens under the Actions PR lock.

Exactly one Jules session matching a reserved operation marker/source can be
recovered after uncertain creation. Zero/multiple matches, uncertain messaging,
invalid signatures and key rotation stop. Preserve original keys and canonical
comment IDs during reconciliation. Never transfer sticky ownership to work around
an uncertain operation.

## Bounded sweeper

```sh
github-agent-router sweep OWNER/REPO --state /trusted/router-sweep.json --max-dispatches 5 --dry-run
```

Run the non-dry command from an explicitly configured external scheduler after
credentialed testing. It performs one pass, caps dispatches and records backoff;
it never installs a schedule or sleeps on a paid Actions runner. A reserved or
uncertain dispatch is not resent until source state changes or an operator
reconciles the reservation. Use one shared checkpoint/lease for that scheduler.

## Verification

Run the complete existing and new unittest suite and compile checks. New pure
policy, fake-service and temporary-Git tests are offline evidence only. They do not
prove the hosted workflows, Jules account access, the local browser/OpenCLI,
AGENTS.md synchronization, a self-hosted runner, or real-device promotion worked.
Keep that distinction in PRs and rollout records.

See [delivery decisions and explicit exceptions](DELIVERY.md).

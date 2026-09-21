# GitHub Agent Router

Sticky routing between GitHub issues/PRs and multiple Jules accounts.

This repository is the execution layer for a workflow where ChatGPT.com can create implementation PRs, GitHub routes review/fix work to one of multiple Jules accounts, and subsequent feedback stays with the same Jules account/session for locality.

## What is implemented

- Two Jules executors: **A** and **B**, each authenticated by its own Jules API key.
- Per-repository home account with optional overflow for **unclaimed** work.
- Sticky issue/PR ownership using `jules-owner:a` / `jules-owner:b`.
- Sticky Jules session metadata stored in a machine-readable GitHub comment.
- Existing active sessions are continued instead of creating a new session.
- `AWAITING_USER_FEEDBACK` becomes `jules:needs-user`.
- A GitHub comment beginning with `/jules` is sent back to the same Jules session.
- Completed sessions may get a follow-up review round, capped by `max_rounds`.
- Jules-authored PRs are excluded from automatic re-review to reduce agent loops.
- Overflow happens only before a task has an owner. Claimed work never silently switches accounts.
- Reusable GitHub Actions workflow for target repositories.
- Unit tests for sticky-routing behavior.

The Jules API integration uses the official Sources and Sessions APIs. The router discovers the repository source visible to each Jules account, creates an `AUTO_CREATE_PR` session, polls its state on later GitHub events, and uses `:sendMessage` for continuation.

## Routing model

```text
new issue / PR
      |
      v
already has jules-owner?
  | yes                | no
  v                    v
same account       repo home account
  |                    |
same session       unavailable/rate-limited?
  |                    |
  |                overflow account
  |                    |
  +------ persist owner/session ------+
                    |
                    v
          future comments / updates
                    |
                    v
             same Jules account
             same session when active
```

Priority is:

1. Existing Jules session.
2. Existing `jules-owner:a|b` label.
3. Repository home account.
4. Overflow account for never-claimed work only.

## Labels

The router uses these labels:

- `jules:run` — opt an issue/PR into Jules routing.
- `jules-owner:a` — sticky owner is Jules A.
- `jules-owner:b` — sticky owner is Jules B.
- `jules:needs-user` — Jules is waiting for clarification, or the autonomous review cap was reached.
- `agent:jules` — optional marker for Jules-generated PRs; those PRs are not recursively routed.

The native Jules `jules` label is intentionally **not** used by this router. When running two Jules identities, use `jules:run` so one deterministic dispatcher owns account selection.

## One-time setup still required

These steps require your credentials or GitHub/Jules account settings and therefore are intentionally not committed to this repository.

### 1. Create one Jules API key per Jules Google account

While signed into Jules account A, create an API key in Jules settings. Repeat while signed into account B.

Keep the two keys distinct:

```text
JULES_A_API_KEY
JULES_B_API_KEY
```

Do not commit either key to this repository, dotfiles, or the wiki.

### 2. Give both Jules accounts repository access

For every target repository, install/authorize the Jules GitHub integration from each Jules account that may receive that repository.

A repository assigned `home: a` only strictly needs A unless B is configured as overflow. If `overflow: b`, both accounts must be able to see the repository in the Jules Sources API.

### 3. Allow private repositories to call this reusable workflow

This repository is private.

In this repository, open:

```text
Settings
→ Actions
→ General
→ Access
```

Enable access for other repositories owned by your GitHub account to use workflows from this repository.

### 4. Add Jules secrets to each target repository

For the first implementation, target repositories pass Jules credentials into the reusable workflow.

In each target repository add:

```text
Settings
→ Secrets and variables
→ Actions
```

Create whichever secrets that repository is allowed to use:

```text
JULES_A_API_KEY
JULES_B_API_KEY
```

If a repository should never overflow to B, do not give it the B key.

A later GitHub App/webhook deployment can centralize these secrets so they do not need to be copied per repository.

### 5. Create the routing labels in each target repository

At minimum create:

```text
jules:run
jules-owner:a
jules-owner:b
jules:needs-user
agent:jules
```

The router can create missing owner/state labels after it starts, but `jules:run` must exist before you can manually apply it in GitHub.

### 6. Add the target workflow

Copy [`examples/target-router.yml`](examples/target-router.yml) to:

```text
.github/workflows/jules-router.yml
```

in each target repository.

Example:

```yaml
name: Jules Router

on:
  issues:
    types: [opened, edited, labeled, reopened]
  pull_request:
    types: [opened, synchronize, reopened, labeled]
  issue_comment:
    types: [created]

jobs:
  jules:
    uses: johnyoonh/github-agent-router/.github/workflows/reusable-router.yml@main
    with:
      home: a
      overflow: b
      max_rounds: 2
      auto_review_prs: false
    secrets:
      jules_a_api_key: ${{ secrets.JULES_A_API_KEY }}
      jules_b_api_key: ${{ secrets.JULES_B_API_KEY }}
```

For a B-owned repository, change:

```yaml
home: b
overflow: a
```

For a repository that must never overflow:

```yaml
home: a
overflow: ""
```

and omit the other account's secret.

### 7. Start with explicit PR opt-in

The example intentionally uses:

```yaml
auto_review_prs: false
```

During rollout, add `jules:run` only to the ChatGPT-created PRs you want Jules to audit.

After you have verified there is no recursion/noise on a repository, change it to:

```yaml
auto_review_prs: true
```

Jules PRs are still excluded when they have `agent:jules` or a recognized Jules branch prefix.

## Normal use

### Route an issue

Create or update the issue and add:

```text
jules:run
```

If it has no owner, the router selects the repository home account. If that account receives a Jules HTTP 429 before ownership is persisted and overflow is configured, the router may try the overflow account.

After a session is created, the issue receives:

```text
jules-owner:a
```

or:

```text
jules-owner:b
```

and a router status comment containing the session ID/URL.

### Route a ChatGPT-created PR

With `auto_review_prs: false`, add:

```text
jules:run
```

to the PR.

With `auto_review_prs: true`, an opened/synchronized eligible PR is routed automatically.

The first routed account becomes sticky. Future branch updates are sent to that account/session rather than load-balanced again.

### Answer a Jules clarification from GitHub

When a session reaches `AWAITING_USER_FEEDBACK`, the router adds:

```text
jules:needs-user
```

Reply on the same issue/PR:

```text
/jules Keep audit history for seven years; deleting an organization must not delete audit records.
```

The router sends the text after `/jules` to that same Jules session and removes `jules:needs-user`.

You can also answer directly at the Jules session URL shown in the router comment.

## Review rounds and loop control

Default:

```text
max_rounds: 2
```

If a routed session is still active, GitHub updates continue the same session.

If a session is terminal and a later qualifying GitHub event arrives, the router may create a follow-up session owned by the same Jules account. Once the configured round cap is reached, it adds `jules:needs-user` instead of continuing indefinitely.

Do not raise the cap until the workflow has proven stable.

## Auto-merge into main: remaining GitHub policy work

This router intentionally does **not** bypass branch protection or merge directly to `main`.

For repositories where you want autonomous testing, configure GitHub so an eligible PR can auto-merge only after the checks you trust pass.

Recommended required checks:

```text
CI
tests
Agent Policy        (future)
Jules Review        (future)
security checks     (where relevant)
```

Then enable GitHub auto-merge for the repository/PRs you choose.

Recommended safety exclusions from unattended merge:

```text
.github/workflows/**
infra/**
terraform/**
auth/**
permissions/**
billing/**
migrations/**
production deployment configuration
```

Changes to the automation system that controls its own permissions should require human review.

### Still to implement before broad unattended merging

- A dedicated `Agent Policy` check that evaluates changed paths and the repository allowlist.
- Automatic marking of Jules-created PRs with `agent:jules` by correlating Session outputs.
- Automatic enabling of GitHub auto-merge when policy + CI + Jules review are green.
- Optional merge queue support.
- Better handling of completed sessions that found no changes versus sessions that created a corrective PR.
- A synchronization/bootstrap command driven from your dotfiles allowlist.
- Central GitHub App/webhook mode so Jules API keys exist in one service rather than every target repository.

Until those are implemented, use GitHub's normal branch/ruleset protections for the final `main` merge.

## Recommended dotfiles ownership

Keep declarative repository routing in your dotfiles, for example:

```yaml
# github/agent-routing.yml
defaults:
  max_rounds: 2
  auto_review_prs: false

repos:
  johnyoonh/frontend:
    home: a
    overflow: b

  johnyoonh/backend:
    home: b
    overflow: a

  johnyoonh/infra:
    home: b
    overflow: ""
```

The future `github-agent-sync` command should read that file and reconcile labels, repository variables/workflow stubs, and policy. Do not store session state in dotfiles. Session ownership belongs on the GitHub issue/PR itself.

## Recommended wiki ownership

Use your wiki for architecture/runbook documentation only, e.g.:

```text
99_meta/system/github-agent-routing.md
99_meta/system/repos.md
99_meta/system-map.md
```

The executable source and tests live here.

## Development

Requires Python 3.11+ and has no third-party runtime dependencies.

```bash
python -m pip install .
python -m unittest discover -s tests -p 'test_*.py'
```

Runtime environment:

```text
GITHUB_TOKEN
GITHUB_EVENT_NAME
GITHUB_EVENT_JSON or GITHUB_EVENT_PATH
JULES_A_API_KEY
JULES_B_API_KEY
JULES_HOME=a|b
JULES_OVERFLOW=a|b|empty
JULES_MAX_ROUNDS=2
JULES_AUTO_REVIEW_PRS=false
```

## Security

- Never commit Jules API keys.
- Keep the router private while it contains account-routing operational details.
- Do not grant a target repository an executor secret it is not allowed to use.
- GitHub remains the authority for merge protection.
- A sticky task never changes Jules owner automatically after it has been claimed.

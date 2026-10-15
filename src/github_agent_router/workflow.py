"""Single managed workflow renderer for event ingress and authenticated sweeps."""
from __future__ import annotations
import re

ROUTER_REPOSITORY = "johnyoonh/github-agent-router"
MANAGED = "# Managed by github-agent-router; schema=1"


def render_workflow(policy, revision):
    if not re.fullmatch(r"[0-9a-f]{40}", revision):
        raise ValueError("router revision must be a full, reviewed commit SHA")
    if not policy.private:
        raise ValueError("public target distribution still requires explicit approval")
    secrets = [f"      jules_{x}_api_key: ${{{{ secrets.JULES_{x.upper()}_API_KEY }}}}" for x in policy.allowed_owners]
    if policy.repository != ROUTER_REPOSITORY:
        secrets.append("      router_read_token: ${{ secrets.ROUTER_READ_TOKEN }}")
    return f'''{MANAGED}
# Delivery protocol=2: one-shot probes; no polling on paid runners.
name: Jules Router
on:
  issues:
    types: [opened, reopened]
  pull_request:
    types: [opened, synchronize, reopened, ready_for_review]
  issue_comment:
    types: [created]
  repository_dispatch:
    types: [jules-cert-sweep]
permissions:
  contents: read
  issues: write
  pull-requests: write
  statuses: write
jobs:
  jules:
    # Filter comments before allocating a runner, including our own state comments.
    if: >-
      (github.event_name != 'issue_comment' || startsWith(github.event.comment.body, '/jules')) &&
      !startsWith(github.event.issue.title || '', '[App feedback]') &&
      !startsWith(github.event.issue.title || '', '[App report]')
    uses: {ROUTER_REPOSITORY}/.github/workflows/reusable-router.yml@{revision}
    with:
      router_ref: {revision}
      home: {policy.home}
      overflow: "{policy.overflow}"
      allowed_owners: "{','.join(policy.allowed_owners)}"
      max_rounds: {policy.max_rounds}
      auto_review_prs: {str(policy.auto_review_prs).lower()}
    secrets:
''' + "\n".join(secrets) + "\n"

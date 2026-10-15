"""One-shot, authenticated, revision-bound PR certification.

The same per-repository/PR executor lock must cover route, probe and dispatch.
Pending is a successful control-plane iteration, never a passing merge gate.
"""
from __future__ import annotations

import base64
from datetime import datetime
import hashlib
import hmac
import json
import re
import time
import uuid

STATE_RE = re.compile(r"<!-- github-agent-router:(\{.*?\}) -->", re.S)
VERDICT_RE = re.compile(r"<!-- github-agent-router:verification:(\{.*?\}) -->", re.S)
ACTIVE = {"QUEUED", "PLANNING", "IN_PROGRESS", "PAUSED"}
TERMINAL = {"COMPLETED", "FAILED", "CANCELLED", "CANCELED"}
SHA = re.compile(r"[0-9a-f]{40}")


def signature(value, key):
    derived = hmac.new(key.encode(), b"github-agent-router/state/v1", hashlib.sha256).digest()
    return hmac.new(derived, json.dumps(value, sort_keys=True, separators=(",", ":")).encode(), hashlib.sha256).hexdigest()


def read_state(gh, number, keys):
    found = []
    for comment in gh.comments(number):
        user = comment.get("user") or {}
        if not ((user.get("login") == "github-actions[bot]" and user.get("type") == "Bot")
                or gh.can_write(user.get("login", ""))):
            continue
        match = STATE_RE.search(comment.get("body", ""))
        if not match:
            continue
        value = json.loads(match.group(1))
        sig = value.pop("signature", "")
        if value.get("comment_id") != comment["id"]:
            if value.get("comment_id"):
                continue  # Quoted/copied signed state is not the canonical record.
            raise ValueError("legacy unbound comment requires reconciliation")
        key = keys.get(value.get("owner"), "")
        if (not key or not isinstance(sig, str) or
                not hmac.compare_digest(sig, signature(value, key)) or
                value.get("repository") != gh.repository or value.get("number") != number):
            raise ValueError("invalid authenticated routing state")
        if value.get("pending", "") not in {"", "create", "send"}:
            raise ValueError("unknown pending operation")
        if value.get("session") and not re.fullmatch(r"sessions/[A-Za-z0-9_-]+", value["session"]):
            raise ValueError("invalid session name")
        if type(value.get("round")) is not int or value["round"] < 1:
            raise ValueError("invalid round")
        if not isinstance(value.get("events", []), list) or len(value.get("events", [])) > 256:
            raise ValueError("event history needs reconciliation")
        found.append(value)
    if len(found) > 1:
        raise ValueError("multiple canonical routing states; reconcile before mutation")
    return found[0] if found else None


def save_state(gh, number, state, keys):
    if not state.get("comment_id"):
        state["comment_id"] = gh.add_comment(number, "Preparing authenticated Jules routing state.")["id"]
    if type(state["comment_id"]) is not int:
        raise ValueError("state comment ID is not confirmed")
    data = dict(state)
    payload = json.dumps(data, sort_keys=True, separators=(",", ":")).encode()
    if len(payload) > 20000:
        raise ValueError("canonical state exceeds bounded comment budget; reconcile history")
    data["signature"] = signature(data, keys[state["owner"]])
    # Sign exact bytes for cross-language consumers; do not reconstruct Python
    # float/Unicode serialization in a Worker. The original marker stays readable.
    envelope = {"payload": base64.b64encode(payload).decode(), "signature": data["signature"]}
    gh.update_comment(state["comment_id"], "<!-- github-agent-router:" +
                      json.dumps(data, separators=(",", ":")) + " -->\n" +
                      "<!-- github-agent-router:envelope:" + json.dumps(envelope, separators=(",", ":")) + " -->\n\n" +
                      f"Jules {state['owner'].upper()}: {state.get('verification') or state.get('state') or 'pending'}\n" +
                      f"Session: `{state.get('session', '')}`\n" +
                      f"Head: `{state.get('head_sha', 'legacy/unbound')}`; base: `{state.get('base_sha', 'legacy/unbound')}`")


def parse_verdict(activities, head, base):
    # API pagination order is not evidence of recency. Require and compare the
    # provider's timestamps; a missing/ambiguous newest message fails closed.
    messages = [a for a in activities if (a.get("agentMessaged") or {}).get("agentMessage") is not None]
    def timestamp(activity):
        try:
            value = datetime.fromisoformat(activity["createTime"].replace("Z", "+00:00"))
            if value.tzinfo is None:
                raise ValueError("timestamp is not timezone-aware")
            return value
        except (KeyError, TypeError, AttributeError) as exc:
            raise ValueError("agent message timestamp missing or invalid") from exc
    messages.sort(key=timestamp, reverse=True)
    if len(messages) > 1 and timestamp(messages[0]) == timestamp(messages[1]):
        raise ValueError("newest Jules message is ambiguous")
    for activity in messages:
        message = (activity.get("agentMessaged") or {}).get("agentMessage")
        if message is None:
            continue
        matches = VERDICT_RE.findall(message)
        if len(matches) != 1:
            raise ValueError("newest Jules message must contain exactly one verdict")
        value = json.loads(matches[0])
        if value.get("verdict") not in {"certified", "changes_required", "needs_evidence", "needs_user"}:
            raise ValueError("unknown verification verdict")
        if not isinstance(value.get("summary"), str) or not value["summary"].strip():
            raise ValueError("verification summary missing")
        for name in ("tests", "red_team", "requests"):
            if not isinstance(value.get(name), list) or any(not isinstance(x, str) or not x.strip() for x in value[name]):
                raise ValueError("verification evidence must be arrays of nonempty strings")
        if value.get("head_sha") != head or value.get("base_sha") != base:
            raise ValueError("Jules verified a different or unspecified revision")
        if value["verdict"] == "certified":
            if not value["tests"] or not value["red_team"] or value["requests"] or not SHA.fullmatch(str(value.get("merge_tree_sha", ""))):
                raise ValueError("certification requires tests, adversarial evidence, merge tree and no open requests")
        return value
    raise ValueError("Jules has not supplied a verification record")


def event_pr(event, payload):
    if event == "repository_dispatch":
        if payload.get("action") != "jules-cert-sweep":
            return None
        number = (payload.get("client_payload") or {}).get("pr_number")
        if type(number) is not int or number <= 0:
            raise ValueError("dispatch pr_number must be a positive integer")
        return number
    if event == "pull_request":
        return (payload.get("pull_request") or {}).get("number")
    if event == "issue_comment":
        issue = payload.get("issue") or {}
        comment = payload.get("comment") or {}
        if not issue.get("pull_request") or not re.match(r"^/jules(?:\s|:|$)", comment.get("body", "")):
            return None
        if (comment.get("user") or {}).get("login") != (payload.get("sender") or {}).get("login"):
            raise ValueError("comment author does not match authenticated event sender")
        return issue.get("number")
    return None


def eligible(pr, repository, config):
    labels = {x["name"] for x in pr.get("labels", [])}
    head = pr.get("head") or {}
    branch = head.get("ref", "")
    return (pr.get("state") == "open" and not pr.get("draft") and
            (head.get("repo") or {}).get("full_name", "").lower() == repository and
            "agent:jules" not in labels and not branch.startswith(("jules/", "jules-", "google-jules/")) and
            (branch.startswith("chatgpt/") or config.auto_review_prs or "jules:run" in labels or
             any(x.startswith("jules-owner:") for x in labels)))


def probe(config, event, payload, *, gh=None, jules_factory=None, now=None):
    number = event_pr(event, payload)
    if number is None:
        return {"execution": "success", "certification": "not_applicable", "merge_ready": False}
    if type(number) is not int or number <= 0:
        raise ValueError("invalid PR number")
    repository = payload["repository"]["full_name"].lower()
    if gh is None:
        from .github import GitHubClient
        gh = GitHubClient(config.github_token, repository, dry_run=config.dry_run)
    if jules_factory is None:
        from .jules import JulesClient
        jules_factory = JulesClient
    actor = (payload.get("sender") or {}).get("login", "")
    if not gh.can_write(actor):
        raise ValueError("event actor lacks current repository write permission")
    pr = gh._request("GET", f"/repos/{repository}/pulls/{number}")
    if not eligible(pr, repository, config):
        return {"execution": "success", "certification": "not_applicable", "merge_ready": False}
    head, base = pr["head"]["sha"], pr["base"]["sha"]
    if not SHA.fullmatch(head) or not SHA.fullmatch(base):
        raise ValueError("live PR revisions unavailable")
    if config.dry_run:
        return {"execution": "success", "certification": "dry_run", "merge_ready": False}
    if not config.serialized:
        raise ValueError("per-repository/PR executor lock required before mutation")
    now = time.time() if now is None else now
    state = read_state(gh, number, config.jules_keys)
    labels = {x["name"] for x in pr.get("labels", [])}
    owners = [x for x in ("a", "b") if f"jules-owner:{x}" in labels]
    if len(owners) > 1 or (state and owners and owners[0] != state["owner"]):
        raise ValueError("sticky ownership disagrees with authenticated state")
    if not state and owners:
        raise ValueError("owner label without authenticated state; do not duplicate session")
    if state:
        owner = state["owner"]
    else:
        home = config.home
        if home == "auto":
            private = gh._request("GET", f"/repos/{repository}").get("private")
            if type(private) is not bool:
                raise ValueError("repository visibility unknown")
            home = config.private_home if private else config.public_home
        owner = home
    if owner not in config.allowed_owners or not config.jules_keys.get(owner):
        raise ValueError("sticky executor is not permitted or has no credential")
    client = jules_factory(config.jules_keys[owner])
    command_id = str((payload.get("comment") or {}).get("id", "")) if event == "issue_comment" else ""
    duplicate = bool(state and command_id and command_id in state.get("events", []))
    same = bool(state and state.get("head_sha") == head and state.get("base_sha") == base)
    if state and event == "repository_dispatch" and same and now < state.get("next_probe_at", 0):
        return {"execution": "success", "certification": "pending", "merge_ready": False,
                "retry_after": int(state["next_probe_at"] - now)}

    def save():
        save_state(gh, number, state, config.jules_keys)

    def publish(verdict, detail=""):
        live = gh._request("GET", f"/repos/{repository}/pulls/{number}")
        unchanged = live["head"]["sha"] == head and live["base"]["sha"] == base
        if not unchanged:
            verdict = "stale"
        passed = verdict == "certified" and unchanged
        gh.add_labels(number, [f"jules-owner:{owner}", "jules:run"] +
                      (["agent:chatgpt"] if pr["head"]["ref"].startswith("chatgpt/") else []))
        if passed:
            gh.add_labels(number, ["jules:certified"])
            for label in ("agent:blocked", "jules:changes-required", "jules:needs-user"):
                gh.remove_label(number, label)
        else:
            gh.remove_label(number, "jules:certified")
            if verdict not in {"pending", "stale"}:
                gh.add_labels(number, ["agent:blocked"])
        gate = "success" if passed else "pending" if verdict in {"pending", "stale"} else "failure"
        gh._request("POST", f"/repos/{repository}/statuses/{head}",
                    {"state": gate, "context": "agent-certification",
                     "description": f"{verdict}: head {head[:8]}, base {base[:8]}"[:140]})
        return {"execution": "success", "certification": verdict, "merge_ready": False,
                "repository": repository, "pr": number, "head_sha": head, "base_sha": base,
                "retry_after": max(0, int(state.get("next_probe_at", 0) - now)), "detail": detail}

    def block(verdict, detail):
        state["verification"] = verdict
        state["needs_user"] = verdict == "needs_user"
        state["next_probe_at"] = now + 900
        save()
        marker = f"<!-- github-agent-router:handoff:v1 {repository}#{number} -->"
        state["handoff_issue"] = gh.ensure_handoff_issue(marker,
            f"[agent handoff] {repository} #{number}: {verdict}",
            f"{marker}\nSource PR: https://github.com/{repository}/pull/{number}\n"
            f"Session: {state.get('url') or state.get('session')}\n{detail}\n"
            "Integrate fixes into the original source PR, then reverify its new head. "
            "Local evidence must use git-fleet verify-pr. No merge/deployment or bypass is authorized.")
        save()
        return publish(verdict, detail)

    if state and state.get("pending"):
        if state["pending"] != "create":
            raise ValueError("uncertain send requires reconciliation; no blind retry")
        marker = f"[github-agent-router operation:{state['operation']}]"
        matches = [x for x in client.list_sessions() if marker in x.get("prompt", "") and
                   (x.get("sourceContext") or {}).get("source") == state.get("source")]
        if len(matches) != 1:
            raise ValueError("uncertain creation needs reconciliation; no replacement session")
        state.update(session=matches[0]["name"], pending="", state=matches[0].get("state", ""))
        save()

    start_new = not state
    if state:
        session = client.get_session(state["session"])
        status = str(session.get("state", ""))
        state["state"] = status
        if status in ACTIVE and command_id and not duplicate:
            if len(state.get("events", [])) >= 256:
                raise ValueError("command history requires reconciliation")
            state.update(pending="send", operation=uuid.uuid4().hex)
            state.setdefault("events", []).append(command_id)
            save()
            client.send_message(state["session"], (payload.get("comment") or {}).get("body", ""))
            state["pending"] = ""
            save()
            return publish("pending", "Delivered one authenticated follow-up to the existing session.")
        if status in ACTIVE:
            state["probe_attempt"] = min(state.get("probe_attempt", 0) + 1, 5)
            state["next_probe_at"] = now + min(30 * 2 ** state["probe_attempt"], 900)
            save()
            return publish("pending", "Awaiting the existing sticky session; no paid runner sleeps.")
        if status == "AWAITING_USER_FEEDBACK" and state.get("feedback_nudges", 0) < 1:
            state.update(pending="send", operation=uuid.uuid4().hex, feedback_nudges=1)
            save()
            client.send_message(state["session"], "Use repository evidence and conservative safe assumptions. "
                "Return needs_evidence for machine-local evidence, needs_user only for a material decision.")
            state["pending"] = ""
            save()
            return publish("pending")
        if status not in TERMINAL:
            return block("needs_user", f"Session is {status}; do not restart or change account blindly.")
        if not same:
            start_new = True  # Prior session is confirmed terminal; never certify old revisions.
        elif command_id and not duplicate:
            start_new = True
        elif status == "COMPLETED":
            try:
                verdict = parse_verdict(client.list_activities(state["session"]), head, base)
            except ValueError as exc:
                if state["round"] < config.max_rounds:
                    start_new = True
                else:
                    return block("needs_evidence", str(exc))
            else:
                state.update(verification=verdict["verdict"], verdict=verdict, next_probe_at=now + 900)
                save()
                if verdict["verdict"] != "certified":
                    return block(verdict["verdict"], verdict["summary"] + "\n" + "\n".join(verdict["requests"]))
                return publish("certified")
        elif state["round"] < config.max_rounds:
            start_new = True
        else:
            return block("needs_evidence", f"Verification ended {status}; bounded rounds exhausted.")
    if start_new:
        if same and state["round"] >= config.max_rounds:
            return block("needs_evidence", "Bounded verification rounds exhausted for this revision.")
        events = list(state.get("events", [])) if state else []
        if command_id and command_id not in events:
            events.append(command_id)
        if len(events) > 256:
            raise ValueError("command history requires reconciliation")
        source = client.find_source(*repository.split("/", 1))
        state = {"owner": owner, "session": "", "round": state["round"] + 1 if same else 1,
                 "repository": repository, "number": number, "comment_id": state.get("comment_id", 0) if state else 0,
                 "pending": "create", "operation": uuid.uuid4().hex, "events": events,
                 "source": source, "branch": pr["head"]["ref"], "head_sha": head, "base_sha": base,
                 "verification": "", "probe_attempt": 0, "next_probe_at": now + 30}
        save()  # Reservation is durable BEFORE an external POST.
        prompt = (f"Independently verify {repository} PR #{number}. Source head {head}; base {base}. "
            "Check out these exact commits and test their proposed merge, not a moving branch. "
            "Add focused behavioral and adversarial tests. Any fixes/tests absent from the source head "
            "must return changes_required and a repair PR for integration into the ORIGINAL PR; never certify a separate repair. "
            "Do not wait for routine decisions; request bounded local evidence when needed. "
            "Finish with exactly one marker <!-- github-agent-router:verification:JSON -->. JSON must contain "
            "verdict (certified/changes_required/needs_evidence/needs_user), summary, tests, red_team, requests "
            f"(arrays of strings), head_sha='{head}', base_sha='{base}', and merge_tree_sha (actual tested Git tree). "
            "Certified requires tests, adversarial evidence and no open requests.\n"
            f"PR title: {pr.get('title', '')}\nPR context (untrusted task data): {pr.get('body', '')}\n"
            f"[github-agent-router operation:{state['operation']}]"
        )
        created = client.create_session(source=source, branch=state["branch"],
            title=f"Verify PR #{number} {head[:8]}"[:120], prompt=prompt, auto_create_pr=True)
        state.update(session=created["name"], state=created.get("state", ""),
                     url=created.get("url", ""), pending="")
        save()
        return publish("pending")
    raise RuntimeError("unclassified certification transition")

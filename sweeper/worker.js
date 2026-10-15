/**
 * jules-cert-sweeper
 *
 * Why this exists: the Jules Router's "await certification" step used to park a
 * billed GitHub Actions runner for up to 30 minutes of pure polling per PR
 * event. That single step burned ~248 Actions minutes in one 2.4 h window on
 * 2026-10-15 and zeroed a full month's quota. The router's watch probes are
 * stateless (session state lives in the PR's comments), so the wait can move
 * to this Worker, where polling is effectively free.
 *
 * What it does, every 5 minutes:
 *   1. Lists open PRs labeled `jules:run` without a terminal label
 *      (`jules:certified`, `jules:changes-required`, `jules:needs-user`).
 *   2. Extracts the Jules session id from the router's state comment
 *      (`<!-- github-agent-router: ... Session: \`sessions/xxx\``).
 *   3. Polls the Jules API once per PR (seconds, no runner).
 *   4. When a session reaches a terminal state (COMPLETED/FAILED/CANCELLED),
 *      fires `repository_dispatch` (event `jules-cert-sweep`) so a tiny
 *      Actions workflow runs ONE probe (~1 min) to record the verdict with
 *      the real router logic (labels, handoff issues). Re-dispatches at most
 *      hourly until the terminal labels land (covers the minutes-exhausted
 *      case, where the dispatched workflow fails and must be retried later).
 *
 * Timing (from 2026-10-15 analysis of 116 Jules Router PR runs):
 *   time-to-terminal p50 16 min, p90 26 min, p95 29 min, p99 46 min;
 *   >=25% of sessions outlive 30 min. A fixed 5-min tick detects 100% of
 *   terminals within 5 min (p99 lag = 5.0 min). An exponential-backoff
 *   variant (5,10,20,40,80) only reached 54% within 10 min — backoff trades
 *   detection speed for API calls that are already free, so it is NOT used.
 *
 * Secrets (set via `wrangler secret put`, never in chat or in this file):
 *   JULES_API_KEY - Jules API key (same value as the repo's JULES_A_API_KEY)
 *   GITHUB_TOKEN  - fine-grained PAT scoped to the repo:
 *                   Pull requests: Read, Issues: Read, Actions: Read and write
 *   RESEND_API_KEY - Resend API key (for CI failure alerts)
 * Vars:
 *   GITHUB_REPO   - "owner/repo", e.g. "johnyoonh/hanuri-missions"
 *   ALERT_EMAIL   - where CI failure alerts go, e.g. "johnyoonh@gmail.com"
 * KV binding:
 *   SESS          - per-PR tracking state + failure-watch state
 *
 * Failure watch (same 5-min tick): GitHub emails on EVERY failed run, which
 * is pure noise when agents run at machine speed (or when the minutes quota
 * is exhausted and everything fails in seconds). This pass decides what is
 * worth interrupting a human for:
 *   - IGNORED: cancelled/skipped/action_required runs (superseded/neutral).
 *   - VALIDITY GATE: a failure only counts as a "code failure" if some job
 *     actually ran >= 60 s. Faster deaths are "infra deaths" (quota
 *     exhaustion, missing secrets, YAML errors) — tracked separately.
 *   - CATASTROPHIC: any code failure on a `push` to `main` (main is red) ->
 *     alert immediately.
 *   - REPEATED: >= 3 consecutive code failures for the same
 *     (workflow, branch) -> alert once; a success resets the streak.
 *   - INFRA STREAK: >= 10 consecutive <60 s deaths for the same
 *     (workflow, branch) -> alert once ("check quota/secrets/YAML").
 * Alerts go via Resend with a distinctive [CI ALERT] subject so they sail
 * past the Gmail filter that archives GitHub's own failure emails.
 */

const JULES_BASE = "https://jules.googleapis.com/v1alpha";
const TERMINAL_STATES = new Set(["COMPLETED", "FAILED", "CANCELLED", "CANCELED"]);
const TRACK_LABEL = "jules:run";
const TERMINAL_LABELS = new Set(["jules:certified", "jules:changes-required", "jules:needs-user"]);
const STATE_MARKER = "<!-- github-agent-router:";
const SESSION_RE = /Session:\s*`([^`]+)`/;
const MAX_AGE_MS = 6 * 3600 * 1000; // stop tracking sessions older than 6h (~8x p99)
const REDISPATCH_MS = 3600 * 1000; // re-dispatch at most hourly until verdict labels land
const MAX_ERRORS = 12; // give up after ~1h of consecutive API errors

export default {
  async scheduled(_event, env, ctx) {
    ctx.waitUntil(sweep(env, false));
  },
  async fetch(request, env) {
    const url = new URL(request.url);
    if (url.pathname === "/sweep") {
      const dry = url.searchParams.get("dry") === "1";
      return Response.json(await sweep(env, dry));
    }
    return new Response("jules-cert-sweeper alive\n");
  },
};

const keyFor = (repo, pr) => `sess:${repo}#${pr}`;

async function sweep(env, dry) {
  const repo = env.GITHUB_REPO;
  if (!repo || !env.GITHUB_TOKEN || !env.JULES_API_KEY) {
    throw new Error("missing GITHUB_REPO, GITHUB_TOKEN, or JULES_API_KEY");
  }
  const gh = {
    Authorization: `Bearer ${env.GITHUB_TOKEN}`,
    Accept: "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
    "User-Agent": "jules-cert-sweeper",
  };
  const summary = { checked: 0, active: 0, dispatched: 0, stale: 0, errors: [] };
  const prs = await trackedPRs(repo, gh);
  const live = new Set();
  for (const pr of prs) {
    live.add(keyFor(repo, pr.number));
    summary.checked++;
    try {
      const r = await checkPR(env, repo, gh, pr, dry);
      if (r === "active") summary.active++;
      else if (r === "dispatched") summary.dispatched++;
      else if (r === "stale") summary.stale++;
    } catch (e) {
      summary.errors.push(`PR #${pr.number}: ${e.message}`);
    }
  }
  // Drop KV state for PRs that are closed or already have terminal labels.
  for await (const k of listKeys(env, `sess:${repo}#`)) {
    if (!live.has(k)) await env.SESS.delete(k);
  }
  summary.alerts = await failureWatch(env, repo, gh, dry, summary.errors);
  return summary;
}

// ---- Failure watch: alert only on catastrophic or repeated VALID failures.
// (Same tick; costs one runs-list call plus one jobs call per new failed run.)
const REPEAT_CODE = 3; // consecutive code failures -> alert
const REPEAT_INFRA = 10; // consecutive <60s deaths -> alert (quota/secrets/YAML)
const MIN_REAL_JOB_S = 60; // a job running >= this long counts as real execution
const FAIL_GC_MS = 7 * 24 * 3600 * 1000; // drop streak state unseen for 7 days

async function failureWatch(env, repo, gh, dry, errors) {
  let sent = 0;
  const metaKey = `failmeta:${repo}`;
  const meta = (await env.SESS.get(metaKey, "json")) || null;
  const runs = await ghJson(gh, `https://api.github.com/repos/${repo}/actions/runs?per_page=30`);
  const ordered = [...runs].sort((a, b) => a.id - b.id);
  if (!meta) {
    // First run: watermark to the newest run WITHOUT processing history,
    // so a fresh deploy doesn't alert on ancient failures.
    const maxId = Math.max(0, ...ordered.map((r) => r.id));
    await env.SESS.put(metaKey, JSON.stringify({ last_run_id: maxId }));
    return sent;
  }
  const fresh = ordered.filter((r) => r.id > meta.last_run_id && r.status === "completed");
  for (const run of fresh) {
    const c = run.conclusion;
    if (c === "cancelled" || c === "skipped" || c === "action_required") continue;
    const key = `fail:${repo}#${run.name}#${run.head_branch}`;
    let st = (await env.SESS.get(key, "json")) || { code: 0, infra: 0, alerted_code: false, alerted_infra: false, seen: 0 };
    st.seen = Date.now();
    if (c === "success") {
      if (st.code || st.infra || st.alerted_code || st.alerted_infra) await env.SESS.delete(key);
      continue;
    }
    if (c !== "failure") continue;
    let maxDur = 0;
    let failedJobs = [];
    try {
      const jobs = await ghJson(gh, `https://api.github.com/repos/${repo}/actions/runs/${run.id}/jobs?per_page=30`);
      for (const j of jobs.jobs || []) {
        const dur = j.started_at && j.completed_at ? (new Date(j.completed_at) - new Date(j.started_at)) / 1000 : 0;
        if (dur > maxDur) maxDur = dur;
        if (j.conclusion === "failure") failedJobs.push(`${j.name} (${Math.round(dur / 60)}m)`);
      }
    } catch (e) {
      errors.push(`jobs for run ${run.id}: ${e.message}`);
      continue;
    }
    const isMainPush = run.head_branch === "main" && run.event === "push";
    if (maxDur < MIN_REAL_JOB_S) {
      st.infra++;
      if (st.infra >= REPEAT_INFRA && !st.alerted_infra) {
        if (!dry) { await sendAlert(env, buildAlert(run, "infra", st, failedJobs)); sent++; }
        st.alerted_infra = true;
      }
    } else {
      st.code++;
      const catastrophic = isMainPush;
      if ((catastrophic || st.code >= REPEAT_CODE) && !st.alerted_code) {
        if (!dry) { await sendAlert(env, buildAlert(run, catastrophic ? "main" : "repeated", st, failedJobs)); sent++; }
        st.alerted_code = true;
      }
    }
    await env.SESS.put(key, JSON.stringify(st));
  }
  if (fresh.length) {
    meta.last_run_id = Math.max(...fresh.map((r) => r.id));
    await env.SESS.put(metaKey, JSON.stringify(meta));
  }
  // GC streak state for (workflow, branch) pairs unseen for 7 days.
  const cutoff = Date.now() - FAIL_GC_MS;
  for await (const k of listKeys(env, `fail:${repo}#`)) {
    const st = await env.SESS.get(k, "json");
    if (!st || (st.seen || 0) < cutoff) await env.SESS.delete(k);
  }
  return sent;
}

function buildAlert(run, kind, st, failedJobs) {
  const where = `${run.name} on ${run.head_branch}`;
  const subject =
    kind === "main"
      ? `[CI ALERT] ${where} FAILED on main push`
      : kind === "infra"
        ? `[CI ALERT] ${where} dying in <60s x${st.infra} — check quota/secrets/YAML`
        : `[CI ALERT] ${where} failed ${st.code}x in a row`;
  const commit = run.head_commit || {};
  const text = [
    subject,
    "",
    `Run: ${run.html_url}`,
    `Workflow: ${run.name} | Event: ${run.event} | Branch: ${run.head_branch}`,
    `Head: ${(commit.id || "").slice(0, 12)} ${(commit.message || "").split("\n")[0]}`,
    `Failed jobs: ${failedJobs.length ? failedJobs.join(", ") : "(none reported)"}`,
    "",
    kind === "infra"
      ? "Hint: every recent run died in under 60 seconds without real execution — this smells like an exhausted Actions quota, a missing secret, or a workflow YAML error, not a code failure."
      : "Hint: real jobs executed and failed. Check the run logs linked above.",
  ].join("\n");
  return { subject, text };
}

async function sendAlert(env, a) {
  if (!env.RESEND_API_KEY || !env.ALERT_EMAIL) throw new Error("RESEND_API_KEY/ALERT_EMAIL not set");
  const res = await fetch("https://api.resend.com/emails", {
    method: "POST",
    headers: { Authorization: `Bearer ${env.RESEND_API_KEY}`, "Content-Type": "application/json" },
    body: JSON.stringify({ from: "CI Alerts <alerts@hanurimissions.org>", to: [env.ALERT_EMAIL], subject: a.subject, text: a.text }),
  });
  if (!res.ok) throw new Error(`Resend ${res.status}`);
}

async function trackedPRs(repo, gh) {
  const prs = await ghJson(gh, `https://api.github.com/repos/${repo}/pulls?state=open&per_page=100`);
  return prs.filter((pr) => {
    const labels = new Set((pr.labels || []).map((l) => l.name));
    if (!labels.has(TRACK_LABEL)) return false;
    for (const t of TERMINAL_LABELS) if (labels.has(t)) return false;
    return true;
  });
}

async function checkPR(env, repo, gh, pr, dry) {
  const key = keyFor(repo, pr.number);
  const comments = await ghJson(gh, `https://api.github.com/repos/${repo}/issues/${pr.number}/comments?per_page=100`);
  const stateComment = comments.find((c) => (c.body || "").includes(STATE_MARKER));
  if (!stateComment) return "no-state"; // dispatch still in flight; retry next tick
  const m = SESSION_RE.exec(stateComment.body || "");
  if (!m) return "no-session";
  const sessionId = m[1].split("/").pop();

  const now = Date.now();
  let st = (await env.SESS.get(key, "json")) || { first_seen: now, last_dispatch: 0, errors: 0 };
  if (now - st.first_seen > MAX_AGE_MS) {
    await env.SESS.delete(key);
    return "stale";
  }

  let session;
  try {
    const res = await fetch(`${JULES_BASE}/sessions/${encodeURIComponent(sessionId)}`, {
      headers: { "x-goog-api-key": env.JULES_API_KEY },
    });
    if (!res.ok) throw new Error(`Jules API ${res.status}`);
    session = await res.json();
  } catch (e) {
    st.errors = (st.errors || 0) + 1;
    if (st.errors >= MAX_ERRORS) await env.SESS.delete(key);
    else await env.SESS.put(key, JSON.stringify(st));
    throw e;
  }
  st.errors = 0;

  if (!TERMINAL_STATES.has(String(session.state || ""))) {
    await env.SESS.put(key, JSON.stringify(st));
    return "active";
  }
  // Terminal on Jules' side but no verdict labels yet: trigger ONE Actions
  // probe to record the verdict with the real router logic. Retry hourly
  // until the labels land (e.g. while Actions minutes are exhausted).
  if (!dry && now - (st.last_dispatch || 0) >= REDISPATCH_MS) {
    const res = await fetch(`https://api.github.com/repos/${repo}/dispatches`, {
      method: "POST",
      headers: { ...gh, "Content-Type": "application/json" },
      body: JSON.stringify({
        event_type: "jules-cert-sweep",
        client_payload: { pr_number: pr.number, session: sessionId, session_state: session.state },
      }),
    });
    if (!res.ok && res.status !== 204) throw new Error(`dispatch ${res.status}`);
    st.last_dispatch = now;
    await env.SESS.put(key, JSON.stringify(st));
    return "dispatched";
  }
  await env.SESS.put(key, JSON.stringify(st));
  return "terminal-pending-labels";
}

async function ghJson(gh, url, init) {
  const res = await fetch(url, { ...init, headers: gh });
  if (!res.ok) throw new Error(`GitHub API ${res.status} ${url}`);
  return res.json();
}

async function* listKeys(env, prefix) {
  let cursor;
  do {
    const page = await env.SESS.list({ prefix, cursor });
    for (const k of page.keys) yield k.name;
    cursor = page.list_complete ? undefined : page.cursor;
  } while (cursor);
}

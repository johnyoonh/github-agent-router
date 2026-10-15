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
 * Vars:
 *   GITHUB_REPO   - "owner/repo", e.g. "johnyoonh/hanuri-missions"
 * KV binding:
 *   SESS          - per-PR tracking state
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
  return summary;
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

/** Advisory cron sweeper. Only the serialized router may mutate Jules/state.
 * KV is a best-effort dispatch ledger, NOT a distributed lock. Duplicate hints
 * must remain safe at the authenticated, revision-bound GitHub consumer.
 */
const ENVELOPE = /<!-- github-agent-router:envelope:(\{.*?\}) -->/gs;
const TERMINAL = new Set(["COMPLETED", "FAILED", "CANCELLED", "CANCELED", "AWAITING_USER_FEEDBACK", "AWAITING_PLAN_APPROVAL"]);
const VERDICTS = new Set(["certified", "changes_required", "needs_evidence", "needs_user"]);
const encoder = new TextEncoder();

export default {
  async scheduled(event, env, ctx) {
    if (event && event.cron === DIGEST_CRON) {
      ctx.waitUntil(sendDigest(env));
    } else {
      ctx.waitUntil(sweep(env));
      ctx.waitUntil(failureWatch(env));
    }
  },
  async fetch(request) {
    // No unauthenticated path may spend API calls or create Actions runs.
    return new URL(request.url).pathname === "/health"
      ? new Response("jules-cert-sweeper alive\n")
      : new Response("Not found\n", { status: 404 });
  },
};

async function hmacKey(raw, usages) {
  return crypto.subtle.importKey("raw", raw, { name: "HMAC", hash: "SHA-256" }, false, usages);
}

export function parseRepoList(str) {
  if (!str) return [];
  return str.split(/[,\s]+/).map(s => s.trim().toLowerCase()).filter(Boolean);
}

export async function resolveRepos(env, request, headers) {
  const allowList = parseRepoList(env.ALLOW_REPOS || env.GITHUB_REPOS_ALLOW || env.GITHUB_REPOS || env.GITHUB_REPO || "");
  const disallowSet = new Set(parseRepoList(env.DISALLOW_REPOS || env.GITHUB_REPOS_DISALLOW || env.EXCLUDE_REPOS || ""));

  let candidates = [];
  if (allowList.length && !allowList.includes("*") && !allowList.includes("all")) {
    candidates = allowList;
  } else {
    // Discover accessible repositories from GitHub, sorted by most recently pushed
    const userRepos = await request("https://api.github.com/user/repos?affiliation=owner,collaborator&sort=pushed&per_page=100", { headers });
    if (Array.isArray(userRepos)) {
      candidates = userRepos
        .filter(r => !r.archived && !r.disabled && r.full_name)
        .map(r => r.full_name.toLowerCase());
    }
  }

  const result = [];
  const seen = new Set();
  for (const r of candidates) {
    if (!disallowSet.has(r) && !seen.has(r) && /^[A-Za-z0-9_.-]+\/[A-Za-z0-9_.-]+$/.test(r)) {
      seen.add(r);
      result.push(r);
    }
  }
  return result;
}

export async function decodeState(comment, repo, number, key, owner) {
  const matches = [...String(comment.body || "").matchAll(ENVELOPE)];
  if (matches.length !== 1) return null;
  const envelope = JSON.parse(matches[0][1]);
  if (typeof envelope.payload !== "string" || envelope.payload.length > 28000 ||
      !/^[0-9a-f]{64}$/.test(envelope.signature || "")) throw new Error("invalid state envelope");
  const payload = Uint8Array.from(atob(envelope.payload), c => c.charCodeAt(0));
  if (payload.length > 20000) throw new Error("state budget exceeded");
  const state = JSON.parse(new TextDecoder().decode(payload));
  if (owner && state.owner !== owner) throw new Error("state scope/account mismatch");
  const selectedKey = typeof key === "object" && key !== null ? (key[state.owner] || "") : key;
  if (!selectedKey) throw new Error("missing key for executor " + state.owner);
  const derived = await crypto.subtle.sign("HMAC", await hmacKey(encoder.encode(selectedKey), ["sign"]),
    encoder.encode("github-agent-router/state/v1"));
  const signature = Uint8Array.from(envelope.signature.match(/../g), h => parseInt(h, 16));
  if (!await crypto.subtle.verify("HMAC", await hmacKey(derived, ["verify"]), signature, payload))
    throw new Error("state signature invalid; reconcile keys");
  if (state.comment_id !== comment.id) return null; // Copies are not canonical state.
  if (state.repository !== repo || state.number !== number ||
      !/^sessions\/[A-Za-z0-9_-]+$/.test(state.session || ""))
    throw new Error("state scope/account mismatch");
  return { ...state, fingerprint: envelope.signature };
}

export async function sweep(env, dry = false, options = {}) {
  const keys = {
    a: env.JULES_A_API_KEY || (env.JULES_OWNER === "a" || !env.JULES_OWNER ? env.JULES_API_KEY : ""),
    b: env.JULES_B_API_KEY || (env.JULES_OWNER === "b" ? env.JULES_API_KEY : ""),
  };
  const hasKey = Boolean(keys.a || keys.b || env.JULES_API_KEY);
  if (!env.GITHUB_TOKEN || !hasKey || !env.SESS)
    throw new Error("missing or invalid sweeper bindings");
  const fetcher = options.fetcher || fetch;
  const now = options.now || Date.now;
  const started = now();
  const budget = Number(env.MAX_DISPATCHES || 5);
  if (!Number.isInteger(budget) || budget < 1 || budget > 20) throw new Error("invalid dispatch budget");
  let calls = 0;
  const gh = { Authorization: `Bearer ${env.GITHUB_TOKEN}`, Accept: "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "jules-cert-sweeper" };
  const summary = { checked: 0, settled: 0, active: 0, dispatched: 0, reserved: 0, errors: [] };
  async function request(url, init = {}) {
    if (++calls > 80 || now() - started > 180000) throw new Error("bounded sweep budget reached");
    const response = await fetcher(url, { ...init, redirect: "error", signal: AbortSignal.timeout(15000) });
    if (!response.ok) throw new Error(`API returned ${response.status}`);
    return response.status === 204 ? null : response.json();
  }
  async function pages(targetRepo, path) {
    const rows = [];
    for (let page = 1; page <= 10; page++) {
      const batch = await request(`https://api.github.com/repos/${targetRepo}/${path}${path.includes("?") ? "&" : "?"}per_page=100&page=${page}`, { headers: gh });
      if (!Array.isArray(batch)) throw new Error("invalid paginated response");
      rows.push(...batch);
      if (batch.length < 100) return rows;
    }
    throw new Error("pagination exceeds bounded sweep");
  }

  const repoList = await resolveRepos(env, request, gh);
  if (!repoList.length) {
    return { ...summary, dry_run: dry, api_calls: calls, repos_checked: 0 };
  }

  for (const repo of repoList) {
    if (summary.dispatched >= budget) break;
    for (const pr of await pages(repo, "pulls?state=open")) {
      const labels = new Set((pr.labels || []).map(x => x.name));
      if (!(labels.has("jules:run") || [...labels].some(x => x.startsWith("jules-owner:")))) continue;
      if (pr.draft || pr.head?.repo?.full_name?.toLowerCase() !== repo.toLowerCase()) continue;
      summary.checked++;
      try {
        const states = [];
        for (const comment of await pages(repo, `issues/${pr.number}/comments`)) {
          const state = await decodeState(comment, repo.toLowerCase(), pr.number, keys, env.JULES_OWNER);
          if (state) states.push(state);
        }
        if (states.length > 1) throw new Error("multiple canonical states require reconciliation");
        if (!states.length) continue; // Legacy records need one protocol-2 router event.
        const state = states[0];
        if (state.pending) throw new Error("uncertain router operation requires reconciliation");
        const current = state.head_sha === pr.head.sha && state.base_sha === pr.base.sha;
        if (current && VERDICTS.has(state.verification)) { summary.settled++; continue; }
        const key = `sess:${repo}#${pr.number}`;
        const fingerprint = `${state.fingerprint}:${pr.head.sha}:${pr.base.sha}`;
        const old = await env.SESS.get(key, "json");
        if (old?.reserved_fingerprint === fingerprint) { summary.reserved++; continue; }
        // Keep fixed five-minute polling; source state may request a later probe.
        if (current && Number(state.next_probe_at || 0) * 1000 > now()) { summary.active++; continue; }
        const julesKey = keys[state.owner] || env.JULES_API_KEY;
        if (!julesKey) throw new Error(`missing key for executor ${state.owner}`);
        const session = await request(`https://jules.googleapis.com/v1alpha/${state.session}`, {
          headers: { "x-goog-api-key": julesKey },
        });
        if (!TERMINAL.has(session.state)) { summary.active++; continue; }
        if (summary.dispatched >= budget) break;
        if (!dry) {
          // Reserve BEFORE the hint. Never delete/retry an uncertain reservation
          // automatically; the real mutation is protected by the consumer's lock.
          await env.SESS.put(key, JSON.stringify({ reserved_fingerprint: fingerprint, reserved_at: now() }));
          await request(`https://api.github.com/repos/${repo}/dispatches`, {
            method: "POST", headers: { ...gh, "Content-Type": "application/json" },
            body: JSON.stringify({ event_type: "jules-cert-sweep", client_payload: {
              pr_number: pr.number, session: state.session, head_sha: pr.head.sha, base_sha: pr.base.sha,
            } }),
          });
        }
        summary.dispatched++;
      } catch (error) {
        // No token, response body, session output or arbitrary external URL in logs.
        summary.errors.push(`PR #${pr.number}: ${error.message}`);
      }
    }
  }
  return { ...summary, dry_run: dry, api_calls: calls, repos_checked: repoList.length };
}

// ---- Failure watch: alert only on catastrophic or repeated VALID failures.
// Runs on the same five-minute tick via the scheduled handler, separate from
// sweep() with its own API budget (sweep()'s tests must never see these
// calls). GitHub emails on EVERY failed run, which is pure noise at agent
// speed or when the minutes quota is exhausted and everything fails in
// seconds. Rules:
//   - IGNORED: cancelled / skipped / action_required runs.
//   - VALIDITY GATE: a failure counts as a *code failure* only if some job
//     actually ran >= 60 s. Faster deaths are *infra deaths* (quota
//     exhaustion, missing secrets, YAML errors), tracked separately.
//   - CATASTROPHIC: any code failure on a push to main -> alert immediately.
//   - REPEATED: >= 3 consecutive code failures for the same (workflow,
//     branch) -> alert once; a success resets the streak.
//   - INFRA STREAK: >= 10 consecutive <60 s deaths for the same
//     (workflow, branch) -> alert once.
// Alerts go via Resend with a [CI ALERT] subject so they sail past the Gmail
// filter that archives GitHub's own failure emails. A fresh deploy watermarks
// to the newest run and processes only new runs — no alert storm on history.
const FW_REPEAT_CODE = 3;
const FW_REPEAT_INFRA = 10;
const FW_MIN_REAL_JOB_S = 60;
const FW_GC_MS = 7 * 24 * 3600 * 1000;
const FW_REPOS_PER_TICK = 25;

export async function failureWatch(env, options = {}) {
  if (!env.GITHUB_TOKEN || !env.SESS) throw new Error("missing sweeper bindings");
  const dry = !!options.dry;
  // Dry runs read real state but make no KV writes and send nothing.
  const store = dry
    ? { get: (k, t) => env.SESS.get(k, t), put: async () => {}, delete: async () => {},
        list: (o) => env.SESS.list(o) }
    : env.SESS;
  const fetcher = options.fetcher || fetch;
  const gh = { Authorization: `Bearer ${env.GITHUB_TOKEN}`, Accept: "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "jules-cert-sweeper" };
  async function request(url, init = {}) {
    const response = await fetcher(url, { ...init, redirect: "error", signal: AbortSignal.timeout(15000) });
    if (!response.ok) throw new Error(`API returned ${response.status}`);
    return response.status === 204 ? null : response.json();
  }
  const repoList = await resolveRepos(env, request, gh);
  const summary = { alerts: 0, repos_checked: 0, errors: [], dry_run: dry };
  if (!repoList.length) return summary;
  // Rotating window: bound per-tick API spend, full fleet covered over ticks.
  const cursorKey = "failcursor";
  const cursor = ((await store.get(cursorKey, "json")) || 0) % repoList.length;
  const batch = [];
  for (let i = 0; i < Math.min(FW_REPOS_PER_TICK, repoList.length); i++) {
    batch.push(repoList[(cursor + i) % repoList.length]);
  }
  await store.put(cursorKey, JSON.stringify((cursor + batch.length) % repoList.length));
  for (const repo of batch) {
    summary.repos_checked++;
    try {
      summary.alerts += await failureWatchRepo(store, env, repo, gh, request, fetcher, dry, summary.errors);
    } catch (error) {
      summary.errors.push(`${repo}: ${error.message}`);
    }
  }
  return summary;
}

async function listKeys(store, prefix) {
  const out = [];
  let cursor;
  do {
    const page = await store.list({ prefix, cursor });
    for (const k of page.keys) out.push(k.name);
    cursor = page.list_complete ? undefined : page.cursor;
  } while (cursor);
  return out;
}

async function failureWatchRepo(store, env, repo, gh, request, fetcher, dry, errors) {
  let sent = 0;
  // Local read-through cache so a tick sees its own writes even when the
  // store is a dry-run no-op wrapper.
  const cache = new Map();
  const getState = async (key) => {
    if (!cache.has(key)) cache.set(key, (await store.get(key, "json")) || null);
    return cache.get(key);
  };
  const putState = async (key, val) => { cache.set(key, val); await store.put(key, JSON.stringify(val)); };
  const delState = async (key) => { cache.delete(key); await store.delete(key); };
  const metaKey = `failmeta:${repo}`;
  const meta = (await store.get(metaKey, "json")) || null;
  const runs = await request(`https://api.github.com/repos/${repo}/actions/runs?per_page=30`, { headers: gh });
  const ordered = [...runs].sort((a, b) => a.id - b.id);
  if (!meta) {
    const maxId = Math.max(0, ...ordered.map((r) => r.id));
    await store.put(metaKey, JSON.stringify({ last_run_id: maxId }));
    return sent;
  }
  const fresh = ordered.filter((r) => r.id > meta.last_run_id && r.status === "completed");
  for (const run of fresh) {
    const c = run.conclusion;
    if (c === "cancelled" || c === "skipped" || c === "action_required") continue;
    const key = `fail:${repo}#${run.name}#${run.head_branch}`;
    let st = (await getState(key)) || { code: 0, infra: 0, alerted_code: false, alerted_infra: false, seen: 0 };
    st.seen = Date.now();
    if (c === "success") {
      if (st.code || st.infra || st.alerted_code || st.alerted_infra) await delState(key);
      continue;
    }
    if (c !== "failure") continue;
    let maxDur = 0;
    const failedJobs = [];
    try {
      const jobs = await request(`https://api.github.com/repos/${repo}/actions/runs/${run.id}/jobs?per_page=30`, { headers: gh });
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
    if (maxDur < FW_MIN_REAL_JOB_S) {
      st.infra++;
      if (st.infra >= FW_REPEAT_INFRA && !st.alerted_infra) {
        if (!dry) { await sendAlert(env, buildAlert(run, "infra", st, failedJobs), fetcher); sent++; }
        else { sent++; }
        st.alerted_infra = true;
        st.alerted_infra_at = Date.now();
      }
    } else {
      st.code++;
      const catastrophic = isMainPush;
      if ((catastrophic || st.code >= FW_REPEAT_CODE) && !st.alerted_code) {
        if (!dry) { await sendAlert(env, buildAlert(run, catastrophic ? "main" : "repeated", st, failedJobs), fetcher); sent++; }
        else { sent++; }
        st.alerted_code = true;
        st.alerted_code_at = Date.now();
      }
    }
    await putState(key, st);
  }
  if (fresh.length) {
    meta.last_run_id = Math.max(...fresh.map((r) => r.id));
    await store.put(metaKey, JSON.stringify(meta));
  }
  const cutoff = Date.now() - FW_GC_MS;
  for (const k of await listKeys(store, `fail:${repo}#`)) {
    const st = await getState(k);
    if (!st || (st.seen || 0) < cutoff) await delState(k);
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

async function sendAlert(env, alert, fetcher) {
  if (!env.RESEND_API_KEY || !env.ALERT_EMAIL) throw new Error("RESEND_API_KEY/ALERT_EMAIL not set");
  const res = await fetcher("https://api.resend.com/emails", {
    method: "POST",
    redirect: "error",
    signal: AbortSignal.timeout(15000),
    headers: { Authorization: `Bearer ${env.RESEND_API_KEY}`, "Content-Type": "application/json" },
    body: JSON.stringify({ from: "CI Alerts <alerts@hanurimissions.org>", to: [env.ALERT_EMAIL], subject: alert.subject, text: alert.text }),
  });
  if (!res.ok) throw new Error(`Resend returned ${res.status}`);
}

// Daily digest: one summary email instead of per-run noise. Wired to the
// second cron trigger ("0 12 * * *", 08:00 EDT). Counts runs in the last 24h
// per workflow, flags sub-minute failures as likely quota/infra (same 60 s
// heuristic as the failure watch, via run duration), and lists open streaks
// plus alerts sent (from the failure-watch KV state).
const DIGEST_CRON = "0 12 * * *";

export async function sendDigest(env, options = {}) {
  if (!env.GITHUB_TOKEN || !env.SESS) throw new Error("missing sweeper bindings");
  const dry = !!options.dry;
  const store = dry
    ? { get: (k, t) => env.SESS.get(k, t), put: async () => {}, delete: async () => {},
        list: (o) => env.SESS.list(o) }
    : env.SESS;
  const fetcher = options.fetcher || fetch;
  const gh = { Authorization: "Bearer " + env.GITHUB_TOKEN, Accept: "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "jules-cert-sweeper" };
  async function request(url, init) {
    const opts = Object.assign({ redirect: "error", signal: AbortSignal.timeout(15000) }, init);
    const response = await fetcher(url, opts);
    if (!response.ok) throw new Error("API returned " + response.status);
    return response.status === 204 ? null : response.json();
  }
  const repoList = await resolveRepos(env, request, gh);
  const since = Date.now() - 24 * 3600 * 1000;
  const sinceIso = new Date(since).toISOString();
  const dateStr = new Date().toISOString().slice(0, 10);
  let totalRuns = 0, totalFailed = 0, alertsSent = 0;
  const lines = [];
  for (const repo of repoList) {
    let runs;
    try {
      runs = await request("https://api.github.com/repos/" + repo + "/actions/runs?per_page=100&created=>" + sinceIso, { headers: gh });
    } catch (e) {
      lines.push(repo + ": could not list runs (" + e.message + ")");
      continue;
    }
    const done = runs.filter((r) => r.status === "completed");
    if (!done.length) {
      lines.push(repo + ": no completed runs in the last 24h.");
      continue;
    }
    totalRuns += done.length;
    const byWf = new Map();
    for (const r of done) {
      let w = byWf.get(r.name);
      if (!w) { w = { runs: 0, failed: 0, infra: 0 }; byWf.set(r.name, w); }
      w.runs++;
      if (r.conclusion === "failure") {
        totalFailed++;
        w.failed++;
        const startMs = new Date(r.run_started_at || r.created_at).getTime();
        const durS = (new Date(r.updated_at).getTime() - startMs) / 1000;
        if (durS < 60) w.infra++;
      }
    }
    let repoFailed = 0;
    for (const w of byWf.values()) repoFailed += w.failed;
    lines.push(repo + ": " + done.length + " runs, " + repoFailed + " failed.");
    const ranked = [...byWf.entries()].sort((a, b) => b[1].failed - a[1].failed);
    for (const entry of ranked) {
      const name = entry[0], w = entry[1];
      if (!w.failed) continue;
      let note = "";
      if (w.infra === w.failed) note = " (all <1 min — likely quota/infra)";
      else if (w.infra) note = " (" + w.infra + " <1 min)";
      lines.push("  - " + name + ": " + w.failed + "/" + w.runs + " failed" + note);
    }
    const prefix = "fail:" + repo + "#";
    for (const k of await listKeys(store, prefix)) {
      const st = await store.get(k, "json");
      if (!st) continue;
      const rest = k.slice(prefix.length).split("#");
      const branch = rest.pop();
      const wf = rest.join("#");
      const open = [];
      if (st.code) open.push(st.code + "x code" + (st.alerted_code ? " (alerted)" : ""));
      if (st.infra) open.push(st.infra + "x infra" + (st.alerted_infra ? " (alerted)" : ""));
      if (open.length) lines.push("  - streak " + wf + " on " + branch + ": " + open.join(", "));
      if (st.alerted_code_at && st.alerted_code_at >= since) alertsSent++;
      if (st.alerted_infra_at && st.alerted_infra_at >= since) alertsSent++;
    }
  }
  const plural = alertsSent === 1 ? "" : "s";
  const subject = "[CI DIGEST] " + dateStr + " — " + totalRuns + " runs, " + totalFailed + " failed, " + alertsSent + " alert" + plural + " sent";
  const text = [subject, ""].concat(lines).join("\n");
  if (!dry) await sendAlert(env, { subject, text }, fetcher);
  return { repos: repoList.length, runs: totalRuns, failed: totalFailed, alerts: alertsSent, dry_run: dry };
}

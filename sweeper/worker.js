/** Advisory cron sweeper. Only the serialized router may mutate Jules/state.
 * KV is a best-effort dispatch ledger, NOT a distributed lock. Duplicate hints
 * must remain safe at the authenticated, revision-bound GitHub consumer.
 */
const ENVELOPE = /<!-- github-agent-router:envelope:(\{.*?\}) -->/gs;
const TERMINAL = new Set(["COMPLETED", "FAILED", "CANCELLED", "CANCELED", "AWAITING_USER_FEEDBACK", "AWAITING_PLAN_APPROVAL"]);
const VERDICTS = new Set(["certified", "changes_required", "needs_evidence", "needs_user"]);
const encoder = new TextEncoder();

export default {
  async scheduled(_event, env, ctx) {
    ctx.waitUntil(sweep(env));
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

export async function decodeState(comment, repo, number, key, owner = "a") {
  const matches = [...String(comment.body || "").matchAll(ENVELOPE)];
  if (matches.length !== 1) return null;
  const envelope = JSON.parse(matches[0][1]);
  if (typeof envelope.payload !== "string" || envelope.payload.length > 28000 ||
      !/^[0-9a-f]{64}$/.test(envelope.signature || "")) throw new Error("invalid state envelope");
  const payload = Uint8Array.from(atob(envelope.payload), c => c.charCodeAt(0));
  if (payload.length > 20000) throw new Error("state budget exceeded");
  const derived = await crypto.subtle.sign("HMAC", await hmacKey(encoder.encode(key), ["sign"]),
    encoder.encode("github-agent-router/state/v1"));
  const signature = Uint8Array.from(envelope.signature.match(/../g), h => parseInt(h, 16));
  if (!await crypto.subtle.verify("HMAC", await hmacKey(derived, ["verify"]), signature, payload))
    throw new Error("state signature invalid; reconcile keys");
  const state = JSON.parse(new TextDecoder().decode(payload));
  if (state.comment_id !== comment.id) return null; // Copies are not canonical state.
  if (state.repository !== repo || state.number !== number || state.owner !== owner ||
      !/^sessions\/[A-Za-z0-9_-]+$/.test(state.session || ""))
    throw new Error("state scope/account mismatch");
  return { ...state, fingerprint: envelope.signature };
}

export async function sweep(env, dry = false, options = {}) {
  const repo = env.GITHUB_REPO;
  if (!/^[A-Za-z0-9_.-]+\/[A-Za-z0-9_.-]+$/.test(repo || "") ||
      !env.GITHUB_TOKEN || !env.JULES_API_KEY || !env.SESS)
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
  async function pages(path) {
    const rows = [];
    for (let page = 1; page <= 10; page++) {
      const batch = await request(`https://api.github.com/repos/${repo}/${path}${path.includes("?") ? "&" : "?"}per_page=100&page=${page}`, { headers: gh });
      if (!Array.isArray(batch)) throw new Error("invalid paginated response");
      rows.push(...batch);
      if (batch.length < 100) return rows;
    }
    throw new Error("pagination exceeds bounded sweep");
  }
  for (const pr of await pages("pulls?state=open")) {
    const labels = new Set((pr.labels || []).map(x => x.name));
    if (!(labels.has("jules:run") || [...labels].some(x => x.startsWith("jules-owner:")))) continue;
    if (pr.draft || pr.head?.repo?.full_name?.toLowerCase() !== repo.toLowerCase()) continue;
    summary.checked++;
    try {
      const states = [];
      for (const comment of await pages(`issues/${pr.number}/comments`)) {
        const state = await decodeState(comment, repo.toLowerCase(), pr.number, env.JULES_API_KEY, env.JULES_OWNER || "a");
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
      const session = await request(`https://jules.googleapis.com/v1alpha/${state.session}`, {
        headers: { "x-goog-api-key": env.JULES_API_KEY },
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
  return { ...summary, dry_run: dry, api_calls: calls };
}

import { test } from "node:test";
import assert from "node:assert/strict";
import { createHmac, webcrypto } from "node:crypto";
import worker, { decodeState, sweep } from "./worker.js";
globalThis.crypto ||= webcrypto;
function comment(changes = {}) {
  const state = { owner: "a", session: "sessions/1", comment_id: 10, number: 7,
    repository: "a/b", head_sha: "a".repeat(40), base_sha: "b".repeat(40), ...changes };
  const payload = Buffer.from(JSON.stringify(state));
  const key = createHmac("sha256", "key").update("github-agent-router/state/v1").digest();
  const signature = createHmac("sha256", key).update(payload).digest("hex");
  return { id: 10, body: `<!-- github-agent-router:envelope:${JSON.stringify({ payload: payload.toString("base64"), signature })} -->` };
}
function fixture(changes = {}) {
  const writes = [], posts = [], reads = [];
  const storage = new Map();
  const env = { GITHUB_REPO: "a/b", GITHUB_TOKEN: "token", JULES_API_KEY: "key",
    SESS: { async get(k) { return storage.get(k); },
      async put(k, v) { writes.push(k); storage.set(k, JSON.parse(v)); } } };
  const pr = { number: 7, labels: [{ name: "jules:run" }], head: { sha: "a".repeat(40), repo: { full_name: "a/b" } }, base: { sha: "b".repeat(40) } };
  const fetcher = async (url, init) => {
    reads.push(url);
    if (url.includes("/pulls?")) return Response.json([pr]);
    if (url.includes("/comments?")) return Response.json([comment(changes)]);
    if (url.includes("jules.googleapis")) return Response.json({ state: "COMPLETED" });
    if (url.endsWith("/dispatches")) {
      assert.equal(writes.length, 1, "reservation must precede POST");
      posts.push(JSON.parse(init.body)); return new Response(null, { status: 204 });
    }
    throw new Error("unexpected URL");
  };
  return { env, fetcher, writes, posts, reads, pr };
}
test("public HTTP cannot trigger even a dry sweep", async () => {
  assert.equal((await worker.fetch(new Request("https://example.test/sweep?dry=1"))).status, 404);
  assert.equal((await worker.fetch(new Request("https://example.test/health"))).status, 200);
});
test("exact payload bytes authenticate Unicode and float spellings", async () => {
  const c = comment({ summary: "\uac80\uc99d", next_probe_at: 1000.0 });
  assert.equal((await decodeState(c, "a/b", 7, "key")).session, "sessions/1");
  await assert.rejects(decodeState(c, "a/b", 7, "wrong-key"));
  assert.equal(await decodeState({ ...c, id: 11 }, "a/b", 7, "key"), null);
});
test("wrong repository or executor is not accepted", async () => {
  await assert.rejects(decodeState(comment(), "a/c", 7, "key"));
  await assert.rejects(decodeState(comment(), "a/b", 7, "key", "b"));
});
test("dry-run has no storage or GitHub writes", async () => {
  const f = fixture();
  const result = await sweep(f.env, true, { fetcher: f.fetcher });
  assert.equal(result.dispatched, 1);
  assert.deepEqual(f.writes, []); assert.deepEqual(f.posts, []);
});
test("settled exact revisions do not call Jules or dispatch", async () => {
  const f = fixture({ verification: "certified" });
  const result = await sweep(f.env, false, { fetcher: f.fetcher });
  assert.equal(result.settled, 1);
  assert.equal(f.reads.some(u => u.includes("jules.googleapis")), false);
  assert.deepEqual(f.posts, []);
});
test("changed base is reconsidered despite an old certified label", async () => {
  const f = fixture({ verification: "certified", base_sha: "c".repeat(40) });
  f.pr.labels.push({ name: "jules:certified" });
  const result = await sweep(f.env, false, { fetcher: f.fetcher });
  assert.equal(result.dispatched, 1);
});
test("reservation prevents duplicate sequential hints", async () => {
  const f = fixture();
  await sweep(f.env, false, { fetcher: f.fetcher });
  const second = await sweep(f.env, false, { fetcher: f.fetcher });
  assert.equal(second.reserved, 1); assert.equal(f.posts.length, 1);
});
test("uncertain POST remains reserved", async () => {
  const f = fixture();
  const fetcher = async (u, i) => u.endsWith("/dispatches") ? Promise.reject(new Error("response lost")) : f.fetcher(u, i);
  assert.equal((await sweep(f.env, false, { fetcher })).errors.length, 1);
  const second = await sweep(f.env, false, { fetcher: f.fetcher });
  assert.equal(second.reserved, 1); assert.equal(f.posts.length, 0);
});
test("multi-account sweep authenticates and dispatches for both Jules A and Jules B", async () => {
  const reads = [], posts = [];
  const storage = new Map();
  const env = {
    GITHUB_REPO: "a/b",
    GITHUB_TOKEN: "ghp_testvalue123",
    JULES_A_API_KEY: "key-a",
    JULES_B_API_KEY: "key-b",
    SESS: { async get(k) { return storage.get(k); }, async put(k, v) { storage.set(k, JSON.parse(v)); } },
  };
  const prA = { number: 7, labels: [{ name: "jules:run" }], head: { sha: "a".repeat(40), repo: { full_name: "a/b" } }, base: { sha: "b".repeat(40) } };
  const prB = { number: 8, labels: [{ name: "jules:run" }], head: { sha: "c".repeat(40), repo: { full_name: "a/b" } }, base: { sha: "d".repeat(40) } };

  // Create comments signed with key-a and key-b respectively
  const commentA = (function() {
    const state = { owner: "a", session: "sessions/1", comment_id: 10, number: 7, repository: "a/b", head_sha: "a".repeat(40), base_sha: "b".repeat(40) };
    const payload = Buffer.from(JSON.stringify(state));
    const k = createHmac("sha256", "key-a").update("github-agent-router/state/v1").digest();
    const sig = createHmac("sha256", k).update(payload).digest("hex");
    return { id: 10, body: `<!-- github-agent-router:envelope:${JSON.stringify({ payload: payload.toString("base64"), signature: sig })} -->` };
  })();
  const commentB = (function() {
    const state = { owner: "b", session: "sessions/2", comment_id: 20, number: 8, repository: "a/b", head_sha: "c".repeat(40), base_sha: "d".repeat(40) };
    const payload = Buffer.from(JSON.stringify(state));
    const k = createHmac("sha256", "key-b").update("github-agent-router/state/v1").digest();
    const sig = createHmac("sha256", k).update(payload).digest("hex");
    return { id: 20, body: `<!-- github-agent-router:envelope:${JSON.stringify({ payload: payload.toString("base64"), signature: sig })} -->` };
  })();

  const fetcher = async (url, init) => {
    reads.push(url);
    if (url.includes("/pulls?")) return Response.json([prA, prB]);
    if (url.includes("/issues/7/comments")) return Response.json([commentA]);
    if (url.includes("/issues/8/comments")) return Response.json([commentB]);
    if (url.includes("jules.googleapis.com/v1alpha/sessions/1")) {
      assert.equal(init.headers["x-goog-api-key"], "key-a");
      return Response.json({ state: "COMPLETED" });
    }
    if (url.includes("jules.googleapis.com/v1alpha/sessions/2")) {
      assert.equal(init.headers["x-goog-api-key"], "key-b");
      return Response.json({ state: "COMPLETED" });
    }
    if (url.endsWith("/dispatches")) {
      posts.push(JSON.parse(init.body));
      return new Response(null, { status: 204 });
    }
    throw new Error("unexpected URL: " + url);
  };

  const result = await sweep(env, false, { fetcher });
  assert.equal(result.dispatched, 2);
  assert.equal(posts.length, 2);
  assert.equal(posts[0].client_payload.pr_number, 7);
  assert.equal(posts[1].client_payload.pr_number, 8);
});
test("allow list and disallow list filter repositories with granular choices", async () => {
  const checked = [];
  const env = {
    ALLOW_REPOS: "org/repo1, org/repo2, org/repo3",
    DISALLOW_REPOS: "org/repo2",
    GITHUB_TOKEN: "ghp_testvalue123",
    JULES_API_KEY: "key",
    SESS: { async get() { return null; }, async put() {} },
  };
  const fetcher = async (url) => {
    if (url.includes("/pulls?")) {
      checked.push(url);
      return Response.json([]);
    }
    throw new Error("unexpected URL: " + url);
  };
  const result = await sweep(env, true, { fetcher });
  assert.equal(result.repos_checked, 2);
  assert.ok(checked.some(u => u.includes("org/repo1")));
  assert.ok(checked.some(u => u.includes("org/repo3")));
  assert.ok(!checked.some(u => u.includes("org/repo2")));
});
test("wildcard allow list auto-discovers repos and applies disallow exclusion", async () => {
  const checked = [];
  const env = {
    ALLOW_REPOS: "*",
    DISALLOW_REPOS: "user/excluded",
    GITHUB_TOKEN: "ghp_testvalue123",
    JULES_API_KEY: "key",
    SESS: { async get() { return null; }, async put() {} },
  };
  const fetcher = async (url) => {
    if (url.includes("/user/repos?")) {
      return Response.json([
        { full_name: "user/active1", archived: false, disabled: false },
        { full_name: "user/excluded", archived: false, disabled: false },
        { full_name: "user/archived", archived: true, disabled: false },
        { full_name: "user/active2", archived: false, disabled: false },
      ]);
    }
    if (url.includes("/pulls?")) {
      checked.push(url);
      return Response.json([]);
    }
    throw new Error("unexpected URL: " + url);
  };
  const result = await sweep(env, true, { fetcher });
  assert.equal(result.repos_checked, 2);
  assert.ok(checked.some(u => u.includes("user/active1")));
  assert.ok(checked.some(u => u.includes("user/active2")));
  assert.ok(!checked.some(u => u.includes("user/excluded")));
  assert.ok(!checked.some(u => u.includes("user/archived")));
});

// ---- Failure watch tests ----
import { failureWatch } from "./worker.js";

function fwStorage() {
  const m = new Map();
  return {
    async get(k) { const v = m.get(k); return v === undefined ? null : v; },
    async put(k, v) { m.set(k, JSON.parse(v)); },
    async del(k) { m.delete(k); },
    size() { return m.size; },
    async list(o) {
      const ks = [];
      for (const k of m.keys()) if (k.startsWith(o.prefix)) ks.push({ name: k });
      return { keys: ks, list_complete: true };
    },
  };
}

function fwFixture() {
  const store = fwStorage();
  const posts = [];
  const runsDb = [];
  const jobsDb = {};
  let seq = 5000;
  const now = Date.now();
  const mkJob = (minutes, conclusion) => [{
    name: "build",
    conclusion,
    started_at: new Date(now - minutes * 60000).toISOString(),
    completed_at: new Date(now).toISOString(),
  }];
  const mkRun = (over, minutes) => {
    const id = ++seq;
    const run = {
      id, name: "CI", head_branch: "feat/x", event: "pull_request",
      status: "completed", conclusion: "failure",
      html_url: "https://example.test/runs/" + id,
      head_commit: { id: "a1b2c3", message: "test commit" },
    };
    Object.assign(run, over);
    runsDb.push(run);
    const jc = run.conclusion === "success" ? "success" : "failure";
    jobsDb[id] = mkJob(minutes, jc);
    return id;
  };
  const env = {
    ALLOW_REPOS: "a/b",
    GITHUB_TOKEN: "placeholder",
    ALERT_EMAIL: "alerts@example.test",
    RESEND_API_KEY: "placeholder",
    SESS: {
      get: (k) => store.get(k),
      put: (k, v) => store.put(k, v),
      delete: (k) => store.del(k),
      list: (o) => store.list(o),
    },
  };
  const fetcher = async (url, init) => {
    const u = String(url);
    if (u.indexOf("api.resend.com/emails") !== -1) {
      posts.push(JSON.parse(init.body));
      return new Response(null, { status: 200 });
    }
    if (u.indexOf("/actions/runs?") !== -1) {
      const sorted = [...runsDb].sort((a, b) => b.id - a.id);
      return Response.json(sorted);
    }
    const jm = u.match(/\/actions\/runs\/(\d+)\/jobs/);
    if (jm) return Response.json({ jobs: jobsDb[jm[1]] || [] });
    throw new Error("unexpected URL: " + u);
  };
  return { env, fetcher, posts, store, mkRun };
}

test("fresh deploy watermarks without alerting on history", async () => {
  const f = fwFixture();
  f.mkRun({}, 5); f.mkRun({}, 5); f.mkRun({}, 5); f.mkRun({}, 5);
  const r = await failureWatch(f.env, { fetcher: f.fetcher });
  assert.equal(r.alerts, 0);
  assert.deepEqual(f.posts, []);
});

test("three consecutive code failures alert exactly once", async () => {
  const f = fwFixture();
  await failureWatch(f.env, { fetcher: f.fetcher });
  f.mkRun({}, 5);
  await failureWatch(f.env, { fetcher: f.fetcher });
  assert.equal(f.posts.length, 0);
  f.mkRun({}, 5); f.mkRun({}, 5);
  await failureWatch(f.env, { fetcher: f.fetcher });
  assert.equal(f.posts.length, 1);
  assert.match(f.posts[0].subject, /failed 3x in a row/);
  f.mkRun({}, 5);
  await failureWatch(f.env, { fetcher: f.fetcher });
  assert.equal(f.posts.length, 1);
});

test("a success resets the failure streak", async () => {
  const f = fwFixture();
  await failureWatch(f.env, { fetcher: f.fetcher });
  f.mkRun({}, 5); f.mkRun({}, 5);
  await failureWatch(f.env, { fetcher: f.fetcher });
  f.mkRun({ conclusion: "success" }, 5);
  await failureWatch(f.env, { fetcher: f.fetcher });
  f.mkRun({}, 5);
  await failureWatch(f.env, { fetcher: f.fetcher });
  assert.equal(f.posts.length, 0);
});

test("code failure on a main push alerts immediately", async () => {
  const f = fwFixture();
  await failureWatch(f.env, { fetcher: f.fetcher });
  f.mkRun({ head_branch: "main", event: "push" }, 8);
  const r = await failureWatch(f.env, { fetcher: f.fetcher });
  assert.equal(r.alerts, 1);
  assert.match(f.posts[0].subject, /FAILED on main push/);
});

test("ten sub-minute deaths alert once with the infra hint", async () => {
  const f = fwFixture();
  await failureWatch(f.env, { fetcher: f.fetcher });
  for (let i = 0; i < 9; i++) f.mkRun({ name: "Lint", head_branch: "feat/y" }, 0.2);
  await failureWatch(f.env, { fetcher: f.fetcher });
  assert.equal(f.posts.length, 0);
  f.mkRun({ name: "Lint", head_branch: "feat/y" }, 0.2);
  await failureWatch(f.env, { fetcher: f.fetcher });
  assert.equal(f.posts.length, 1);
  assert.match(f.posts[0].subject, /dying in <60s/);
  assert.match(f.posts[0].text, /quota/);
});

test("cancelled runs are ignored", async () => {
  const f = fwFixture();
  await failureWatch(f.env, { fetcher: f.fetcher });
  f.mkRun({ conclusion: "cancelled" }, 0);
  const r = await failureWatch(f.env, { fetcher: f.fetcher });
  assert.equal(r.alerts, 0);
  assert.deepEqual(f.posts, []);
});

test("dry run sends nothing and writes nothing", async () => {
  const f = fwFixture();
  await failureWatch(f.env, { fetcher: f.fetcher });
  f.mkRun({}, 5); f.mkRun({}, 5); f.mkRun({}, 5);
  const before = f.store.size();
  const r = await failureWatch(f.env, { fetcher: f.fetcher, dry: true });
  assert.equal(r.alerts, 1);
  assert.deepEqual(f.posts, []);
  assert.equal(f.store.size(), before);
});


// ---- Digest tests ----
import { sendDigest } from "./worker.js";

function dgStore() {
  const m = new Map();
  return {
    get: async (k) => { const v = m.get(k); return v === undefined ? null : v; },
    put: async (k, v) => { m.set(k, JSON.parse(v)); },
    del: async (k) => { m.delete(k); },
    size: () => m.size,
    list: async (o) => {
      const ks = [];
      for (const k of m.keys()) if (k.startsWith(o.prefix)) ks.push({ name: k });
      return { keys: ks, list_complete: true };
    },
  };
}

function dgFixture() {
  const store = dgStore();
  const posts = [];
  const runsDb = [];
  let seq = 9000;
  const now = Date.now();
  const mkRun = (over, durMin) => {
    const id = ++seq;
    const start = new Date(now - durMin * 60000).toISOString();
    const run = {
      id: id,
      name: "CI",
      head_branch: "feat/x",
      event: "pull_request",
      status: "completed",
      conclusion: "success",
      html_url: "https://example.test/runs/" + id,
      head_commit: { id: "a1b2c3", message: "test commit" },
      run_started_at: start,
      updated_at: new Date(now).toISOString(),
      created_at: start,
    };
    for (const k of Object.keys(over)) run[k] = over[k];
    runsDb.push(run);
    return id;
  };
  const env = {
    ALLOW_REPOS: "a/b",
    GITHUB_TOKEN: "placeholder",
    ALERT_EMAIL: "alerts@example.test",
    RESEND_API_KEY: "placeholder",
    SESS: {
      get: (k) => store.get(k),
      put: (k, v) => store.put(k, v),
      delete: (k) => store.del(k),
      list: (o) => store.list(o),
    },
  };
  const fetcher = async (url, init) => {
    const u = String(url);
    if (u.indexOf("api.resend.com/emails") !== -1) {
      posts.push(JSON.parse(init.body));
      return new Response(null, { status: 200 });
    }
    if (u.indexOf("/actions/runs?") !== -1) {
      const sorted = [...runsDb].sort((a, b) => b.id - a.id);
      return Response.json(sorted);
    }
    throw new Error("unexpected URL: " + u);
  };
  return { env: env, fetcher: fetcher, posts: posts, store: store, mkRun: mkRun };
}

test("digest summarizes runs and flags sub-minute failures", async () => {
  const f = dgFixture();
  f.mkRun({ conclusion: "success" }, 5);
  f.mkRun({ conclusion: "success" }, 4);
  f.mkRun({ conclusion: "failure", name: "CI" }, 6);
  f.mkRun({ conclusion: "failure", name: "Lint" }, 0.3);
  f.mkRun({ conclusion: "failure", name: "Lint" }, 0.2);
  const r = await sendDigest(f.env, { fetcher: f.fetcher });
  assert.equal(r.runs, 5);
  assert.equal(r.failed, 3);
  assert.equal(f.posts.length, 1);
  assert.match(f.posts[0].subject, /\[CI DIGEST\]/);
  assert.match(f.posts[0].subject, /5 runs, 3 failed/);
  assert.match(f.posts[0].text, /Lint: 2\/2 failed \(all <1 min/);
  assert.match(f.posts[0].text, /CI: 1\/3 failed/);
});

test("digest lists open streaks and recent alerts", async () => {
  const f = dgFixture();
  f.mkRun({ conclusion: "failure" }, 6);
  const st = {
    code: 3, infra: 0, alerted_code: true, alerted_infra: false,
    alerted_code_at: Date.now(), seen: Date.now(),
  };
  await f.env.SESS.put("fail:a/b#CI#feat/x", JSON.stringify(st));
  const r = await sendDigest(f.env, { fetcher: f.fetcher });
  assert.equal(r.alerts, 1);
  assert.match(f.posts[0].subject, /1 alert sent/);
  assert.match(f.posts[0].text, /streak CI on feat\/x: 3x code \(alerted\)/);
});

test("digest with no runs still sends a heartbeat", async () => {
  const f = dgFixture();
  const r = await sendDigest(f.env, { fetcher: f.fetcher });
  assert.equal(r.runs, 0);
  assert.equal(f.posts.length, 1);
  assert.match(f.posts[0].text, /no completed runs/);
});

test("digest dry run sends nothing", async () => {
  const f = dgFixture();
  f.mkRun({ conclusion: "failure" }, 6);
  const r = await sendDigest(f.env, { fetcher: f.fetcher, dry: true });
  assert.equal(r.dry_run, true);
  assert.deepEqual(f.posts, []);
});

test("scheduled handler routes the daily cron to the digest", async () => {
  const f = dgFixture();
  f.mkRun({ conclusion: "success" }, 2);
  // scheduled() calls sendDigest(env) with no options, so it reads the
  // global fetch; swap in the fixture fetcher for the duration of the test.
  const realFetch = globalThis.fetch;
  globalThis.fetch = f.fetcher;
  try {
    const waited = [];
    const ctx = { waitUntil: (p) => waited.push(p) };
    const mod = await import("./worker.js");
    await mod.default.scheduled({ cron: "0 12 * * *" }, f.env, ctx);
    await Promise.all(waited);
  } finally {
    globalThis.fetch = realFetch;
  }
  assert.equal(f.posts.length, 1);
  assert.match(f.posts[0].subject, /\[CI DIGEST\]/);
});

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
    GITHUB_TOKEN: "token",
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
    GITHUB_TOKEN: "token",
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
    GITHUB_TOKEN: "token",
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


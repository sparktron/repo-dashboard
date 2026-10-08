// Run with: node test_app.js
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const source = fs.readFileSync(path.join(__dirname, "app.js"), "utf8");
const tick = () => new Promise(resolve => setImmediate(resolve));
function repo(name, overrides = {}) {
  return { name, path: "/repos/" + name, branch: "main", upstream: "origin/main",
    has_remote: true, ahead: 1, behind: 0, status: "good", flags: [],
    dirty: {}, ...overrides };
}
function data(repos) {
  return { repos, root: "/repos", non_git: [], summary: { counts: {},
    repo_count: repos.length, non_git_count: 0 } };
}
async function page(repos, { live = true, confirm = true, post, getRepos } = {}) {
  const elements = new Map();
  const documentEvents = {};
  const requests = [];
  const repoFetches = [];
  const confirmations = [];
  function element(id) {
    if (!elements.has(id)) elements.set(id, {
      value: "0", innerHTML: "", textContent: "", attributes: {}, events: {},
      addEventListener(type, cb) { this.events[type] = cb; },
      setAttribute(key, value) { this.attributes[key] = value; },
      getAttribute(key) { return this.attributes[key] || null; },
      removeAttribute(key) { delete this.attributes[key]; }
    });
    return elements.get(id);
  }
  const payload = data(repos);
  vm.runInNewContext(source, {
    BOOTSTRAP: live ? null : payload,
    document: { getElementById: element, documentElement: element("html"),
      addEventListener(type, cb) { documentEvents[type] = cb; } },
    window: { scrollY: 0, scrollTo() {}, matchMedia() { return { matches: false }; },
      confirm(message) { confirmations.push(message); return confirm; } },
    localStorage: { removeItem() {}, setItem() {} },
    setInterval() { return 1; }, clearInterval() {},
    fetch(url, options) {
      if (url === "/api/repos") {
        repoFetches.push(url);
        const d = getRepos ? getRepos(repoFetches.length, payload) : Promise.resolve(payload);
        return d.then(x => ({ ok: true, json: async () => x }));
      }
      const body = JSON.parse(options.body);
      requests.push(body);
      return post ? post(body, requests.length) : Promise.resolve({
        text: async () => JSON.stringify({ ok: true, output: "done" })
      });
    }
  });
  await tick();
  return { element, requests, confirmations, repoFetches,
    click: id => element(id).events.click(),
    rowClick(repoPath, action) {
      const button = { getAttribute: key => key === "data-path" ? repoPath : action };
      documentEvents.click({ target: { closest: selector => selector === ".gitbtn" ? button : null } });
    }
  };
}

test("Pull all includes filtered repos, skips blocked repos and continues after failures", async () => {
  const p = await page([repo("one"), repo("two"), repo("three"),
    repo("detached", { detached: true }), repo("local", { upstream: null, has_remote: false })], {
    post: async (body, n) => {
      if (n === 2) throw new Error("network unavailable");
      return { text: async () => JSON.stringify({ ok: true, output: "done" }) };
    }
  });
  p.element("q").value = "one";
  p.element("q").events.input.call(p.element("q"));
  await p.click("pullAll");
  assert.deepEqual(p.requests.map(r => r.path), ["/repos/one", "/repos/two", "/repos/three"]);
  assert.ok(p.requests.every(r => r.action === "pull" && r.branch === "main"));
  assert.equal(p.confirmations.length, 0);
  assert.match(p.element("bulkStatus").textContent, /2 succeeded, 1 failed, 2 skipped \(5 repos\)/);
  assert.match(p.element("bulkIssues").innerHTML, /network unavailable/);
  assert.match(p.element("bulkIssues").innerHTML, /detached HEAD/);
  assert.match(p.element("bulkIssues").innerHTML, /no upstream/);
  assert.equal(p.element("pullAll").disabled, false);
});

test("Push all confirms once with every target and skips repos with nothing to push", async () => {
  const p = await page([repo("one"), repo("new", { upstream: null }), repo("synced", { ahead: 0 })]);
  await p.click("pushAll");
  assert.equal(p.confirmations.length, 1);
  assert.match(p.confirmations[0], /push 2 repos; skip 1/);
  assert.match(p.confirmations[0], /new \(\/repos\/new\): main → origin\/main/);
  assert.deepEqual(p.requests.map(r => r.path), ["/repos/one", "/repos/new"]);
  assert.ok(p.requests.every(r => r.action === "push"));
  assert.match(p.element("bulkStatus").textContent, /2 succeeded, 0 failed, 1 skipped/);
});

test("canceling Push all sends no Git requests", async () => {
  const p = await page([repo("one")], { confirm: false });
  await p.click("pushAll");
  assert.equal(p.requests.length, 0);
  assert.equal(p.element("pushAll").disabled, false);
});

test("bulk requests are serial, lock other actions and preserve the confirmed branches", async () => {
  let resolveFirst;
  const p = await page([repo("one"), repo("two")], {
    post: (body, n) => n === 1 ? new Promise(resolve => { resolveFirst = resolve; }) :
      Promise.resolve({ text: async () => JSON.stringify({ ok: true, output: "done" }) })
  });
  const running = p.click("pushAll");
  assert.equal(p.requests.length, 1);
  assert.equal(p.element("pullAll").disabled, true);
  assert.equal(p.element("pushAll").disabled, true);
  await p.click("pullAll");
  p.rowClick("/repos/two", "pull");
  assert.equal(p.requests.length, 1);
  resolveFirst({ text: async () => JSON.stringify({ ok: true, output: "done",
    data: data([repo("one"), repo("two", { branch: "changed" })]) }) });
  await running;
  assert.equal(p.requests.length, 2);
  assert.equal(p.requests[1].branch, "main");
  assert.equal(p.confirmations.length, 1);
  assert.equal(p.element("pushAll").disabled, false);
});

test("server refusals and plain-text errors are shown without stopping the batch", async () => {
  const p = await page([repo("one"), repo("two"), repo("three")], {
    post: async (body, n) => n === 1
      ? { text: async () => JSON.stringify({ ok: false, output: "branch changed" }) }
      : n === 2 ? { status: 403, text: async () => "forbidden" }
        : { text: async () => JSON.stringify({ ok: true, output: "done" }) }
  });
  await p.click("pullAll");
  assert.equal(p.requests.length, 3);
  assert.match(p.element("bulkStatus").textContent, /1 succeeded, 2 failed, 0 skipped/);
  assert.match(p.element("bulkIssues").innerHTML, /branch changed/);
  assert.match(p.element("bulkIssues").innerHTML, /403: forbidden/);
});

test("an individual action locks bulk controls until it completes", async () => {
  let finish;
  const p = await page([repo("one")], {
    post: () => new Promise(resolve => { finish = resolve; })
  });
  p.rowClick("/repos/one", "pull");
  assert.equal(p.element("pushAll").disabled, true);
  await p.click("pushAll");
  assert.equal(p.requests.length, 1);
  finish({ text: async () => JSON.stringify({ ok: true, output: "done" }) });
  await tick();
  assert.equal(p.element("pushAll").disabled, false);
});

test("static snapshots and empty dashboards cannot run bulk actions", async () => {
  for (const p of [await page([repo("one")], { live: false }), await page([])]) {
    assert.equal(p.element("pullAll").disabled, true);
    assert.equal(p.element("pushAll").disabled, true);
    await p.click("pullAll");
    await p.click("pushAll");
    assert.equal(p.requests.length, 0);
  }
});

test("every request carries the upstream the user confirmed", async () => {
  const p = await page([repo("one"), repo("new", { upstream: null })]);
  await p.click("pushAll");
  assert.deepEqual(p.requests.map(r => r.upstream), ["origin/main", null]);
});

test("a batch cannot start mid-refresh, and refreshes wait out a batch", async () => {
  let finishRefresh, finishPull;
  const p = await page([repo("one")], {
    getRepos: (n, payload) => n === 2
      ? new Promise(resolve => { finishRefresh = () => resolve(payload); })
      : Promise.resolve(payload),
    post: () => new Promise(resolve => { finishPull = resolve; })
  });
  p.click("refresh");
  assert.equal(p.element("pullAll").disabled, true);
  await p.click("pullAll");
  assert.equal(p.requests.length, 0);
  finishRefresh();
  await tick(); await tick();
  assert.equal(p.element("pullAll").disabled, false);

  const running = p.click("pullAll");
  assert.equal(p.requests.length, 1);
  p.click("refresh");
  assert.equal(p.repoFetches.length, 2);
  finishPull({ text: async () => JSON.stringify({ ok: true, output: "done" }) });
  await running;
});

test("a slow refresh cannot overwrite the scan a later pull returned", async () => {
  let finishRefresh;
  const p = await page([repo("one")], {
    getRepos: (n, payload) => n === 2
      ? new Promise(resolve => { finishRefresh = () => resolve(payload); })
      : Promise.resolve(payload),
    post: async () => ({ text: async () => JSON.stringify({ ok: true, output: "done",
      data: data([repo("one", { branch: "after-pull" })]) }) })
  });
  p.click("refresh");                      // starts first, will finish last
  p.rowClick("/repos/one", "pull");
  for (let i = 0; i < 5; i++) await tick();
  assert.match(p.element("rows").innerHTML, /after-pull/);
  finishRefresh();
  for (let i = 0; i < 5; i++) await tick();
  assert.match(p.element("rows").innerHTML, /after-pull/);
});

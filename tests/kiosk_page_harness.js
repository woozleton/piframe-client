// Runs the kiosk page's script in Node with a fake DOM and fake timers,
// then a scenario against it. Used by tests/test_kiosk_page.py:
//   node kiosk_page_harness.js <page-script.js> <scenario.js>
// The scenario runs in the page's own global scope (it sees stages,
// bannerEl, activeIndex, pollState ...) plus the helpers below, and
// prints its findings with report(...).
"use strict";
const fs = require("fs");
const vm = require("vm");

const [pageFile, scenarioFile] = process.argv.slice(2);

class FakeClassList {
  constructor() { this.set = new Set(); }
  add(...names) { names.forEach((n) => this.set.add(n)); }
  remove(...names) { names.forEach((n) => this.set.delete(n)); }
  contains(name) { return this.set.has(name); }
  toggle(name, force) {
    const on = force === undefined ? !this.set.has(name) : !!force;
    if (on) this.set.add(name); else this.set.delete(name);
    return on;
  }
}

class FakeEl {
  constructor(id) {
    this.id = id || "";
    this.classList = new FakeClassList();
    this.style = { cssText: "" };
    this.dataset = {};
    this.textContent = "";
    this.innerHTML = "";
    this.parentNode = { replaceChild() {} };
    // media
    this._src = "";
    this.srcSets = 0;
    this.loadCalls = 0;
    this.paused = true;
    this.currentTime = 0;
    this.readyState = 0;
    this.loop = false;
    this.muted = false;
    this.volume = 1;
    this.videoWidth = 1920;
    this.videoHeight = 1080;
    // image
    this.complete = false;
    this.naturalWidth = 0;
    this.naturalHeight = 0;
    this.decodeOk = null;
  }
  get src() { return this._src; }
  set src(value) { this._src = value; this.srcSets += 1; this.complete = false; this.naturalWidth = 0; }
  get currentSrc() { return this._src; }
  removeAttribute(name) { if (name === "src") this._src = ""; }
  setAttribute() {}
  getAttribute(name) { return name === "src" ? this._src : null; }
  addEventListener() {}
  removeEventListener() {}
  querySelector() { return new FakeEl(); }
  appendChild() {}
  getContext() { return null; }
  load() { this.loadCalls += 1; }
  play() { this.paused = false; return Promise.resolve(); }
  pause() { this.paused = true; }
  // Settles only once the test says how the image went (loadImage).
  decode() { return new Promise((resolve, reject) => { this._decode = { resolve, reject }; }); }
}

const elements = new Map();
const document = {
  documentElement: new FakeEl("html"),
  body: new FakeEl("body"),
  getElementById(id) {
    if (!elements.has(id)) elements.set(id, new FakeEl(id));
    return elements.get(id);
  },
  createElement() { return new FakeEl(); },
};

// Fake timers: nothing runs until advance(ms).
let now = 0;
let nextTimerId = 1;
const timers = new Map();
function addTimer(fn, ms, repeat) {
  const id = nextTimerId++;
  timers.set(id, { fn, at: now + Math.max(0, Number(ms) || 0), every: repeat ? Math.max(1, Number(ms) || 0) : 0 });
  return id;
}

let state = null;
const events = [];

const sandbox = {
  console,
  document,
  navigator: { userAgent: "node" },
  innerWidth: 1920,
  innerHeight: 1080,
  getComputedStyle: () => ({ getPropertyValue: () => "" }),
  setTimeout: (fn, ms) => addTimer(fn, ms, false),
  setInterval: (fn, ms) => addTimer(fn, ms, true),
  clearTimeout: (id) => timers.delete(id),
  clearInterval: (id) => timers.delete(id),
  requestAnimationFrame: () => 0,
  cancelAnimationFrame: () => {},
  addEventListener() {},
  Image: FakeEl,
  fetch: async (url, opts) => {
    if (opts && opts.method === "POST") {
      events.push(JSON.parse(opts.body));
      return { json: async () => ({}) };
    }
    return { json: async () => JSON.parse(JSON.stringify(state)) };
  },
};
sandbox.window = sandbox;
vm.createContext(sandbox);

async function flush() {
  for (let i = 0; i < 20; i++) await new Promise((r) => setImmediate(r));
}

Object.assign(sandbox, {
  events,
  report: (value) => process.stdout.write(JSON.stringify(value) + "\n"),
  flush,
  // Write a new state file and let the page poll it.
  setState: async (next) => { state = next; await sandbox.pollState(); await flush(); },
  advance: async (ms) => {
    const until = now + ms;
    for (;;) {
      let due = null;
      for (const [id, t] of timers) {
        if (t.at <= until && (!due || t.at < due[1].at)) due = [id, t];
      }
      if (!due) break;
      const [id, t] = due;
      now = t.at;
      if (t.every) t.at += t.every; else timers.delete(id);
      t.fn();
      await flush();
    }
    now = until;
  },
  // Finish an image's load the way Chromium would: onload / onerror, then decode.
  loadImage: async (img, ok) => {
    img.complete = true;
    img.naturalWidth = ok ? 800 : 0;
    img.naturalHeight = ok ? 600 : 0;
    if (ok && img.onload) img.onload();
    if (!ok && img.onerror) img.onerror();
    if (img._decode) { if (ok) img._decode.resolve(); else img._decode.reject(new Error("decode")); }
    await flush();
  },
  // A video's first frame decoding.
  loadVideo: async (video) => {
    video.readyState = 4;
    if (video.onloadeddata) video.onloadeddata();
    await flush();
  },
});

state = { mode: "idle", items: [], idle_item: null, banner: null };
vm.runInContext(fs.readFileSync(pageFile, "utf8"), sandbox, { filename: "page.js" });
const scenario = fs.readFileSync(scenarioFile, "utf8");
vm.runInContext(`(async () => {\n${scenario}\n})()`, sandbox, { filename: "scenario.js" })
  .then(() => flush())
  .catch((err) => { process.stderr.write(String(err && err.stack || err) + "\n"); process.exit(1); });

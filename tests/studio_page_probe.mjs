// Ганяє скрипт сторінки редактора в Node поверх заглушки DOM.
//
// `node --check` ловить лише описки. Арифметика стрічки — збереження довжини
// репліки при перетягуванні, прилипання до сусідів, кроки лінійки, склеювання
// кроків історії — не видно ні звідти, ні з тестів на Python: вона живе цілком
// усередині рядка зі сторінкою. Саме там і ховались баги, тож перевіряємо її
// так само, як браузер: виконуємо той самий скрипт і дивимось на результат.
//
// Заглушка навмисно дурна: вона не вдає браузер, а лише дозволяє скрипту
// доїхати до кінця, не впавши. Щойно сторінці знадобиться щось нове з DOM,
// стенд це помітить падінням, а не мовчазним пропуском перевірок.
//
// Запуск: node tests/studio_page_probe.mjs <шлях до вийнятого page.js>

import { readFileSync } from "node:fs";
import vm from "node:vm";

const script = readFileSync(process.argv[2], "utf-8");

function element(tag = "div") {
  const el = {
    tagName: tag.toUpperCase(),
    children: [],
    dataset: {},
    style: {},
    value: "",
    textContent: "",
    scrollLeft: 0,
    clientWidth: 900,
    scrollHeight: 20,
    rows: 1,
    checked: tag === "follow",
    classList: {
      _set: new Set(),
      add(...n) { n.forEach((x) => this._set.add(x)); },
      remove(...n) { n.forEach((x) => this._set.delete(x)); },
      toggle(n, on) { if (on) this._set.add(n); else this._set.delete(n); },
      contains(n) { return this._set.has(n); },
    },
    append(...kids) { this.children.push(...kids); },
    prepend(...kids) { this.children.unshift(...kids); },
    replaceChildren(...kids) { this.children = kids; },
    remove() {},
    setAttribute() {},
    getAttribute() { return null; },
    addEventListener() {},
    removeEventListener() {},
    setPointerCapture() {},
    releasePointerCapture() {},
    querySelectorAll() { return []; },
    querySelector() { return null; },
    closest() { return null; },
    scrollIntoView() {},
    getBoundingClientRect() { return { left: 0, top: 0, width: 900, height: 100 }; },
    focus() {},
    blur() {},
  };
  return el;
}

const nodes = new Map();
const byId = (id) => {
  if (!nodes.has(id)) {
    const el = element(id === "video" ? "video" : "div");
    if (id === "follow" || id === "guides") el.checked = id === "follow";
    if (id === "video") {
      el.currentTime = 0;
      el.duration = 60;
      el.paused = true;
      el.play = () => {};
      el.pause = () => {};
    }
    nodes.set(id, el);
  }
  return nodes.get(id);
};

const cuesFixture = [
  { index: 1, start: 0, end: 2, lines: ["one"], chars: 3, cps: 1.5, level: "ok", problems: [] },
  { index: 2, start: 3, end: 5, lines: ["two"], chars: 3, cps: 1.5, level: "ok", problems: [] },
  { index: 3, start: 6, end: 9, lines: ["three"], chars: 5, cps: 1.7, level: "ok", problems: [] },
];

const sandbox = {
  console,
  setTimeout, clearTimeout, setInterval, clearInterval,
  requestAnimationFrame: () => 0,
  Date, Math, JSON, Number, String, Object, Array, Error, isFinite, parseInt, parseFloat, Map, Set,
  document: {
    documentElement: { dataset: {} },
    activeElement: { tagName: "BODY" },
    getElementById: byId,
    createElement: element,
    createTextNode: (t) => ({ textContent: t }),
    addEventListener() {},
    querySelector: () => null,
    querySelectorAll: () => [],
  },
  localStorage: { getItem: () => null, setItem() {} },
  fetch: async () => ({
    ok: true,
    status: 200,
    json: async () => ({
      cues: cuesFixture,
      errors: 0,
      warnings: 0,
      limits: { max_line_chars: 42, max_lines: 2, max_cps: 17, min_duration: 0.83 },
      stamp: "v1",
    }),
  }),
};
sandbox.window = sandbox;
sandbox.globalThis = sandbox;
sandbox.window.addEventListener = () => {};

const probe = `
globalThis.__probe = {
  get cues() { return cues; }, set cues(v) { cues = v; },
  get pps() { return pps; }, set pps(v) { pps = v; },
  get past() { return past; },
  get snapping() { return snapping; }, set snapping(v) { snapping = v; },
  get marks() { return marks; }, set marks(v) { marks = v; },
  propose, ruler, snapTo, remember, retime, clockText, parseClock, duration, stepFor,
};
`;

vm.createContext(sandbox);
vm.runInContext(script + probe, sandbox);

const P = sandbox.__probe;
let failures = 0;
const check = (name, ok, detail) => {
  if (ok) { console.log("  ok   " + name); return; }
  failures += 1;
  console.log("  FAIL " + name + (detail === undefined ? "" : "  -> " + detail));
};

// The page boots asynchronously; give the fetch a turn before poking at state.
await new Promise((r) => setTimeout(r, 20));

console.log("cues loaded from /cues.json:", P.cues.length);

// --- 1. dragging a cue by its body must keep its length ---------------------
console.log("\n1. drag a cue right, hard into its neighbour");
P.cues = [
  { start: 0, end: 2, text: "one" },
  { start: 3, end: 5, text: "two" },
  { start: 6, end: 9, text: "three" },
];
P.snapping = false;
{
  const base = { start: 0, end: 2 };
  const out = P.propose(0, "move", base, 999);
  const width = out.end - out.start;
  check("length preserved", Math.abs(width - 2) < 1e-9, "width " + width.toFixed(3));
  check("stops at the neighbour", Math.abs(out.end - 3) < 1e-9, "end " + out.end);
}

// --- 2. resizing still clamps to the neighbour ------------------------------
console.log("\n2. drag the right edge into the neighbour");
{
  const out = P.propose(0, "r", { start: 0, end: 2 }, 999);
  check("edge stops at neighbour start", Math.abs(out.end - 3) < 1e-9, "end " + out.end);
  check("start untouched", out.start === 0);
}

// --- 3. snapping ------------------------------------------------------------
console.log("\n3. snapping to neighbour edges");
P.snapping = true;
P.pps = 100; // 8px threshold == 0.08s
{
  const near = P.propose(2, "l", { start: 6, end: 9 }, -0.94); // aims at 5.06, near 5
  check("snaps onto the neighbour end", Math.abs(near.start - 5) < 1e-9, "start " + near.start);
  check("reports the snap point", near.at === 5, "at " + near.at);

  const far = P.propose(2, "l", { start: 6, end: 9 }, -0.5); // aims at 5.5, too far
  check("leaves a far edge alone", Math.abs(far.start - 5.5) < 1e-9, "start " + far.start);
  check("no snap reported", far.at === null, "at " + far.at);

  P.snapping = false;
  const off = P.propose(2, "l", { start: 6, end: 9 }, -0.94);
  check("snapping off means no snap", Math.abs(off.start - 5.06) < 1e-9, "start " + off.start);
  P.snapping = true;
}

// --- 4. ruler: labels rarer than ticks, ticks divide labels -----------------
console.log("\n4. ruler steps across zoom levels");
for (const pps of [0.5, 2, 12, 60, 200, 400]) {
  P.pps = pps;
  const r = P.ruler();
  const ratio = r.label / r.tick;
  check(
    "pps " + pps + ": label " + r.label + "s / tick " + r.tick + "s",
    r.label >= r.tick && Math.abs(ratio - Math.round(ratio)) < 1e-6 && r.label * pps >= 92,
    "ratio " + ratio.toFixed(3) + ", label px " + (r.label * pps).toFixed(1),
  );
  check(
    "pps " + pps + ": ticks subdivide the labels",
    r.tick < r.label,
    "label and tick are both " + r.label + "s, so the scale has no subdivisions",
  );
}

// --- 5. undo coalescing -----------------------------------------------------
console.log("\n5. undo history while typing");
P.pps = 12;
P.cues = [{ start: 0, end: 2, text: "" }];
const before = P.past.length;
for (const text of ["П", "Пр", "При", "Прив"]) {
  P.remember("text:0");
  P.cues[0].text = text;
}
const typed = P.past.length - before;
check("a typing run is one undo step", typed === 1, typed + " steps");

P.remember("nudge:0start");
const afterOther = P.past.length - before;
check("a different field starts a new step", afterOther === 2, afterOther + " steps");

// --- 6. timecode round-trip -------------------------------------------------
console.log("\n6. timecodes");
check("1:05.50 parses", Math.abs(P.parseClock("1:05.50") - 65.5) < 1e-9);
check("comma decimal parses", Math.abs(P.parseClock("0:01,25") - 1.25) < 1e-9);
check("infinity is refused", P.parseClock("1e999") === null);
check("junk is refused", P.parseClock("abc") === null);
check("round trip", P.clockText(65.5) === "1:05.50", P.clockText(65.5));

console.log("\n" + (failures ? failures + " FAILED" : "all checks passed"));
process.exit(failures ? 1 : 0);

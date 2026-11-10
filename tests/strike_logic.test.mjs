import test from "node:test";
import assert from "node:assert/strict";
import {
  gestureMode, fill, applyGesture, isStroke, runs, seed, strokePath, isMuted, nextEdge,
  phraseAt, manualFromHold, nudge, removeManualAt, reviewStops, History, Outbox,
} from "../src/videoworks/web/strike/logic.js";

const S1 = { struck: [1, 2], manual: [] };
const S2 = { struck: [1, 2, 3], manual: [[4, 5]] };
const S3 = { struck: [], manual: [[1, 2]] };

function fakeStorage() {
  const m = new Map();
  return {
    getItem: (k) => (m.has(k) ? m.get(k) : null),
    setItem: (k, v) => m.set(k, String(v)),
    removeItem: (k) => m.delete(k),
  };
}

test("жест, що почався на чистому слові, викреслює; на викресленому — стирає", () => {
  const struck = new Set([2]);
  assert.equal(gestureMode(struck, 1), "strike");
  assert.equal(gestureMode(struck, 2), "erase");
});

test("fill заповнює пропущені слова в обидва боки й минає порожні", () => {
  assert.deepEqual(fill(3, 7, new Set([5])), [4, 6, 7]);
  assert.deepEqual(fill(7, 4, new Set()), [6, 5, 4]);
  assert.deepEqual(fill(null, 4, new Set()), [4]);
  assert.deepEqual(fill(null, 4, new Set([4])), []);
  assert.deepEqual(fill(4, 4, new Set()), []);
});

test("applyGesture не змінює вхідну множину", () => {
  const struck = new Set([1, 2]);
  const added = applyGesture(struck, [3, 4], "strike");
  assert.deepEqual([...added].sort(), [1, 2, 3, 4]);
  const removed = applyGesture(struck, [2, 9], "erase");
  assert.deepEqual([...removed], [1]);
  assert.deepEqual([...struck], [1, 2]);
  assert.notEqual(added, struck);
});

test("рух менше 6 px у межах слова — це клік, а не мазок", () => {
  assert.equal(isStroke(3, 2, false), false);
  assert.equal(isStroke(7, 0, false), true);
  assert.equal(isStroke(1, 0, true), true);
});

test("runs зливає сусідів одного рядка й рве на переносі", () => {
  const line = (i) => (i < 5 ? 0 : 1);
  assert.deepEqual(runs(new Set([2, 3, 4, 5, 6, 9]), line), [[2, 4], [5, 6], [9, 9]]);
  assert.deepEqual(runs(new Set(), line), []);
});

test("seed детермінований і розводить сусідні слова", () => {
  assert.equal(seed(5), seed(5));
  assert.notEqual(seed(5), seed(6));
  assert.ok(Number.isInteger(seed(5)));
});

test("strokePath детермінований і залежить від зерна", () => {
  const a = strokePath(120, 24, 7);
  assert.equal(a, strokePath(120, 24, 7));
  assert.notEqual(a, strokePath(120, 24, 8));
  assert.match(a, /^M/);
  assert.match(a, /Z$/);
});

test("strokePath лишається в межах прямокутника з полем 6 px", () => {
  for (const [w, h] of [[8, 20], [40, 18], [120, 24], [600, 30]]) {
    for (const s of [0, 1, 2, 3, seed(1), seed(2), 4294967295]) {
      const d = strokePath(w, h, s);
      assert.ok(!d.includes("NaN"), d);
      const nums = d.match(/-?\d+(\.\d+)?/g).map(Number);
      for (let i = 0; i < nums.length; i += 2) {
        assert.ok(nums[i] >= -6 && nums[i] <= w + 6, `x=${nums[i]} w=${w}`);
        assert.ok(nums[i + 1] >= -6 && nums[i + 1] <= h + 6, `y=${nums[i + 1]} h=${h}`);
      }
      for (const n of d.match(/-?\d+\.\d+/g) ?? []) {
        assert.ok(n.split(".")[1].length <= 1, n);
      }
    }
  }
});

test("isMuted на межах: початок включно, кінець ні", () => {
  const s = [[1, 2], [5, 6]];
  assert.deepEqual(
    [0.99, 1, 1.5, 2, 5.5, 7].map((t) => isMuted(s, t)),
    [false, true, true, false, true, false],
  );
  assert.equal(isMuted([], 1), false);
});

test("nextEdge дає найближчу межу після t", () => {
  const s = [[1, 2], [5, 6]];
  assert.equal(nextEdge(s, 0), 1);
  assert.equal(nextEdge(s, 1), 2);
  assert.equal(nextEdge(s, 2), 5);
  assert.equal(nextEdge(s, 6), null);
  assert.equal(nextEdge([], 0), null);
});

test("phraseAt: до першої −1, у паузі — попередня", () => {
  const ph = [{ start: 1, end: 2 }, { start: 3, end: 4 }];
  assert.equal(phraseAt(ph, 0.5), -1);
  assert.equal(phraseAt(ph, 1), 0);
  assert.equal(phraseAt(ph, 2.5), 0);
  assert.equal(phraseAt(ph, 3), 1);
  assert.equal(phraseAt(ph, 99), 1);
  assert.equal(phraseAt([], 1), -1);
});

test("ручна позначка відступає на 0.3 с і не йде в мінус", () => {
  assert.deepEqual(manualFromHold(10, 12, 100), [9.7, 12]);
  assert.deepEqual(manualFromHold(0.1, 1, 100), [0, 1]);
  const short = manualFromHold(10, 10.05, 100);
  assert.ok(short[1] - short[0] >= 0.2 - 1e-9);
  assert.deepEqual(manualFromHold(99.9, 100.4, 100), [99.6, 100]);
  assert.equal(manualFromHold(5, 4, 100), null);
  assert.equal(manualFromHold(NaN, 4, 100), null);
});

test("ручна позначка в самому кінці запису не виходить за межі й не коротша за 0.2", () => {
  assert.deepEqual(manualFromHold(99.95, 100, 100), [99.65, 100]);
  const past = manualFromHold(100.5, 101, 100);
  assert.ok(past[1] <= 100 && past[0] >= 0 && past[1] - past[0] >= 0.2 - 1e-9);
});

test("nudge не вивертає відрізок і не виходить за запис", () => {
  assert.deepEqual(nudge([5, 5.25], "start", 0.1, 100), [5.05, 5.25]);
  assert.deepEqual(nudge([5, 5.25], "start", 0.2, 100), [5.05, 5.25]);
  assert.deepEqual(nudge([5, 5.25], "end", -0.1, 100), [5, 5.2]);
  assert.deepEqual(nudge([0.05, 3], "start", -0.1, 100), [0, 3]);
  assert.deepEqual(nudge([5, 99.95], "end", 0.1, 100), [5, 100]);
});

test("removeManualAt прибирає один відрізок і не чіпає вхідний масив", () => {
  const m = [[1, 2], [3, 4], [5, 6]];
  assert.deepEqual(removeManualAt(m, 1), [[1, 2], [5, 6]]);
  assert.equal(m.length, 3);
});

test("reviewStops додає 1.5 с контексту, обрізає й зливає сусідів", () => {
  assert.deepEqual(reviewStops([[10, 11], [12, 13], [30, 31]], 100), [[8.5, 14.5], [28.5, 32.5]]);
  assert.deepEqual(reviewStops([[0.5, 1], [99, 99.5]], 100), [[0, 2.5], [97.5, 100]]);
  assert.deepEqual(reviewStops([], 100), []);
});

test("History: скасування знімає жест цілком, новий жест відтинає повернення", () => {
  const h = new History(S1);
  assert.equal(h.canUndo, false);
  h.push(S2);
  h.push(S3);
  assert.equal(h.canUndo, true);
  assert.deepEqual(h.undo(), S2);
  assert.deepEqual(h.undo(), S1);
  assert.equal(h.undo(), null);
  assert.equal(h.canRedo, true);
  assert.deepEqual(h.redo(), S2);
  h.push({ struck: [9], manual: [] });
  assert.equal(h.canRedo, false);
  assert.equal(h.redo(), null);
  assert.deepEqual(h.undo(), S2);
});

test("History не додає той самий знімок двічі", () => {
  const h = new History(S1);
  h.push({ struck: [1, 2], manual: [] });
  assert.equal(h.canUndo, false);
  h.push(S2);
  h.push({ struck: [1, 2, 3], manual: [[4, 5]] });
  assert.deepEqual(h.undo(), S1);
  assert.equal(h.undo(), null);
});

test("History тримає щонайбільше 200 знімків і губить найстаріші", () => {
  const h = new History({ struck: [0], manual: [] });
  for (let i = 1; i <= 300; i++) h.push({ struck: [i], manual: [] });
  let steps = 0;
  let last = null;
  for (let s = h.undo(); s !== null; s = h.undo()) { steps++; last = s; }
  assert.equal(steps, 199);
  assert.deepEqual(last, { struck: [101], manual: [] });
});

test("Outbox тримає лише останній знімок і переживає перезавантаження", () => {
  const storage = fakeStorage();
  const a = new Outbox(storage, "k");
  a.set(S1);
  a.set(S2);
  assert.equal(a.pending, true);
  assert.deepEqual(new Outbox(storage, "k").restore(), S2);
  a.markSent(S2);
  assert.equal(a.pending, false);
  assert.equal(new Outbox(storage, "k").restore(), null);
});

test("Outbox.markSent старого знімка не стирає новіший", () => {
  const storage = fakeStorage();
  const o = new Outbox(storage, "k");
  o.set(S1);
  o.set(S2);
  o.markSent(S1);
  assert.equal(o.pending, true);
  assert.deepEqual(new Outbox(storage, "k").restore(), S2);
});

test("Outbox не падає, коли сховище кидає", () => {
  const boom = () => { throw new Error("quota"); };
  const o = new Outbox({ getItem: boom, setItem: boom, removeItem: boom }, "k");
  o.set(S1);
  assert.equal(o.pending, true);
  assert.equal(o.restore(), null);
  o.markSent(S1);
  assert.equal(o.pending, false);
});

test("Outbox.restore не віддає того, що не схоже на знімок", () => {
  // У сховищі може лежати що завгодно: чужий ключ, обрізаний запис, стара версія.
  for (const junk of ["null", "7", "\"x\"", "[]", "{}", "{\"struck\":[1]}", "{\"struck\":{},\"manual\":[]}", "{"]) {
    const storage = fakeStorage();
    storage.setItem("k", junk);
    const o = new Outbox(storage, "k");
    assert.equal(o.restore(), null, junk);
    assert.equal(o.pending, false, junk);
  }
});

test("strokePath: сухий хвіст короткий, довгий мазок не тоншає на останніх словах", () => {
  // Хвіст у частку ширини на довгому рядку лишав би останні слова ледь зачепленими.
  const w = 1000;
  const h = 30;
  for (const s of [0, 1, seed(5), seed(77)]) {
    const nums = strokePath(w, h, s).match(/-?\d+(\.\d+)?/g).map(Number);
    const at = new Map(); // x -> усі y контуру на цій вертикалі: верхній і нижній край
    for (let i = 0; i < nums.length; i += 2) {
      if (nums[i] <= w - 200 || nums[i] >= w - 80) continue;
      at.set(nums[i], [...(at.get(nums[i]) ?? []), nums[i + 1]]);
    }
    const thick = [...at.values()].filter((ys) => ys.length > 1).map((ys) => Math.max(...ys) - Math.min(...ys));
    assert.ok(thick.length >= 4);
    assert.ok(Math.min(...thick) >= h * 0.42, `seed ${s}: ${Math.min(...thick)}`);
  }
});

test("Outbox несе відбиток основи, але порівнює знімки без нього", () => {
  // Сторінка кладе в знімок відбиток файлу, від якого зроблено правку: після
  // перезавантаження залишок іде на сервер зі старим відбитком і не затирає чуже.
  const storage = fakeStorage();
  const o = new Outbox(storage, "k");
  o.set({ ...S1, transcript: "t", stamp: "old" });
  assert.equal(new Outbox(storage, "k").restore().stamp, "old");
  // Та сама правка з оновленою основою замінює відбиток, а не стає іншою правкою.
  o.set({ ...S1, transcript: "t", stamp: "new" });
  assert.equal(new Outbox(storage, "k").restore().stamp, "new");
  o.markSent({ ...S1, transcript: "t", stamp: "whatever" });
  assert.equal(o.pending, false);
  assert.equal(new Outbox(storage, "k").restore(), null);
});

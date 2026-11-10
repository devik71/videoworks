// Уся арифметика сторінки викреслювача. Тут немає ні DOM, ні window, ні fetch:
// помилку в підрахунку слів чи секунд мусить ловити Node, а не людина з мишкою,
// тож решта файлів сторінки лише малює й викликає ці функції.

export const MOVE_THRESHOLD = 6; // px: менший рух у межах слова — це клік, а не мазок
export const REACTION = 0.3; // с: людина тисне клавішу вже після того, як слово прозвучало
export const MIN_MANUAL = 0.2; // с: коротший відрізок не почути й не влучити в нього
export const NUDGE = 0.1; // с: крок підсування межі відрізка
export const CONTEXT = 1.5; // с: скільки навколо викресленого прослухати перед ефіром

const HISTORY_LIMIT = 200;
const r3 = (v) => Math.round(v * 1000) / 1000;

// ---- жести над словами ----

// Жест, що почався на викресленому слові, стирає; на чистому — викреслює.
// Режим визначає перше слово, щоб один рух не міняв його посеред шляху.
export function gestureMode(struck, first) {
  return struck.has(first) ? "erase" : "strike";
}

// Швидка миша перестрибує слова, тож між двома подіями добираємо всі проміжні.
// Порожні слова викреслювати нічого, їх пропускаємо.
export function fill(last, next, blank) {
  const out = [];
  const step = last === null || next >= last ? 1 : -1;
  const from = last === null ? next : last + step;
  for (let i = from; step > 0 ? i <= next : i >= next; i += step) {
    if (!blank.has(i)) out.push(i);
  }
  return out;
}

// Нова множина: стара лежить в історії скасувань і мусить лишатися недоторканою.
export function applyGesture(struck, touched, mode) {
  const out = new Set(struck);
  for (const i of touched) {
    if (mode === "strike") out.add(i);
    else out.delete(i);
  }
  return out;
}

export function isStroke(dx, dy, crossedWord) {
  return crossedWord || Math.hypot(dx, dy) >= MOVE_THRESHOLD;
}

// Суцільні викреслені слова одного рядка — один мазок пензля, а не по мазку на слово.
export function runs(struck, lineOf) {
  const out = [];
  for (const i of [...struck].sort((a, b) => a - b)) {
    const tail = out[out.length - 1];
    if (tail && tail[1] === i - 1 && lineOf(tail[1]) === lineOf(i)) tail[1] = i;
    else out.push([i, i]);
  }
  return out;
}

// ---- мазок пензля ----

// Хеш цілого (Wang): сусідні слова мають отримати помітно різні мазки.
export function seed(index) {
  let h = (index | 0) >>> 0;
  h = ((h ^ 61) ^ (h >>> 16)) >>> 0;
  h = (h + (h << 3)) >>> 0;
  h = (h ^ (h >>> 4)) >>> 0;
  h = Math.imul(h, 0x27d4eb2d) >>> 0;
  return (h ^ (h >>> 15)) >>> 0;
}

function mulberry32(a) {
  return () => {
    a = (a + 0x6d2b79f5) >>> 0;
    let t = Math.imul(a ^ (a >>> 15), 1 | a);
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

// Замкнений контур мазка в рамці 0..width × 0..height. Той самий вхід дає той самий
// рядок, тож мазок не «пливе» при перемальовуванні. Виглядає рукотворним: товщина
// гуляє, на початку краплина фарби, до кінця пензель сохне й обривається бахромою,
// а вся лінія трохи з'їжджає вбік. Виліт за рамку обмежений 6 px.
export function strokePath(width, height, seedValue) {
  const rnd = mulberry32(seedValue >>> 0);
  const w = Math.max(width, 1);
  const h = Math.max(height, 1);
  const pad = 6;
  const n = Math.max(2, Math.ceil(w / 12) + 1);
  const slant = (rnd() - 0.5) * 3;
  const top = [];
  const bottom = [];
  for (let k = 0; k < n; k++) {
    const u = k / (n - 1);
    const x = u === 1 ? w - rnd() * 3 : w * u;
    const center = h / 2 + slant * (u - 0.5);
    let thick = h * (0.62 + (rnd() - 0.5) * 0.24);
    if (u < 0.15) thick *= 1.25; // потовщення там, де пензель торкнувся
    let fray = 0.6;
    if (u > 0.7) {
      const dry = (u - 0.7) / 0.3;
      thick *= 1 - 0.65 * dry; // хвіст тоншає
      fray += 2.4 * dry; // і рветься
    }
    top.push([x, center - thick / 2 + (rnd() - 0.5) * 2 * fray]);
    bottom.push([x, center + thick / 2 + (rnd() - 0.5) * 2 * fray]);
  }
  const clampX = (v) => Math.min(w + pad, Math.max(-pad, v));
  const clampY = (v) => Math.min(h + pad, Math.max(-pad, v));
  const fmt = (v) => String(Math.round(v * 10) / 10 + 0);
  const pt = (p) => `${fmt(clampX(p[0]))} ${fmt(clampY(p[1]))}`;
  const mid = (a, b) => [(a[0] + b[0]) / 2, (a[1] + b[1]) / 2];
  // Ламана → гладка: кожна точка стає керуючою квадратичної кривої між серединами.
  const edge = (pts) => {
    let d = "";
    for (let i = 1; i < pts.length - 1; i++) d += `Q${pt(pts[i])} ${pt(mid(pts[i], pts[i + 1]))}`;
    return d + `L${pt(pts[pts.length - 1])}`;
  };
  const back = bottom.slice().reverse();
  return `M${pt(top[0])}${edge(top)}L${pt(back[0])}${edge(back)}L${pt(bottom[0])}Z`;
}

// ---- звук і фрази ----

// Відрізки відсортовані й не перетинаються; [start, end): кінець уже звучить.
function lastStartingAtOrBefore(items, t, startOf) {
  let lo = 0;
  let hi = items.length - 1;
  let found = -1;
  while (lo <= hi) {
    const mid = (lo + hi) >> 1;
    if (startOf(items[mid]) <= t) {
      found = mid;
      lo = mid + 1;
    } else hi = mid - 1;
  }
  return found;
}

export function isMuted(spans, t) {
  const i = lastStartingAtOrBefore(spans, t, (s) => s[0]);
  return i >= 0 && t < spans[i][1];
}

// Найближча межа після t: за нею планують рампу гучності.
export function nextEdge(spans, t) {
  let lo = 0;
  let hi = spans.length;
  while (lo < hi) {
    const mid = (lo + hi) >> 1;
    if (spans[mid][1] > t) hi = mid;
    else lo = mid + 1;
  }
  if (lo === spans.length) return null;
  return t < spans[lo][0] ? spans[lo][0] : spans[lo][1];
}

export function phraseAt(phrases, t) {
  return lastStartingAtOrBefore(phrases, t, (p) => p.start);
}

// ---- ручні відрізки ----

// Утримання клавіші: початок відступає на час реакції, бо слово вже прозвучало.
// Надто короткий відрізок подовжуємо, а біля кінця запису зсуваємо назад.
export function manualFromHold(pressed, released, duration) {
  if (![pressed, released, duration].every(Number.isFinite) || released < pressed) return null;
  let start = Math.min(Math.max(pressed - REACTION, 0), duration);
  let end = Math.min(released, duration);
  if (end - start < MIN_MANUAL) {
    end = start + MIN_MANUAL;
    if (end > duration) {
      end = duration;
      start = Math.max(duration - MIN_MANUAL, 0);
    }
  }
  return [r3(start), r3(end)];
}

// Рухома межа зупиняється, коли відрізок стає завузьким, і не виходить за запис.
export function nudge(span, edge, delta, duration) {
  const [start, end] = span;
  if (edge === "start") {
    return [r3(Math.min(Math.max(start + delta, 0), end - MIN_MANUAL)), r3(end)];
  }
  return [r3(start), r3(Math.max(Math.min(end + delta, duration), start + MIN_MANUAL))];
}

export function removeManualAt(manual, index) {
  return manual.filter((_, i) => i !== index);
}

// Куди слухати перед ефіром: кожне глушіння з запасом, сусідні запаси зливаємо.
export function reviewStops(spans, duration) {
  const out = [];
  for (const [s, e] of [...spans].sort((a, b) => a[0] - b[0])) {
    const from = r3(Math.max(s - CONTEXT, 0));
    const to = r3(Math.min(e + CONTEXT, duration));
    const tail = out[out.length - 1];
    if (tail && from <= tail[1]) tail[1] = Math.max(tail[1], to);
    else out.push([from, to]);
  }
  return out;
}

// ---- історія й черга відправки ----

// Знімки порівнюємо за значенням: однаковий вміст — однаковий ключ.
const keyOf = (snapshot) => JSON.stringify([snapshot.struck, snapshot.manual]);
const copyOf = (snapshot) => JSON.parse(JSON.stringify(snapshot));

export class History {
  constructor(snapshot) {
    this.items = [copyOf(snapshot)];
    this.at = 0;
  }

  push(snapshot) {
    if (keyOf(snapshot) === keyOf(this.items[this.at])) return;
    this.items.length = this.at + 1; // новий жест відтинає гілку «повернути»
    this.items.push(copyOf(snapshot));
    if (this.items.length > HISTORY_LIMIT) this.items.shift();
    this.at = this.items.length - 1;
  }

  undo() {
    if (!this.canUndo) return null;
    this.at -= 1;
    return copyOf(this.items[this.at]);
  }

  redo() {
    if (!this.canRedo) return null;
    this.at += 1;
    return copyOf(this.items[this.at]);
  }

  get canUndo() {
    return this.at > 0;
  }

  get canRedo() {
    return this.at < this.items.length - 1;
  }
}

// Остання ще не підтверджена сервером правка. Лежить у сховищі, щоб пережити
// перезавантаження; кожен виклик сховища обгорнуто, бо приватний режим чи повна
// квота кидають винятки, а правка в пам'яті від цього не повинна пропасти.
export class Outbox {
  constructor(storage, key) {
    this.storage = storage;
    this.key = key;
    this.held = null;
  }

  set(snapshot) {
    this.held = copyOf(snapshot);
    try {
      this.storage.setItem(this.key, JSON.stringify(this.held));
    } catch {
      // лишається лише пам'ять
    }
  }

  get pending() {
    return this.held !== null;
  }

  // Підтвердження старого знімка не має стирати новішу правку, що вже чекає.
  markSent(snapshot) {
    if (this.held === null || keyOf(this.held) !== keyOf(snapshot)) return;
    this.held = null;
    try {
      this.storage.removeItem(this.key);
    } catch {
      // сховище недоступне — нічого стирати
    }
  }

  restore() {
    try {
      const raw = this.storage.getItem(this.key);
      if (raw === null || raw === undefined) return null;
      this.held = JSON.parse(raw);
      return copyOf(this.held);
    } catch {
      return null;
    }
  }
}

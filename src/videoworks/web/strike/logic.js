// Уся арифметика сторінки викреслювача. Тут немає ні DOM, ні window, ні fetch:
// помилку в підрахунку слів чи секунд мусить ловити Node, а не людина з мишкою,
// тож решта файлів сторінки лише малює й викликає ці функції.

export const MOVE_THRESHOLD = 6; // px: менший рух у межах слова — це клік, а не мазок
export const REACTION = 0.3; // с: людина тисне клавішу вже після того, як слово прозвучало
export const MIN_MANUAL = 0.2; // с: коротший відрізок не почути й не влучити в нього
export const NUDGE = 0.1; // с: крок підсування межі відрізка
export const CONTEXT = 1.5; // с: скільки навколо викресленого прослухати перед ефіром
export const FILL_SLACK = 4; // px: рука веде не рівно по рядку, шлях трохи товщий за лінію
export const FILL_LIMIT = 40; // слів: більше за одну подію миші не перестрибнути й на бігу

const HISTORY_LIMIT = 200;
const r3 = (v) => Math.round(v * 1000) / 1000;

// ---- жести над словами ----

// Жест, що почався на викресленому слові, стирає; на чистому — викреслює.
// Режим визначає перше слово, щоб один рух не міняв його посеред шляху.
export function gestureMode(struck, first) {
  return struck.has(first) ? "erase" : "strike";
}

// Чи проходить відрізок шляху пензля (a → b) крізь прямокутник слова, розширений
// на slack. Відтинання Ліанга — Барського: відрізок ріжемо чотирма межами по черзі.
export function crosses(ax, ay, bx, by, rect, slack = FILL_SLACK) {
  if (!rect || ![ax, ay, bx, by].every(Number.isFinite)) return false;
  const dx = bx - ax;
  const dy = by - ay;
  let t0 = 0;
  let t1 = 1;
  const edges = [
    [-dx, ax - (rect.left - slack)], [dx, rect.right + slack - ax],
    [-dy, ay - (rect.top - slack)], [dy, rect.bottom + slack - ay],
  ];
  for (const [p, q] of edges) {
    if (p === 0) {
      if (q < 0) return false; // іде вздовж межі й лежить поза нею
    } else if (p < 0) t0 = Math.max(t0, q / p);
    else t1 = Math.min(t1, q / p);
    if (t0 > t1) return false;
  }
  return true;
}

// Те саме для шляху з кількох точок: слово на шляху, якщо його зачепив хоч один
// відрізок. Шлях з однієї точки — це сама точка.
export function sweeps(path, rect, slack = FILL_SLACK) {
  if (path.length === 1) {
    return crosses(path[0][0], path[0][1], path[0][0], path[0][1], rect, slack);
  }
  for (let k = 1; k < path.length; k++) {
    const a = path[k - 1];
    const b = path[k];
    if (crosses(a[0], a[1], b[0], b[1], rect, slack)) return true;
  }
  return false;
}

// Швидка миша перестрибує слова, тож між двома подіями добираємо проміжні.
// Порожні слова викреслювати нічого, їх пропускаємо.
//
// crossed(i) каже, чи пройшов над словом i шлях вказівника від останнього взятого
// слова до цього. Саме від останнього взятого, а не від попередньої події: подія
// може влучити в проміжок між словами чи в поле, де слова немає, і тоді відрізок
// «від попередньої події» починався б уже за словами, які рука щойно перетнула.
// Із ним діє правило «викреслюється лише те, над чим пройшла рука»:
//   - слово під вказівником (next) береться завжди;
//   - проміжне — лише якщо шлях його перетнув. Швидкий рух уздовж рядка перетинає
//     кожне слово на ньому; рух навскоси через три рядки — по слову-два на рядок, а
//     не всі слова між початком і кінцем за номерами;
//   - якщо проміжних більше за FILL_LIMIT, це не рука, а аркуш поїхав під вказівником
//     (прокрутка, зсув розкладки): не добираємо нічого й геометрію навіть не питаємо.
// Цей інструмент вирішує, що вирізати з ефіру: недобране слово людина домалює,
// а зайве викреслене може не помітити.
export function fill(last, next, blank, crossed = null) {
  const out = [];
  const step = last === null || next >= last ? 1 : -1;
  const from = last === null ? next : last + step;
  for (let i = from; step > 0 ? i <= next : i >= next; i += step) {
    if (!blank.has(i)) out.push(i);
  }
  if (!crossed) return out;
  if (out.length > FILL_LIMIT) return blank.has(next) ? [] : [next];
  return out.filter((i) => i === next || crossed(i));
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
  // Пензель сохне на останніх сантиметрах, а не на третині рядка: на довгому
  // мазку частка ширини лишала б останні слова ледь зачепленими.
  const tail = Math.min(0.3, 72 / w);
  const top = [];
  const bottom = [];
  for (let k = 0; k < n; k++) {
    const u = k / (n - 1);
    const x = u === 1 ? w - rnd() * 3 : w * u;
    const center = h / 2 + slant * (u - 0.5);
    let thick = h * (0.62 + (rnd() - 0.5) * 0.24);
    if (u < 0.15) thick *= 1.25; // потовщення там, де пензель торкнувся
    let fray = 0.6;
    if (u > 1 - tail) {
      const dry = (u - (1 - tail)) / tail;
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
// Відрізок, уже коротший за мінімум (такі приходять зі старої розшифровки), можна
// лише розширювати. Межі запису застосовуємо останніми — інакше саме такий
// короткий відрізок виштовхнуло б за нуль чи за кінець.
export function nudge(span, edge, delta, duration) {
  const [start, end] = span;
  if (edge === "start") {
    const cap = Math.max(end - MIN_MANUAL, Math.min(start, end));
    return [r3(Math.max(Math.min(start + delta, cap), 0)), r3(end)];
  }
  const floor = Math.min(start + MIN_MANUAL, Math.max(start, end));
  return [r3(start), r3(Math.min(Math.max(end + delta, floor), duration))];
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

// ---- звук пензля й слова для чисел ----

// Висота писку: з кожним словом у жесті на півтон вище (довгий мазок звучить як
// глісандо), швидший рух — дзвінкіше. Стеля в 14 слів і межі 400…2400 Гц тримають
// звук у смузі, де він смішний, а не пронизливий. Сміття на вході дає найнижчу ноту.
export function squeakPitch(step, speed) {
  const word = Math.min(Math.max(Number(step) || 0, 0), 14);
  const rush = Math.min(Math.max(Number(speed) || 0, 0), 2);
  const pitch = 520 * 2 ** (word / 12) * (0.85 + rush * 0.2);
  return Math.min(Math.max(pitch, 400), 2400);
}

// Українське відмінювання за числом: forms = [1 слово, 2 слова, 5 слів].
export function plural(n, forms) {
  const whole = Math.abs(Math.trunc(n));
  const unit = whole % 10;
  const tens = whole % 100;
  if (unit === 1 && tens !== 11) return forms[0];
  if (unit >= 2 && unit <= 4 && (tens < 12 || tens > 14)) return forms[1];
  return forms[2];
}

// Тривалість для людини. Спершу округлюємо все число, потім ділимо на хвилини:
// інакше 119.6 с дає «1 хв 60 с», а 59.6 — «60 с».
export function seconds(v) {
  if (v < 10) return `${v.toFixed(1)} с`;
  const whole = Math.round(v);
  if (whole < 60) return `${whole} с`;
  return `${Math.floor(whole / 60)} хв ${whole % 60} с`;
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

  // Серія дрібних правок одного й того самого (підсування межі клавішею) — один
  // крок: останній знімок переписуємо. Якщо серія повернулась туди, звідки
  // почалась, крок зникає зовсім — скасовувати порожнє нема чого.
  amend(snapshot) {
    if (this.at === 0) {
      this.push(snapshot);
      return;
    }
    this.items.length = this.at + 1;
    if (keyOf(snapshot) === keyOf(this.items[this.at - 1])) {
      this.items.pop();
      this.at -= 1;
    } else this.items[this.at] = copyOf(snapshot);
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
      const found = JSON.parse(raw);
      // Під ключем може лежати сміття; знімок без обох списків зламав би сторінку далі.
      if (!found || !Array.isArray(found.struck) || !Array.isArray(found.manual)) return null;
      this.held = found;
      return copyOf(this.held);
    } catch {
      return null;
    }
  }
}

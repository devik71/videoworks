// Сторінка викреслювача: стан, клавіші й збереження. Рахує logic.js, аркуш показує
// sheet.js, жести ловить brush.js, грає й глушить player.js, решта файлів — по одній
// справі кожен; тут їхнє зведення докупи. Правила глушіння живуть на сервері:
// відрізки й лічильники сторінка тільки показує.

import {
  applyGesture, phraseAt, plural, removeManualAt, History, Outbox, CONTEXT,
} from "./logic.js";
import { attach } from "./brush.js";
import { createPlayer } from "./player.js";
import { createSound } from "./sound.js";
import { createMap } from "./map.js";
import { createManual } from "./manual.js";
import { createTour } from "./tour.js";
import { createHelp } from "./help.js";
import { createSaver } from "./saving.js";
import { bindKeys } from "./keys.js";
import { note, unnote, tell, say, live } from "./notes.js";
import { storage, keep, recallObject } from "./memory.js";
import {
  sheet, build, show, showManual, manualRow, wholePhrase, bring, freeze, quietState, make,
  clock,
} from "./sheet.js";

const EDGE = 0.15; // с: межі слів від ASR неточні, тож фразу граємо із запасом
const LINGER = 0.12; // с: слово ще підсвічене, поки не почалось наступне
const SAVE_DELAY = 500;
const HURRY_DELAY = 150; // мс: край позначки посунули й чекають, щоб його почути
// Залишок без відбитка (записаний старішою сторінкою): невідомо, від чого його робили,
// тож вважаємо основу застарілою. Справжній відбиток — sha256 або порожній рядок.
const UNKNOWN_BASE = "?";
const SOUND_KEY = "strike:sound"; // один вибір на всі заняття: це звичка, а не проєкт
const REVIEW = ["перевірити викреслене", "перевірити"]; // підпис кнопки: повний і вузький
const NEAR_START = 0.5; // с: відрізок без слів, що починає позначку, а не лежить у ній

const $ = (id) => document.getElementById(id);
const els = {
  sheet: $("sheet"), video: $("video"), bar: $("bar"), count: $("count"), mode: $("mode"),
  play: $("play"), clock: $("clock"), undo: $("undo"), redo: $("redo"),
  stage: document.querySelector(".stage"),
  review: $("review"), help: $("help"), sound: $("sound"), blot: $("blot"),
};

// Єдиний стан сторінки. struck і manual — намір людини; spans і counts — те, що
// з нього вивів сервер, і сторінка їх ніколи не перераховує сама.
const state = {
  data: null, struck: new Set(), manual: [], spans: [], counts: null, stamp: "",
  active: 0, mode: "phrase",
  blot: -1, // відрізок без слів, з яким зараз працюють клавіші [ і ]; −1 — жодного
};

let history = null;
let outbox = null;
let viewKey = "";
let nowWord = -1;
let clockSecond = -1;
let videoBroken = false;

// Місце на аркуші сторінка памʼятає сама — це активна фраза. Браузер після
// перезавантаження повертає ще й свою прокрутку, причому пізніше за нас: аркуш
// їхав з-під вказівника, і активна фраза опинялась за межами вікна.
if ("scrollRestoration" in window.history) window.history.scrollRestoration = "manual";

// ---- дрібниці ----

function seconds(v) {
  if (v < 10) return `${v.toFixed(1)} с`;
  if (v < 60) return `${Math.round(v)} с`;
  return `${Math.floor(v / 60)} хв ${Math.round(v % 60)} с`;
}

function snapshot() {
  return {
    struck: [...state.struck].sort((a, b) => a - b),
    manual: state.manual.map((pair) => [pair[0], pair[1]]),
    transcript: state.data.transcript,
    // Від якої версії файлу зроблено правку: залишок, що пережив перезавантаження,
    // мусить іти на сервер зі своїм старим відбитком, а не зі щойно прочитаним.
    stamp: state.stamp,
  };
}

const marks = (snap) => JSON.stringify([snap.struck, snap.manual]);

// ---- лічильник ----

// Усе, що показує відрізки й лічильники від сервера. Числа — його; сторінка лише
// відмінює слова.
function showMarks() {
  const { words, manual, seconds: total } = state.counts;
  const parts = [];
  if (words) parts.push(`${words} ${plural(words, ["слово", "слова", "слів"])}`);
  if (manual && words) parts.push(`${manual} без слів`);
  else if (manual) parts.push(`${manual} ${plural(manual, ["місце", "місця", "місць"])} без слів`);
  if (parts.length) {
    parts.push(seconds(total));
    els.count.replaceChildren(make("span", "lead", "викреслено "), parts.join(" · "));
  } else els.count.textContent = "ще нічого не викреслено";
  els.review.disabled = state.spans.length === 0;
  chart.marks(state.spans);
}

function syncSteps() {
  els.undo.disabled = !history.canUndo;
  els.redo.disabled = !history.canRedo;
}

// ---- активна фраза й памʼять ----

function setActive(index, { instant = false, scroll = true } = {}) {
  const { phrases } = sheet;
  if (!phrases.length) return;
  const next = Math.min(Math.max(index, 0), phrases.length - 1);
  const old = phrases[state.active];
  if (old && next !== state.active) {
    old.el.classList.remove("active");
    old.el.removeAttribute("aria-current");
  }
  state.active = next;
  phrases[next].el.classList.add("active");
  phrases[next].el.setAttribute("aria-current", "true");
  remember();
  if (scroll) bring(phrases[next], instant);
}

// Відрізок без слів, з яким працюють клавіші [ і ]. Вибрати його — це ще й сказати,
// що клавіші тепер про нього.
function setBlot(index) {
  state.blot = index >= 0 && index < state.manual.length ? index : -1;
  showManual(state.manual, state.blot);
}

function remember() {
  keep(viewKey, JSON.stringify({ active: state.active, mode: state.mode }));
}

// ---- мапа й звук ----

const chart = createMap($("chart"), {
  onSeek(t) {
    const { phrases } = sheet;
    if (!phrases.length) return;
    tour.quit();
    if (state.blot >= 0) setBlot(-1);
    const at = Math.max(phraseAt(phrases, t), 0);
    setActive(at);
    // У режимі «по фразах» догравання спиняється там, де скінчиться фраза під
    // головкою, а якщо головка в паузі — наступна.
    const upto = t <= phrases[at].end || !phrases[at + 1] ? phrases[at] : phrases[at + 1];
    const stop = state.mode === "phrase" && player.playing && t <= upto.end;
    player.seek(t, stop ? upto.end + EDGE : null);
  },
});

let voice = null;
let soundOn = storage.getItem(SOUND_KEY) !== "off";

// Звук пензля. Кликати лише з жесту людини: саме тоді браузер дозволяє звук, і
// саме тоді плеєр збирає свій аудіограф, контекст якого тут і береться.
function ping(kind, step = 0, speed = 0) {
  if (!soundOn) return;
  const context = player.audio();
  if (!context) return;
  if (!voice) voice = createSound(context);
  voice[kind](step, speed);
}

function setSound(on, { silent = false } = {}) {
  soundOn = on;
  els.sound.setAttribute("aria-pressed", String(on));
  els.sound.setAttribute("aria-label", on ? "Звук пензля ввімкнено" : "Звук пензля вимкнено");
  if (silent) return;
  keep(SOUND_KEY, on ? "on" : "off");
  live(on ? "звук пензля ввімкнено" : "звук пензля вимкнено", 1600);
  if (on) ping("squeak", 5, 1);
}

// ---- відтворення ----

const player = createPlayer(els.video, {
  spans: () => state.spans,
  onTick(t, stopped) {
    if (!state.data) return;
    const whole = Math.floor(t);
    if (whole !== clockSecond) {
      clockSecond = whole;
      els.clock.textContent = `${clock(t)} / ${clock(state.data.duration)}`;
    }
    chart.head(t);
    marker.tick(t);
    const { wordEls, phrases } = sheet;
    // Слова теж відсортовані за початком і мають .start — пошук той самий, що для фраз.
    let word = stopped ? -1 : phraseAt(state.data.words, t);
    if (word >= 0 && (!wordEls[word] || t > state.data.words[word].end + LINGER)) word = -1;
    if (word !== nowWord) {
      if (nowWord >= 0) wordEls[nowWord].classList.remove("now");
      if (word >= 0) wordEls[word].classList.add("now");
      nowWord = word;
    }
    if (stopped) {
      tour.arrived();
      return;
    }
    // Аркуш їде за головкою в режимі «підряд», під час обходу позначок і поки
    // тримають позначку без слів: в усіх трьох випадках відео йде далі за фразу.
    const follow = state.mode === "flow" || tour.following || marker.holding;
    if (!follow || !player.playing) return;
    const at = phraseAt(phrases, t);
    // Розгін перед щойно обраною фразою ще лежить у попередній — це не привід вертатись.
    const lead = phrases[state.active].start - t;
    if (at >= 0 && at !== state.active && !(lead > 0 && lead <= 2 * EDGE)) {
      // Коли вибрано рядок без слів, аркуш стоїть на ньому, а не тікає за фразою.
      setActive(at, { scroll: state.blot < 0 });
    }
  },
  onChange(playing) {
    document.body.classList.toggle("playing", playing);
    els.play.setAttribute("aria-label", playing ? "Спинити" : "Грати");
  },
});

function seekPhrase(index) {
  if (!sheet.phrases.length) return;
  tour.quit();
  if (state.blot >= 0) setBlot(-1);
  setActive(index);
  const phrase = sheet.phrases[state.active];
  player.play(phrase.start - EDGE, state.mode === "phrase" ? phrase.end + EDGE : null);
}

function toggle() {
  if (!sheet.phrases.length) return;
  tour.quit();
  if (player.playing) player.pause();
  else if (state.mode === "phrase") seekPhrase(state.active);
  else player.play(null);
}

function setMode(mode) {
  state.mode = mode === "flow" ? "flow" : "phrase";
  for (const button of els.mode.querySelectorAll("button")) {
    button.setAttribute("aria-pressed", String(button.dataset.mode === state.mode));
  }
  if (sheet.phrases.length && player.playing && !tour.following) {
    player.until(state.mode === "phrase" ? sheet.phrases[state.active].end + EDGE : null);
  }
  remember();
}

// Після позначки без слів у режимі «по фразах»: відео дограє фразу, на якій
// відпустили, і стане; якщо відпустили в паузі між фразами — стає одразу.
function resume() {
  if (state.mode !== "phrase" || !player.playing) return;
  const at = phraseAt(sheet.phrases, player.time);
  const phrase = sheet.phrases[at];
  if (phrase && player.time <= phrase.end) {
    setActive(at, { scroll: false });
    player.until(phrase.end + EDGE);
  } else player.pause();
}

// ---- правки й історія ----

// Єдиний вхід для будь-якої зміни позначок: жест, клавіша X, скасування, відновлення
// після обриву. record === false — крок уже лежить в історії (це саме скасування);
// amend — продовження попереднього кроку (серія підсувань межі); hurry — записати
// швидше, бо людина чекає, щоб почути результат. Повертає, чи щось змінилось.
function commit(next, { record = true, amend = false, hurry = false } = {}) {
  const before = snapshot();
  state.struck = new Set(next.struck);
  state.manual = next.manual.map((pair) => [pair[0], pair[1]]);
  const now = snapshot();
  show(state.struck);
  if (marks(before) === marks(now)) return false;
  if (JSON.stringify(before.manual) !== JSON.stringify(now.manual)) {
    if (state.blot >= state.manual.length) state.blot = -1;
    showManual(state.manual, state.blot);
  }
  if (record && amend) history.amend(now);
  else if (record) history.push(now);
  syncSteps();
  outbox.set(now);
  saver.schedule(hurry ? HURRY_DELAY : SAVE_DELAY);
  return true;
}

// Крок історії міняє й список відрізків без слів — вибраний номер уже не про те.
function travel(snap) {
  if (!snap) return;
  if (state.blot >= 0) setBlot(-1);
  commit(snap, { record: false });
}

// Поки пензель на папері, історія стоїть: крок назад посеред мазка зсунув би
// рядки під рукою й переписав би те, від чого жест почався.
const undo = () => {
  if (!brush.live) travel(history.undo());
};
const redo = () => {
  if (!brush.live) travel(history.redo());
};

function strikeActive() {
  const phrase = sheet.phrases[state.active];
  if (!phrase || brush.live) return;
  const all = [];
  for (let i = phrase.first; i < phrase.first + phrase.count; i++) {
    if (sheet.wordEls[i]) all.push(i);
  }
  const mode = all.every((i) => state.struck.has(i)) ? "erase" : "strike";
  const next = { struck: [...applyGesture(state.struck, all, mode)], manual: state.manual };
  if (commit(next)) ping(mode === "strike" ? "splat" : "pop");
}

// ---- відрізки без слів і обхід позначок ----

function dropManual(m) {
  if (!state.manual[m]) return;
  state.blot = -1;
  const next = { struck: [...state.struck], manual: removeManualAt(state.manual, m) };
  if (commit(next)) ping("pop");
}

// Клік по рядку: послухати його разом із тим, що довкола, і взяти під клавіші.
function playManual(m) {
  const span = state.manual[m];
  if (!span) return;
  tour.quit();
  setBlot(m);
  player.play(span[0] - CONTEXT, Math.min(span[1] + CONTEXT, state.data.duration));
}

// Рядки, які жест зараз зітре, бліднуть ще до відпускання.
function fadeRows(blots) {
  for (const row of els.sheet.querySelectorAll(".manual")) {
    row.classList.toggle("going", blots.includes(Number(row.dataset.m)));
  }
}

// Показує на аркуші місце позначки (відрізка глушіння від сервера): фразу, в якій
// вона починається, або рядок без слів, якщо позначка починається саме з нього.
function reveal(span) {
  const { phrases } = sheet;
  if (!phrases.length) return;
  let at = Math.max(phraseAt(phrases, span[0]), 0);
  // Запас глушіння заходить у паузу перед фразою — тоді позначка належить наступній.
  if (phrases[at].end <= span[0] && phrases[at + 1] && phrases[at + 1].start < span[1]) at += 1;
  const m = state.manual.findIndex(
    ([from, to]) => from >= span[0] - 0.001 && to <= span[1] + 0.001
      && from - span[0] < NEAR_START,
  );
  const row = m >= 0 ? manualRow(m) : null;
  if (state.blot !== (row ? m : -1)) setBlot(row ? m : -1);
  setActive(at, { scroll: !row });
  if (row) bring({ el: manualRow(m) });
}

const marker = createManual({
  state, player, video: els.video, button: els.blot, commit, resume, live, ping,
  begin: () => tour.quit(),
  strip: (span) => chart.hold(span),
  // Нова позначка могла лягти далеко від активної фрази — показуємо її рядок.
  shown() {
    const row = manualRow(state.blot);
    if (row) bring({ el: row });
  },
});

const tour = createTour({
  state, player, reveal, live,
  reviewing(on) {
    els.review.setAttribute("aria-pressed", String(on));
    els.review.querySelector(".long").textContent = on ? "спинити перевірку" : REVIEW[0];
    els.review.querySelector(".short").textContent = on ? "спинити" : REVIEW[1];
  },
});

const help = createHelp($("keys"));

// ---- збереження ----

const saver = createSaver({
  state, snapshot,
  outbox: () => outbox,
  applied: showMarks,
  settled: () => marker.saved(),
  reread: () => load({ drop: true }),
});

// ---- завантаження ----

// Помилка відео може прийти раніше за стан, а назва проєкту для команди — зі стану.
function showVideoNote() {
  if (!videoBroken || !state.data) return;
  note("video", "Браузер не може програти це відео. Запусти:", {
    code: `videoworks strike ${state.data.title} --proxy`,
  });
}

// Залишок із минулого разу: беремо лише те, що справді є словом цієї розшифровки.
function cleaned(kept) {
  const count = state.data.words.length;
  const real = (i) => Number.isInteger(i) && i >= 0 && i < count && !sheet.blank.has(i);
  return {
    struck: kept.struck.filter(real),
    manual: kept.manual.filter(
      (pair) => Array.isArray(pair) && pair.length === 2 && pair.every(Number.isFinite),
    ),
  };
}

// drop — людина обрала «перечитати»: її незбережене свідомо поступається файлу.
let loads = 0; // щоб із двох «перечитати» поспіль аркуш будувало лише останнє

async function load({ drop = false } = {}) {
  const mine = ++loads;
  tour.quit();
  marker.cancel();
  await saver.restart();
  let data;
  try {
    const response = await fetch("/state.json", { cache: "no-store" });
    if (!response.ok) throw new Error(String(response.status));
    data = await response.json();
  } catch {
    if (mine !== loads) return;
    if (!state.data) {
      quietState("Не вдалося прочитати заняття. Перевір, чи ще працює videoworks strike.",
        ["спробувати ще", () => load()]);
    } else say("не вдалося перечитати", true);
    return;
  }
  if (mine !== loads) return; // тим часом попросили перечитати ще раз

  if (drop && outbox) outbox.markSent(snapshot());
  state.data = data;
  state.struck = new Set(data.struck);
  state.manual = data.manual;
  state.spans = data.spans;
  state.counts = data.counts;
  state.stamp = data.stamp;
  state.blot = -1;
  document.title = `${data.title} — викреслювач`;
  viewKey = `strike:${data.title}:view`;
  // restore() раніше за все інше: без нього черга не знає про залишок і не зітре його.
  outbox = new Outbox(storage, `strike:${data.title}:outbox`);
  const kept = outbox.restore();
  history = new History(snapshot());

  nowWord = -1;
  build(data);
  showManual(state.manual);
  show(state.struck, { quiet: true });
  chart.load(data);
  unnote("save");
  say("");
  showMarks();
  syncSteps();
  els.clock.textContent = `${clock(0)} / ${clock(data.duration)}`;
  const { unglued } = data;
  if (unglued > 0) {
    const lost = plural(unglued, ["слово не знайшлось", "слова не знайшлись", "слів не знайшлись"]);
    tell("unglued", `${unglued} ${lost} у новій розшифровці — вони стали позначками без слів.`);
  } else unnote("unglued");

  showVideoNote();

  const view = recallObject(viewKey);
  setMode(view.mode);
  if (sheet.phrases.length) {
    setActive(Number.isInteger(view.active) ? view.active : 0, { instant: true });
    if (!player.playing) els.video.currentTime = sheet.phrases[state.active].start;
  }

  if (kept && kept.transcript === data.transcript) {
    // Минулого разу правка не дійшла до файлу: повертаємо її й досилаємо одразу —
    // але з тим відбитком, від якого її робили. Якщо файл відтоді змінили деінде,
    // сервер відмовить, і людина сама обере, чиї позначки лишити.
    const fresh = state.stamp;
    state.stamp = typeof kept.stamp === "string" ? kept.stamp : UNKNOWN_BASE;
    if (commit(cleaned(kept))) saver.save();
    else {
      state.stamp = fresh;
      outbox.markSent(kept);
    }
  } else if (kept) {
    outbox.markSent(kept);
    tell("dropped", "Останні незбережені мазки не підійшли до нової розшифровки — "
      + "їх довелося відкинути.");
  }

  // Довідка сама показується раз на заняття: далі вона лише на вимогу.
  const seenKey = `strike:${data.title}:help`;
  if (sheet.phrases.length && !storage.getItem(seenKey)) {
    help.open();
    keep(seenKey, "1");
  }
}

// ---- жести, клавіші, кнопки ----

const brush = attach(els.sheet, {
  blank: { has: (i) => sheet.blank.has(i) }, // аркуш можуть перечитати, а пензель один
  struck: () => state.struck,
  rectOf: (i) => (sheet.wordEls[i] ? sheet.wordEls[i].getBoundingClientRect() : null),
  // Жест живий — під рукою ніщо не рухається: позначку без слів, яку ще тримали,
  // закриваємо до першого мазка, прокрутку аркуша відкладаємо до кінця жесту.
  onDown: () => marker.release(),
  onLive(on) {
    if (!freeze(on) || on) return;
    const row = state.blot >= 0 ? manualRow(state.blot) : null;
    if (row) bring({ el: row });
    else if (sheet.phrases.length) bring(sheet.phrases[state.active]);
  },
  onBlotClick: (m) => playManual(m),
  wordAt(x, y) {
    const el = document.elementFromPoint(x, y);
    return el && el.dataset && el.dataset.i !== undefined ? Number(el.dataset.i) : null;
  },
  blotAt(x, y) {
    const el = document.elementFromPoint(x, y);
    const row = el && el.closest(".manual");
    // Хрестик — окрема кнопка: натиск на неї не починає мазка.
    return row && !el.closest(".manual-x") ? Number(row.dataset.m) : null;
  },
  // Жест триває: показуємо, що вийде, але стан не чіпаємо до відпускання.
  onPreview(touched, mode, blots) {
    show(applyGesture(state.struck, touched, mode));
    fadeRows(blots);
  },
  onGesture(touched, mode, blots) {
    const manual = state.manual.filter((_, m) => !blots.includes(m));
    if (blots.length) state.blot = -1;
    const whole = mode === "strike" && wholePhrase(touched);
    const changed = commit({ struck: [...applyGesture(state.struck, touched, mode)], manual });
    fadeRows([]);
    if (!changed) return;
    if (whole) ping("splat");
    else if (blots.length && !touched.length) ping("pop");
  },
  onCancel() {
    show(state.struck);
    fadeRows([]);
  },
  onClick(index) {
    tour.quit();
    if (state.blot >= 0) setBlot(-1);
    setActive(sheet.phraseOf[index]);
    const phrase = sheet.phrases[state.active];
    const from = Math.max(state.data.words[index].start, phrase.start) - EDGE;
    player.play(from, state.mode === "phrase" ? phrase.end + EDGE : null);
  },
  // Кожне нове слово під пензлем звучить: викреслення пищить дедалі вище, а
  // стирання чпокає.
  onStep(index, speed, step, mode) {
    ping(mode === "strike" ? "squeak" : "pop", step, speed);
  },
});

bindKeys({
  ready: () => state.data !== null,
  painting: () => brush.live,
  helpShown: () => help.shown,
  help: (open) => (open ? help.open() : help.close()),
  undo, redo, toggle, strikePhrase: strikeActive,
  go: (dir) => seekPhrase(state.active + dir),
  holdStart: () => marker.press(),
  holdEnd: () => marker.release(),
  mark: (dir) => tour.step(dir),
  sound: () => setSound(!soundOn),
  nudge(edge, delta) {
    if (state.blot < 0) live("спершу клацни позначку «без слів»", 2200);
    else {
      tour.quit();
      marker.shift(edge, delta);
    }
  },
  escape() {
    if (tour.quit()) player.pause();
    else if (marker.holding) marker.cancel();
    else if (state.blot >= 0) setBlot(-1);
  },
});

els.play.addEventListener("click", toggle);
els.undo.addEventListener("click", undo);
els.redo.addEventListener("click", redo);
els.video.addEventListener("click", toggle);
els.mode.addEventListener("click", (event) => {
  const button = event.target.closest("button[data-mode]");
  if (button) setMode(button.dataset.mode);
});
els.sound.addEventListener("click", () => setSound(!soundOn));
els.review.addEventListener("click", () => tour.toggleReview());
// Відкрили мишею — фокус на кнопку не вертаємо, інакше пробіл тиснув би її.
els.help.addEventListener("click", (event) => help.toggle({ restore: event.detail === 0 }));
// Після кліку мишею фокус не лишається на кнопці, інакше пробіл тиснув би її знову,
// а не грав фразу. З клавіатури (detail === 0) фокус не чіпаємо.
els.bar.addEventListener("click", (event) => {
  const button = event.target.closest("button");
  if (button && event.detail > 0) button.blur();
});

els.sheet.addEventListener("click", (event) => {
  const row = event.target.closest(".manual");
  if (!row) return;
  const m = Number(row.dataset.m);
  if (event.target.closest(".manual-x")) dropManual(m);
  else playManual(m);
});

els.video.addEventListener("loadedmetadata", () => {
  const { videoWidth, videoHeight } = els.video;
  if (videoWidth && videoHeight) {
    els.stage.style.setProperty("--ratio", (videoWidth / videoHeight).toFixed(4));
  }
});
els.video.addEventListener("error", () => {
  videoBroken = true;
  showVideoNote();
});

window.addEventListener("beforeunload", (event) => {
  if (!outbox || !outbox.pending) return;
  event.preventDefault();
  event.returnValue = "";
});
// Вкладку ховають — не чекаємо пів секунди, шлемо зараз.
document.addEventListener("visibilitychange", () => {
  if (document.hidden && outbox && outbox.pending) saver.save();
});

setSound(soundOn, { silent: true });
els.video.src = "/video";
load();

// Аркуш: текст фраз, рядки пауз і відрізків без слів, фарба поверх слів і рух до
// активної фрази. Що саме викреслено й що з цим робити, вирішує app.js — тут лише
// показ того, що йому дали.

import { runs } from "./logic.js";
import { paint } from "./brush.js";

const NS = "http://www.w3.org/2000/svg";
const EYE_LEVEL = 0.28; // частка висоти аркуша, де тримається активна фраза
const GLIDE_MS = 180;
const NEAR = "400px 0px"; // запас довкола вікна, де мазки вже намальовані

const page = document.getElementById("page");
const stage = document.querySelector(".stage");
const bar = document.getElementById("bar");
const calm = window.matchMedia("(prefers-reduced-motion: reduce)");
const narrow = window.matchMedia("(max-width: 820px)");

// Те, що аркуш знає про текст; app.js читає, але не міняє.
export const sheet = {
  phrases: [], // { el, svg, geo, seen, first, count, start, end }
  wordEls: [], // <span> слова за його номером; null для порожнього
  phraseOf: [], // номер фрази за номером слова
  blank: new Set(), // порожні слова: їх не видно й не викреслити
};

// Рядки аркуша, що не міняються (фрази й музичні паузи), за часом: між ними
// шукають своє місце відрізки без слів.
let fixed = []; // { at, el }

let shown = new Set(); // що намальовано: стан або стан із жестом, який ще триває
let watcher = null;

export function make(tag, className, text) {
  const el = document.createElement(tag);
  if (className) el.className = className;
  if (text !== undefined) el.textContent = text;
  return el;
}

export function clock(t) {
  const whole = Math.max(0, Math.floor(t));
  return `${Math.floor(whole / 60)}:${String(whole % 60).padStart(2, "0")}`;
}

// Аркуш без тексту: завантаження не вдалось або слів немає взагалі.
export function quietState(text, action) {
  const box = make("div", "quiet-state");
  box.append(make("p", "", text));
  if (action) {
    const button = make("button", "pill", action[0]);
    button.type = "button";
    button.addEventListener("click", action[1]);
    box.append(button);
  }
  page.replaceChildren(box);
}

// Текст будуємо один раз і цілком: кілька тисяч слів браузер тримає легко, а
// пошук по сторінці й прокручування смугою працюють, як на звичайному аркуші.
export function build(data) {
  const { words, title, duration } = data;
  if (watcher) watcher.disconnect();
  sheet.phrases = [];
  sheet.wordEls = new Array(words.length).fill(null);
  sheet.phraseOf = new Array(words.length).fill(-1);
  sheet.blank = new Set();
  words.forEach((word, i) => {
    if (!word.w.trim()) sheet.blank.add(i);
  });

  if (!data.phrases.length) {
    quietState("У цьому записі немає жодного розпізнаного слова — викреслювати нічого.");
    return;
  }

  const head = make("header", "head");
  head.append(
    make("h1", "", title),
    make("p", "", `${clock(duration)} запису. Веди пензлем по словах, яких не має бути в ефірі; `
      + "клацни слово — почуєш його. Викреслене тут мовчить, а в готовому відео там буде музика."),
  );
  const rows = make("div", "rows");
  rows.id = "rows";

  fixed = [];
  const gaps = data.gaps.slice();
  const gapRow = ([from, to]) => {
    const el = make("div", "gap", `♪ ${clock(from)} – ${clock(to)}`);
    fixed.push({ at: from, el });
    return el;
  };
  data.phrases.forEach((phrase, p) => {
    while (gaps.length && gaps[0][0] < phrase.start) rows.append(gapRow(gaps.shift()));
    fixed.push({ at: phrase.start, el: null });
    const el = make("p", "phrase");
    el.dataset.p = String(p);
    el.dataset.t = clock(phrase.start);
    for (let i = phrase.first; i < phrase.first + phrase.count; i++) {
      if (sheet.blank.has(i)) continue;
      const span = make("span", words[i].dim ? "w dim" : "w", words[i].w.trim());
      span.dataset.i = String(i);
      sheet.wordEls[i] = span;
      sheet.phraseOf[i] = p;
      if (el.childNodes.length) el.append(" ");
      el.append(span);
    }
    rows.append(el);
    fixed[fixed.length - 1].el = el;
    sheet.phrases.push({ ...phrase, el, svg: null, geo: null, seen: false });
  });
  for (const gap of gaps) rows.append(gapRow(gap));
  page.replaceChildren(head, rows);

  // Мазки живуть лише біля вікна перегляду: тисячі <svg> поза ним нікому не потрібні.
  watcher = new IntersectionObserver((entries) => {
    for (const entry of entries) {
      const phrase = sheet.phrases[Number(entry.target.dataset.p)];
      phrase.seen = entry.isIntersecting;
      if (phrase.seen) paintPhrase(phrase, true);
      else if (phrase.svg) {
        phrase.svg.remove();
        phrase.svg = null;
      }
    }
  }, { rootMargin: NEAR });
  for (const phrase of sheet.phrases) watcher.observe(phrase.el);
}

// Відрізки без слів стоять на своєму місці в часі: перед першим рядком, що
// починається пізніше. Пауза, в якій відрізок лежить, почалась раніше — тож він
// стає під її рядком, а не над ним. Порядок у списку manual значення не має:
// номер у data-m — це місце в списку, а місце на аркуші визначає час.
// focused — номер відрізка, з яким зараз працюють клавіші [ і ].
export function showManual(manual, focused = -1) {
  const rows = document.getElementById("rows");
  if (!rows) return;
  // Перемальовування не має забирати фокус у того, хто йде аркушем із клавіатури.
  const held = rows.contains(document.activeElement)
    && document.activeElement.closest(".manual") !== null;
  for (const old of rows.querySelectorAll(".manual")) old.remove();
  const order = manual.map((_, m) => m).sort((a, b) => manual[a][0] - manual[b][0]);
  for (const m of order) {
    const [from, to] = manual[m];
    const row = make("div", m === focused ? "manual on" : "manual");
    row.dataset.m = String(m);
    const play = make("button", "manual-play", `♪ без слів · ${(to - from).toFixed(1)} с`);
    play.type = "button";
    play.title = `${clock(from)} – ${clock(to)} · клацни, щоб послухати`;
    const drop = make("button", "manual-x", "✕");
    drop.type = "button";
    drop.title = "Стерти цю позначку";
    drop.setAttribute("aria-label", "Стерти позначку без слів");
    row.append(play, drop);
    const next = fixed.find((item) => item.at > from);
    if (next) next.el.before(row);
    else rows.append(row);
    if (held && m === focused) play.focus({ preventScroll: true });
  }
}

// Рядок відрізка без слів за його номером у списку.
export function manualRow(m) {
  return document.querySelector(`#rows .manual[data-m="${m}"]`);
}

// ---- фарба ----

// Місця слів у фразі міряємо раз і пам'ятаємо до зміни ширини: жест перемальовує
// мазок на кожне нове слово, і міряти розкладку щоразу було б відчутно.
function geometry(phrase) {
  if (phrase.geo) return phrase.geo;
  const base = phrase.el.getBoundingClientRect();
  const geo = new Map();
  const r2 = (v) => Math.round(v * 100) / 100;
  let line = -1;
  let top = -Infinity;
  for (let i = phrase.first; i < phrase.first + phrase.count; i++) {
    if (!sheet.wordEls[i]) continue;
    const rect = sheet.wordEls[i].getBoundingClientRect();
    if (Math.abs(rect.top - top) > rect.height / 2) {
      line += 1;
      top = rect.top;
    }
    geo.set(i, {
      x: r2(rect.left - base.left), y: r2(rect.top - base.top),
      w: r2(rect.width), h: r2(rect.height), line,
    });
  }
  phrase.geo = geo;
  return geo;
}

function paintPhrase(phrase, quiet) {
  const mine = [];
  for (let i = phrase.first; i < phrase.first + phrase.count; i++) if (shown.has(i)) mine.push(i);
  if (!mine.length && !phrase.svg) return;
  if (!phrase.svg) {
    phrase.svg = document.createElementNS(NS, "svg");
    phrase.svg.setAttribute("class", "paint");
    phrase.svg.setAttribute("aria-hidden", "true");
    phrase.el.append(phrase.svg);
  }
  const geo = geometry(phrase);
  paint(phrase.svg, runs(mine, (i) => geo.get(i).line), (i) => geo.get(i), { quiet });
}

// Показує цю множину викреслених слів на всіх фразах біля вікна. quiet — без
// руху: так малюється те, що не є щойно зробленим мазком.
export function show(struck, { quiet = false } = {}) {
  shown = struck;
  for (const phrase of sheet.phrases) if (phrase.seen) paintPhrase(phrase, quiet);
}

// Чи накрив жест хоч одну фразу цілком (із двох і більше слів): тоді це вже не
// мазок по слову, а викреслена думка, і звучить вона інакше.
export function wholePhrase(touched) {
  const got = new Set(touched);
  for (const p of new Set(touched.map((i) => sheet.phraseOf[i]))) {
    const phrase = sheet.phrases[p];
    let words = 0;
    let whole = true;
    for (let i = phrase.first; i < phrase.first + phrase.count && whole; i++) {
      if (!sheet.wordEls[i]) continue;
      words += 1;
      whole = got.has(i);
    }
    if (whole && words >= 2) return true;
  }
  return false;
}

// Ширина аркуша змінилась — слова переїхали на інші рядки, старі заміри брешуть.
let pageWidth = 0;
new ResizeObserver(([entry]) => {
  const width = Math.round(entry.contentRect.width);
  if (width === pageWidth) return;
  const first = pageWidth === 0;
  pageWidth = width;
  if (first) return;
  for (const phrase of sheet.phrases) phrase.geo = null;
  show(shown, { quiet: true });
}).observe(page);

// ---- рух до фрази ----

let glideId = 0;

// Власне прокручування замість scrollIntoView: браузерне «плавно» триває скільки
// йому заманеться, а тут рух мусить устигати за клавішею, яку тиснуть раз у раз.
function glide(top, instant) {
  cancelAnimationFrame(glideId);
  const from = window.scrollY;
  if (instant || calm.matches || Math.abs(top - from) < 2) {
    window.scrollTo(0, top);
    return;
  }
  const began = performance.now();
  const step = (now) => {
    const k = Math.min(1, (now - began) / GLIDE_MS);
    window.scrollTo(0, from + (top - from) * (1 - (1 - k) ** 3));
    if (k < 1) glideId = requestAnimationFrame(step);
  };
  glideId = requestAnimationFrame(step);
}

// Ставить фразу (чи будь-що з полем el) на рівень очей — у верхню третину видимої
// частини аркуша.
export function bring(phrase, instant = false) {
  // На вузькому екрані верх вікна зайнятий відео — рівень очей рахуємо під ним.
  const stick = narrow.matches ? stage.offsetHeight : 0;
  const room = window.innerHeight - stick - bar.offsetHeight;
  const top = phrase.el.getBoundingClientRect().top + window.scrollY - stick - room * EYE_LEVEL;
  glide(Math.max(0, top), instant);
}

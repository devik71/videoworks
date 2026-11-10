// Сторінка викреслювача: стан, клавіші й збереження. Рахує logic.js, аркуш показує
// sheet.js, жести ловить brush.js, грає й глушить player.js — тут їхнє зведення докупи.
// Правила глушіння живуть на сервері: відрізки й лічильники сторінка тільки показує.

import { applyGesture, phraseAt, History, Outbox } from "./logic.js";
import { attach } from "./brush.js";
import { createPlayer } from "./player.js";
import { sheet, build, show, showManual, bring, quietState, make, clock } from "./sheet.js";

const EDGE = 0.15; // с: межі слів від ASR неточні, тож фразу граємо із запасом
const LINGER = 0.12; // с: слово ще підсвічене, поки не почалось наступне
const SAVE_DELAY = 500;
const RETRY_DELAY = 3000;

const $ = (id) => document.getElementById(id);
const els = {
  sheet: $("sheet"), video: $("video"), bar: $("bar"), count: $("count"), saved: $("saved"),
  banner: $("banner"), mode: $("mode"), play: $("play"), clock: $("clock"), undo: $("undo"),
  redo: $("redo"), hint: $("hint"), stage: document.querySelector(".stage"),
};

// Єдиний стан сторінки. struck і manual — намір людини; spans і counts — те, що
// з нього вивів сервер, і сторінка їх ніколи не перераховує сама.
const state = {
  data: null, struck: new Set(), manual: [], spans: [], counts: null, stamp: "",
  active: 0, mode: "phrase",
};

let history = null;
let outbox = null;
let viewKey = "";
let nowWord = -1;
let clockSecond = -1;
let videoBroken = false;

// ---- дрібниці ----

// Приватний режим може кинути вже на самому зверненні до сховища.
const storage = (() => {
  try {
    window.localStorage.getItem("strike");
    return window.localStorage;
  } catch {
    return { getItem: () => null, setItem() {}, removeItem() {} };
  }
})();

function plural(n, one, few, many) {
  const unit = n % 10;
  const tens = n % 100;
  if (unit === 1 && tens !== 11) return one;
  if (unit >= 2 && unit <= 4 && (tens < 12 || tens > 14)) return few;
  return many;
}

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
  };
}

const marks = (snap) => JSON.stringify([snap.struck, snap.manual]);

// ---- плашки й рядок стану ----

// Стійкі повідомлення: кожне під своїм ключем, щоб біда з відео не затирала біду
// із записом. Те саме повідомлення вдруге не перемальовуємо — фокус на кнопці цілий.
const notes = new Map();

function note(key, text, { code = "", actions = [] } = {}) {
  const had = notes.get(key);
  if (had && had.text === text && had.code === code) return;
  notes.set(key, { text, code, actions });
  showNotes();
}

function unnote(key) {
  if (notes.delete(key)) showNotes();
}

function showNotes() {
  els.banner.replaceChildren();
  for (const { text, code, actions } of notes.values()) {
    const row = make("div", "note");
    const words = make("p", "", text);
    if (code) words.append(" ", make("code", "", code));
    row.append(words);
    for (const [label, run] of actions) {
      const button = make("button", "pill", label);
      button.type = "button";
      button.addEventListener("click", run);
      row.append(button);
    }
    els.banner.append(row);
  }
  els.banner.hidden = notes.size === 0;
}

function say(text, bad = false) {
  els.saved.textContent = text;
  els.saved.classList.toggle("bad", bad);
}

function showCounts() {
  const { words, manual, seconds: total } = state.counts;
  const parts = [];
  if (words) parts.push(`${words} ${plural(words, "слово", "слова", "слів")}`);
  if (manual) parts.push(`${manual} ${plural(manual, "місце", "місця", "місць")} без слів`);
  els.count.textContent = parts.length
    ? `викреслено ${parts.join(" і ")} · ${seconds(total)}`
    : "ще нічого не викреслено";
}

function syncSteps() {
  els.undo.disabled = !history.canUndo;
  els.redo.disabled = !history.canRedo;
}

// ---- активна фраза й памʼять ----

function setActive(index, { instant = false } = {}) {
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
  bring(phrases[next], instant);
}

function remember() {
  try {
    storage.setItem(viewKey, JSON.stringify({ active: state.active, mode: state.mode }));
  } catch {
    // без пам'яті сторінка просто почне з початку
  }
}

function recall() {
  try {
    const found = JSON.parse(storage.getItem(viewKey));
    if (found && typeof found === "object") return found;
  } catch {
    // зіпсований запис — те саме, що жодного
  }
  return {};
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
    const { wordEls, phrases } = sheet;
    // Слова теж відсортовані за початком і мають .start — пошук той самий, що для фраз.
    let word = stopped ? -1 : phraseAt(state.data.words, t);
    if (word >= 0 && (!wordEls[word] || t > state.data.words[word].end + LINGER)) word = -1;
    if (word !== nowWord) {
      if (nowWord >= 0) wordEls[nowWord].classList.remove("now");
      if (word >= 0) wordEls[word].classList.add("now");
      nowWord = word;
    }
    if (state.mode !== "flow" || stopped || !player.playing) return;
    const at = phraseAt(phrases, t);
    // Розгін перед щойно обраною фразою ще лежить у попередній — це не привід вертатись.
    const lead = phrases[state.active].start - t;
    if (at >= 0 && at !== state.active && !(lead > 0 && lead <= 2 * EDGE)) setActive(at);
  },
  onChange(playing) {
    document.body.classList.toggle("playing", playing);
    els.play.setAttribute("aria-label", playing ? "Спинити" : "Грати");
  },
});

function seekPhrase(index) {
  if (!sheet.phrases.length) return;
  setActive(index);
  const phrase = sheet.phrases[state.active];
  player.play(phrase.start - EDGE, state.mode === "phrase" ? phrase.end + EDGE : null);
}

function toggle() {
  if (!sheet.phrases.length) return;
  if (player.playing) player.pause();
  else if (state.mode === "phrase") seekPhrase(state.active);
  else player.play(null);
}

function setMode(mode) {
  state.mode = mode === "flow" ? "flow" : "phrase";
  for (const button of els.mode.querySelectorAll("button")) {
    button.setAttribute("aria-checked", String(button.dataset.mode === state.mode));
  }
  if (sheet.phrases.length && player.playing) {
    player.until(state.mode === "phrase" ? sheet.phrases[state.active].end + EDGE : null);
  }
  remember();
}

// ---- правки й історія ----

// Єдиний вхід для будь-якої зміни позначок: жест, клавіша X, скасування, відновлення
// після обриву. record === false — крок уже лежить в історії (це саме скасування).
// Повертає, чи щось справді змінилось.
function commit(next, { record = true } = {}) {
  const before = snapshot();
  state.struck = new Set(next.struck);
  state.manual = next.manual.map((pair) => [pair[0], pair[1]]);
  const now = snapshot();
  show(state.struck);
  if (marks(before) === marks(now)) return false;
  if (JSON.stringify(before.manual) !== JSON.stringify(now.manual)) showManual(state.manual);
  if (record) history.push(now);
  syncSteps();
  els.hint.textContent = "";
  outbox.set(now);
  schedule(SAVE_DELAY);
  return true;
}

function undo() {
  const back = history.undo();
  if (back) commit(back, { record: false });
}

function redo() {
  const forth = history.redo();
  if (forth) commit(forth, { record: false });
}

function strikeActive() {
  const phrase = sheet.phrases[state.active];
  if (!phrase) return;
  const all = [];
  for (let i = phrase.first; i < phrase.first + phrase.count; i++) {
    if (sheet.wordEls[i]) all.push(i);
  }
  const mode = all.every((i) => state.struck.has(i)) ? "erase" : "strike";
  commit({ struck: [...applyGesture(state.struck, all, mode)], manual: state.manual });
}

// ---- збереження ----

let saveTimer = 0;
let sending = false;

function schedule(delay) {
  clearTimeout(saveTimer);
  saveTimer = setTimeout(save, delay);
}

async function save({ force = false } = {}) {
  clearTimeout(saveTimer);
  // Поки відповідь у дорозі, нова правка чекає в черзі: після відповіді її дошлють.
  if (sending || !outbox.pending) return;
  const sent = snapshot();
  sending = true;
  say("зберігаю…");
  let status = 0;
  let reply = null;
  try {
    const response = await fetch("/strikes", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        words: sent.struck, manual: sent.manual, stamp: state.stamp,
        transcript: sent.transcript, force,
      }),
    });
    status = response.status;
    reply = await response.json();
  } catch {
    reply = null; // сервер мовчить або відповів не JSON — для людини це одне й те саме
  }
  sending = false;

  if (reply && reply.saved) {
    state.spans = reply.spans;
    state.counts = reply.counts;
    state.stamp = reply.stamp;
    outbox.markSent(sent);
    unnote("save");
    showCounts();
    if (outbox.pending) schedule(0);
    else say("збережено");
  } else if (reply && reply.moved) {
    say("не збережено", true);
    const reread = ["перечитати", () => load({ drop: true })];
    if (force) {
      // Примусовий запис сервер відхиляє лише тоді, коли змінилась сама розшифровка.
      note("save", "Розшифровку перезібрали, і ці позначки до неї вже не пасують. "
        + "Перечитай — останні мазки доведеться повторити.", { actions: [reread] });
    } else {
      note("save", "Файл позначок змінили деінде, поки ця сторінка була відкрита.", {
        actions: [reread, ["записати моє", () => save({ force: true })]],
      });
    }
  } else if (status >= 400 && status < 500) {
    // Повтор тут нічого не дасть: сервер відмовив саме цьому вмісту.
    say("не збережено", true);
    note("save", `Не вдалося зберегти: ${(reply && reply.error) || status}.`, {
      actions: [["перечитати", () => load({ drop: true })]],
    });
  } else {
    say("не збережено", true);
    note("save", "Не вдалося зберегти — пробую ще. Позначки лежать у цьому браузері й не загубляться.");
    schedule(RETRY_DELAY);
  }
}

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
async function load({ drop = false } = {}) {
  clearTimeout(saveTimer);
  let data;
  try {
    const response = await fetch("/state.json", { cache: "no-store" });
    if (!response.ok) throw new Error(String(response.status));
    data = await response.json();
  } catch {
    if (!state.data) {
      quietState("Не вдалося прочитати заняття. Перевір, чи ще працює videoworks strike.",
        ["спробувати ще", () => load()]);
    }
    return;
  }

  if (drop && outbox) outbox.markSent(snapshot());
  state.data = data;
  state.struck = new Set(data.struck);
  state.manual = data.manual;
  state.spans = data.spans;
  state.counts = data.counts;
  state.stamp = data.stamp;
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
  unnote("save");
  say("");
  showCounts();
  syncSteps();
  els.clock.textContent = `${clock(0)} / ${clock(data.duration)}`;
  const { unglued } = data;
  els.hint.textContent = unglued > 0
    ? `${unglued} ${plural(unglued, "слово не знайшлось", "слова не знайшлись", "слів не знайшлись")}`
      + " у новому транскрипті — вони стали відрізками без слів"
    : "";

  showVideoNote();

  const view = recall();
  setMode(view.mode);
  if (sheet.phrases.length) {
    setActive(Number.isInteger(view.active) ? view.active : 0, { instant: true });
    if (!player.playing) els.video.currentTime = sheet.phrases[state.active].start;
  }

  if (kept && kept.transcript === data.transcript) {
    // Минулого разу правка не дійшла до файлу: повертаємо її й досилаємо одразу.
    if (commit(cleaned(kept))) save();
    else outbox.markSent(kept);
  } else if (kept) {
    outbox.markSent(kept);
    els.hint.textContent = "Останні незбережені мазки не підійшли до нової розшифровки.";
  }
}

// ---- жести, клавіші, кнопки ----

attach(els.sheet, {
  blank: { has: (i) => sheet.blank.has(i) }, // аркуш можуть перечитати, а пензель один
  struck: () => state.struck,
  wordAt(x, y) {
    const el = document.elementFromPoint(x, y);
    return el && el.dataset && el.dataset.i !== undefined ? Number(el.dataset.i) : null;
  },
  // Жест триває: показуємо, що вийде, але стан не чіпаємо до відпускання.
  onPreview(touched, mode) {
    show(applyGesture(state.struck, touched, mode));
  },
  onGesture(touched, mode) {
    commit({ struck: [...applyGesture(state.struck, touched, mode)], manual: state.manual });
  },
  onCancel() {
    show(state.struck);
  },
  onClick(index) {
    setActive(sheet.phraseOf[index]);
    const phrase = sheet.phrases[state.active];
    const from = Math.max(state.data.words[index].start, phrase.start) - EDGE;
    player.play(from, state.mode === "phrase" ? phrase.end + EDGE : null);
  },
  onStep() {}, // сюди наступний крок підключає звук пензля
});

document.addEventListener("keydown", (event) => {
  const el = event.target;
  if (!state.data || !(el instanceof HTMLElement)) return;
  if (el.isContentEditable || /^(INPUT|TEXTAREA|SELECT)$/.test(el.tagName)) return;
  // event.code — фізична клавіша: на українській розкладці X і Z стоять там само.
  if (event.ctrlKey || event.metaKey) {
    if (event.altKey) return;
    if (event.code === "KeyZ") {
      event.preventDefault();
      if (event.shiftKey) redo();
      else undo();
    } else if (event.code === "KeyY" && !event.shiftKey) {
      event.preventDefault();
      redo();
    }
    return;
  }
  if (event.altKey) return;
  if (event.code === "Space") {
    if (el instanceof HTMLButtonElement) return; // кнопка сама відповідає на пробіл
    event.preventDefault();
    if (!event.repeat) toggle();
  } else if (event.code === "ArrowRight" || event.code === "ArrowLeft") {
    event.preventDefault();
    seekPhrase(state.active + (event.code === "ArrowRight" ? 1 : -1));
  } else if (event.code === "KeyX" && !event.shiftKey) {
    event.preventDefault();
    if (!event.repeat) strikeActive();
  }
});

els.play.addEventListener("click", toggle);
els.undo.addEventListener("click", undo);
els.redo.addEventListener("click", redo);
els.video.addEventListener("click", toggle);
els.mode.addEventListener("click", (event) => {
  const button = event.target.closest("button[data-mode]");
  if (button) setMode(button.dataset.mode);
});
// Після кліку мишею фокус не лишається на кнопці, інакше пробіл тиснув би її знову,
// а не грав фразу. З клавіатури (detail === 0) фокус не чіпаємо.
els.bar.addEventListener("click", (event) => {
  const button = event.target.closest("button");
  if (button && event.detail > 0) button.blur();
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
  if (document.hidden && outbox && outbox.pending) save();
});

els.video.src = "/video";
load();

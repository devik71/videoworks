// Слова сторінки до людини: стійкі плашки над аркушем і два місця в рядку стану —
// «збережено чи ні» та «що відбувається просто зараз». Про позначки, відео й
// сервер цей файл нічого не знає: йому дають текст і кажуть, куди.

const banner = document.getElementById("banner");
const saved = document.getElementById("saved");
const hint = document.getElementById("hint");
const bar = document.getElementById("bar");

// Кожна плашка під своїм ключем, щоб біда з відео не затирала біду із записом.
// Те саме повідомлення вдруге не перемальовуємо — фокус на кнопці цілий.
const notes = new Map();

function showNotes() {
  banner.replaceChildren();
  for (const { text, code, actions } of notes.values()) {
    const row = document.createElement("div");
    row.className = "note";
    const words = document.createElement("p");
    words.textContent = text;
    if (code) {
      const command = document.createElement("code");
      command.textContent = code;
      words.append(" ", command);
    }
    row.append(words);
    for (const [label, run] of actions) {
      const button = document.createElement("button");
      button.type = "button";
      button.className = "pill";
      button.textContent = label;
      button.addEventListener("click", run);
      row.append(button);
    }
    banner.append(row);
  }
  banner.hidden = notes.size === 0;
}

export function note(key, text, { code = "", actions = [] } = {}) {
  const had = notes.get(key);
  if (had && had.text === text && had.code === code) return;
  notes.set(key, { text, code, actions });
  showNotes();
}

export function unnote(key) {
  if (notes.delete(key)) showNotes();
}

// Звістка, яку людина має прочитати, але з якою нічого не треба робити: висить на
// видноті за будь-якої ширини вікна, доки її не закриють, і не зникає від жесту.
export function tell(key, text) {
  note(key, text, { actions: [["гаразд", () => unnote(key)]] });
}

// Стан запису. У вузькому вікні звичні стани стискаються до значка (це робить
// стиль за data-state), а біда лишається словами й відсуває лічильник.
export function say(text, bad = false) {
  saved.textContent = text;
  saved.classList.toggle("bad", bad);
  saved.dataset.state = bad ? "bad" : text === "збережено" ? "ok" : "busy";
  bar.classList.toggle("unsaved", bad);
}

// Що відбувається просто зараз: росте позначка, йде перевірка, підказка до клавіші.
// ms — скільки тримати; без нього текст висить, доки його не змінять.
let liveTimer = 0;

export function live(text, ms = 0) {
  clearTimeout(liveTimer);
  hint.textContent = text;
  // У вузькому вікні рядок один: поки є що сказати, лічильник поступається місцем.
  bar.classList.toggle("busy", text !== "");
  if (text && ms) liveTimer = setTimeout(() => live(""), ms);
}

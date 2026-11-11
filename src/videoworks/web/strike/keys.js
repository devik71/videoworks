// Клавіші сторінки: яка фізична клавіша що означає. Самі дії — в app.js; тут лише
// розбір натискання, щоб таблицю з довідки було видно в одному місці.

import { NUDGE } from "./logic.js";

// act:
//   ready() -> boolean      аркуш завантажено
//   painting() -> boolean   пензель зараз на папері
//   helpShown() -> boolean, help(open: boolean)
//   undo(), redo(), toggle(), go(dir), strikePhrase(), holdStart(), holdEnd(),
//   mark(dir), sound(), nudge(edge, delta), escape()
export function bindKeys(act) {
  document.addEventListener("keydown", (event) => {
    const el = event.target;
    if (!act.ready() || !(el instanceof HTMLElement)) return;
    if (el.isContentEditable || /^(INPUT|TEXTAREA|SELECT)$/.test(el.tagName)) return;
    // Одна рука малює — друга нічого не зсуває під нею: ні історії, ні фраз, ні
    // позначок. Пробіл і стрілки ще й не мають прокрутити сторінку самі.
    if (act.painting()) {
      if (/^(Space|Arrow|Page|Home|End)/.test(event.code)) event.preventDefault();
      return;
    }
    // «?» на українській розкладці стоїть на іншій клавіші, тож ловимо і знак, і місце.
    const asks = !event.ctrlKey && !event.metaKey && !event.altKey
      && (event.key === "?" || (event.code === "Slash" && event.shiftKey));
    if (act.helpShown()) {
      // Під карткою клавіші не працюють: викреслити щось наосліп було б прикро.
      // Esc закриває сам браузер.
      if (asks && !event.repeat) {
        event.preventDefault();
        act.help(false);
      }
      return;
    }
    // event.code — фізична клавіша: на українській розкладці X і Z стоять там само.
    if (event.ctrlKey || event.metaKey) {
      if (event.altKey) return;
      if (event.code === "KeyZ") {
        event.preventDefault();
        if (event.shiftKey) act.redo();
        else act.undo();
      } else if (event.code === "KeyY" && !event.shiftKey) {
        event.preventDefault();
        act.redo();
      }
      return;
    }
    if (event.altKey) return;
    if (asks) {
      event.preventDefault();
      if (!event.repeat) act.help(true);
    } else if (event.code === "Space") {
      if (el instanceof HTMLButtonElement) return; // кнопка сама відповідає на пробіл
      event.preventDefault();
      if (!event.repeat) act.toggle();
    } else if (event.code === "ArrowRight" || event.code === "ArrowLeft") {
      event.preventDefault();
      act.go(event.code === "ArrowRight" ? 1 : -1);
    } else if (event.code === "KeyX" && !event.shiftKey) {
      event.preventDefault();
      if (!event.repeat) act.strikePhrase();
    } else if (event.code === "KeyB" && !event.shiftKey) {
      event.preventDefault();
      if (!event.repeat) act.holdStart(); // автоповтор клавіші — це те саме утримання
    } else if (event.code === "KeyN") {
      event.preventDefault();
      if (!event.repeat) act.mark(event.shiftKey ? -1 : 1);
    } else if (event.code === "KeyM" && !event.shiftKey) {
      event.preventDefault();
      if (!event.repeat) act.sound();
    } else if (event.code === "BracketLeft" || event.code === "BracketRight") {
      event.preventDefault();
      // Без Shift рухається початок, із Shift — кінець; [ — раніше, ] — пізніше.
      const delta = event.code === "BracketLeft" ? -NUDGE : NUDGE;
      act.nudge(event.shiftKey ? "end" : "start", delta);
    } else if (event.code === "Escape") {
      act.escape();
    }
  });

  // Відпускання ловимо завжди, навіть під довідкою: інакше позначка росла б далі.
  document.addEventListener("keyup", (event) => {
    if (event.code === "KeyB") act.holdEnd();
  });
}

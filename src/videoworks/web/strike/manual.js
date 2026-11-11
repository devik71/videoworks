// Позначки без слів: те, чого немає в тексті й чого пензлем не дістати. Тримаєш
// клавішу чи кнопку, поки грає відео, — виходить відрізок; потім його краї можна
// посунути. Як саме він записується й показується, вирішує app.js через `app`.

import { manualFromHold, nudge, REACTION } from "./logic.js";

const BURST_MS = 1000; // підсування, що йдуть щільніше, — один крок скасування
const EDGE_AROUND = 0.3; // с з кожного боку від посунутого краю, щоб почути його

// app:
//   state            стан сторінки: manual, blot (номер відрізка під клавішами), data
//   player, video    відтворення
//   button           кнопка біля відео — те саме, що клавіша B
//   begin()          перед утриманням: спинити те, що грало саме
//   shown()          показати на аркуші щойно зроблену позначку
//   commit(next, options) -> boolean
//   resume()         повернути зупинку в кінці фрази, якщо режим «по фразах»
//   live(text, ms)   рядок стану: що відбувається просто зараз
//   strip(span)      смуга на мапі, що росте
//   ping(kind)       звук
export function createManual(app) {
  const { state, player, video, button } = app;
  let hold = null; // { from } — час відео, коли натиснули
  let lastNudge = null; // { at, span } — щоб наступне підсування приєднати до кроку
  let edgeToPlay = null;

  const byStart = (a, b) => a[0] - b[0] || a[1] - b[1];
  const same = (a, b) => a[0] === b[0] && a[1] === b[1];

  function shown(on) {
    document.body.classList.toggle("holding", on);
    if (!on) {
      app.strip(null);
      app.live("");
    }
  }

  // Натискання має сенс лише тоді, коли відео грає: відрізок міряється його часом.
  function press() {
    if (hold || !state.data) return;
    if (!player.playing) {
      app.live("спершу запусти відео, тоді тримай", 2200);
      return;
    }
    app.begin();
    hold = { from: player.time };
    // Фраза може скінчитись, а рука ще тримає: зупинка в кінці фрази зачекає.
    player.until(null);
    shown(true);
    tick(player.time);
  }

  function tick(t) {
    if (!hold) return;
    const from = Math.max(hold.from - REACTION, 0);
    app.strip([from, Math.max(t, from)]);
    app.live(`♪ ${Math.max(t - from, 0).toFixed(1)} с`);
  }

  function release() {
    if (!hold) return;
    const { from } = hold;
    hold = null;
    shown(false);
    const span = manualFromHold(from, player.time, state.data.duration);
    app.resume();
    if (!span) return;
    const manual = [...state.manual, span].sort(byStart);
    // Щойно зроблена позначка одразу під клавішами [ і ]: край зазвичай хочеться
    // підправити, поки памʼятаєш, де він мав бути.
    state.blot = manual.findIndex((pair) => same(pair, span));
    if (!app.commit({ struck: [...state.struck], manual })) return;
    app.ping("splat");
    app.shown();
  }

  // Перемотали посеред утримання — відрізок «звідти досюди» вже не має сенсу.
  function cancel() {
    if (!hold) return;
    hold = null;
    shown(false);
    app.resume();
  }

  video.addEventListener("seeking", cancel);
  // Відео спинилось само чи пробілом — позначка закінчується там, де воно стало.
  video.addEventListener("pause", release);
  window.addEventListener("blur", release);

  button.addEventListener("pointerdown", (event) => {
    if (event.pointerType === "mouse" && event.button !== 0) return;
    event.preventDefault(); // фокус лишається, де був: пробіл і далі грає
    button.setPointerCapture(event.pointerId);
    press();
  });
  for (const type of ["pointerup", "pointercancel", "lostpointercapture"]) {
    button.addEventListener(type, release);
  }
  // Довгий дотик на телефоні відкрив би меню замість позначки.
  button.addEventListener("contextmenu", (event) => event.preventDefault());

  // Зсуває край відрізка, що під клавішами. Після запису цей край програється:
  // чути, що саме тепер мовчить.
  function shift(edge, delta) {
    const before = state.manual[state.blot];
    if (!before) return false;
    const after = nudge(before, edge, delta, state.data.duration);
    if (same(before, after)) return true; // далі нікуди — це теж відповідь
    const now = performance.now();
    const amend = lastNudge !== null && now - lastNudge.at < BURST_MS
      && same(lastNudge.span, before);
    const manual = state.manual.map((pair, m) => (m === state.blot ? after : pair)).sort(byStart);
    state.blot = manual.findIndex((pair) => same(pair, after));
    lastNudge = { at: now, span: after };
    edgeToPlay = edge === "start" ? after[0] : after[1];
    app.commit({ struck: [...state.struck], manual }, { amend, hurry: true });
    return true;
  }

  return {
    press,
    release,
    cancel,
    tick,
    shift,
    get holding() {
      return hold !== null;
    },
    // Сервер прийняв позначки, глушіння вже нове — тепер край можна послухати.
    saved() {
      if (edgeToPlay === null) return;
      const edge = edgeToPlay;
      edgeToPlay = null;
      if (state.blot >= 0) player.play(edge - EDGE_AROUND, edge + EDGE_AROUND);
    },
  };
}

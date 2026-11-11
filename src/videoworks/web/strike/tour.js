// Обхід позначок: стрибок до сусідньої клавішею й «перевірити викреслене» —
// останній прохід перед ефіром, де кожне глушіння звучить разом із тим, що довкола.
// Позначки тут — це відрізки глушіння від сервера: саме їх почує глядач.

import { reviewStops, CONTEXT } from "./logic.js";

const SLACK = 0.05; // с: головка стає на початок із похибкою кадру

// app:
//   state            стан сторінки: spans, data
//   player           відтворення
//   reveal(span)     показати на аркуші місце цієї позначки
//   live(text, ms)   рядок стану
//   reviewing(on)    вигляд кнопки перевірки
export function createTour(app) {
  const { state, player } = app;
  let review = null; // { stops, at }
  let last = -1; // позначка, до якої стрибнули востаннє
  let roaming = false; // грає позначка, і аркуш має їхати за головкою

  function around(span) {
    return [Math.max(span[0] - CONTEXT, 0), Math.min(span[1] + CONTEXT, state.data.duration)];
  }

  function quit() {
    roaming = false;
    if (!review) return false;
    review = null;
    app.reviewing(false);
    app.live("");
    return true;
  }

  // Наступна чи попередня позначка, по колу. Повторне натискання йде далі від тієї,
  // яку щойно слухали, а не від головки: вона ще стоїть у контексті перед позначкою.
  function step(dir) {
    const marks = state.spans;
    quit();
    if (!marks.length) {
      app.live("позначок ще немає", 1800);
      return;
    }
    const t = player.time;
    let index;
    const was = marks[last] ? around(marks[last]) : null;
    if (was && t >= was[0] - SLACK && t <= was[1] + SLACK) index = last + dir;
    else if (dir > 0) index = marks.findIndex((span) => span[0] > t);
    else {
      index = -1;
      marks.forEach((span, i) => {
        if (span[1] <= t) index = i;
      });
    }
    if (index >= marks.length || (index < 0 && dir > 0)) index = 0;
    else if (index < 0) index = marks.length - 1;
    last = index;
    const [from, to] = around(marks[index]);
    app.reveal(marks[index]);
    app.live(`позначка ${index + 1} з ${marks.length}`, 2200);
    roaming = true;
    player.play(from, to);
  }

  function go() {
    const [from, to] = review.stops[review.at];
    app.live(`перевірка · ${review.at + 1} з ${review.stops.length}`);
    const first = state.spans.find((span) => span[1] > from);
    if (first) app.reveal(first);
    player.play(from, to);
  }

  return {
    step,
    // Кнопка працює як перемикач: друге натискання спиняє.
    toggleReview() {
      if (quit()) {
        player.pause();
        return;
      }
      const stops = reviewStops(state.spans, state.data.duration);
      if (!stops.length) return;
      review = { stops, at: 0 };
      app.reviewing(true);
      go();
    },
    // Людина взялась за щось інше — обхід мовчки поступається. Повертає, чи йшла
    // перевірка.
    quit,
    // Плеєр дограв до замовленої зупинки.
    arrived() {
      roaming = false;
      if (!review) return;
      review.at += 1;
      if (review.at < review.stops.length) {
        go();
        return;
      }
      quit();
      app.live("перевірено — усе прослухано", 2600);
    },
    get reviewing() {
      return review !== null;
    },
    get following() {
      return roaming || review !== null;
    },
  };
}

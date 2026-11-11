// Відтворення й глушіння. Про фрази, аркуш і збереження цей файл не знає: йому дають
// відео, спосіб дізнатись поточні відрізки глушіння й просять грати звідси досюди.

import { isMuted } from "./logic.js";

const LOOKAHEAD = 0.03; // с: рампа мусить початись до межі, а не на ній
const RAMP = 0.01; // с: стала часу; за три такі гучність уже на місці — це й є ~30 мс

export function createPlayer(video, { spans, onTick, onChange }) {
  let context = null;
  let gain = null;
  let plain = false; // WebAudio немає — глушимо самим відео, без рампи
  let hushed = null;
  let stopAt = null;
  let frameId = 0;

  // Аудіограф збираємо на перший запуск, а не на завантаження: до жесту людини
  // браузер тримає контекст приспаним, і відео, заведене крізь нього, мовчало б.
  function wire() {
    if (context || plain) return;
    try {
      const Context = window.AudioContext || window.webkitAudioContext;
      const made = new Context();
      const node = made.createGain();
      made.createMediaElementSource(video).connect(node).connect(made.destination);
      context = made;
      gain = node;
      // Заглушений елемент віддає в граф тишу, і жодна гучність її вже не підніме.
      video.muted = false;
      snap(video.currentTime);
    } catch {
      plain = true;
    }
  }

  // Гучність одразу, без рампи: відтворення починається з місця t, і якщо воно
  // у викресленому, до першого кадру не має просочитись ані звуку. Рампа потрібна
  // межі, на яку головка наїжджає сама; стрибок головки — це й так розрив звуку.
  function snap(t) {
    if (!gain) return;
    hushed = isMuted(spans(), t + LOOKAHEAD);
    gain.gain.cancelScheduledValues(context.currentTime);
    // Пряме присвоєння діє негайно навіть тоді, коли контекст іще не прокинувся.
    gain.gain.value = hushed ? 0 : 1;
  }

  function hush(t) {
    // До першого запуску графа ще немає, а чим глушити — невідомо: чіпати
    // video.muted можна лише тоді, коли WebAudio справді недоступний.
    if (!gain && !plain) return;
    const want = isMuted(spans(), t + LOOKAHEAD);
    if (want === hushed) return;
    hushed = want;
    if (gain) gain.gain.setTargetAtTime(want ? 0 : 1, context.currentTime, RAMP);
    else video.muted = want;
  }

  function frame() {
    frameId = 0;
    const t = video.currentTime;
    if (stopAt !== null && t >= stopAt) {
      stopAt = null;
      video.pause();
      onTick(t, true);
      return;
    }
    hush(t);
    onTick(t, false);
    if (!video.paused && !video.ended) frameId = requestAnimationFrame(frame);
  }

  video.addEventListener("play", () => {
    if (!frameId) frameId = requestAnimationFrame(frame);
    onChange(true);
  });
  video.addEventListener("pause", () => onChange(false));
  video.addEventListener("ended", () => {
    onChange(false);
    // Запис скінчився раніше за замовлену зупинку — для того, хто чекав, це вона й є.
    if (stopAt !== null) {
      stopAt = null;
      onTick(video.currentTime, true);
    }
  });
  // Перемотування на паузі кадрів не дає, а слово під головкою показати треба.
  video.addEventListener("seeked", () => {
    if (!video.paused) return;
    hush(video.currentTime);
    onTick(video.currentTime, false);
  });

  function wake() {
    wire();
    if (context && context.state === "suspended") context.resume().catch(() => {});
  }

  return {
    // Той самий контекст для звуку пензля: другого на сторінці не буде. Кликати лише
    // з жесту людини — раніше браузер звуку не дасть. null — WebAudio недоступний.
    audio() {
      wake();
      return context;
    },
    // from === null — грати з того місця, де стоїть; to === null — не спинятись.
    play(from, to = null) {
      wake();
      stopAt = to;
      if (from !== null) {
        video.currentTime = Math.max(0, from);
        snap(Math.max(0, from));
      }
      const started = video.play();
      // Відео, якого браузер не вміє, відхиляє обіцянку; про це вже каже плашка.
      if (started) started.catch(() => {});
    },
    pause() {
      video.pause();
    },
    // Перемикання режиму посеред фрази: знімає або ставить зупинку, не чіпаючи звук.
    until(to) {
      stopAt = to;
    },
    // Перемотування без зміни «грає чи стоїть»: мапа тягне головку.
    seek(t, to = null) {
      stopAt = to;
      video.currentTime = Math.max(0, t);
      // Як і play: стрибок у викреслене глушимо одразу, а не з наступного кадру.
      snap(Math.max(0, t));
    },
    get time() {
      return video.currentTime;
    },
    get playing() {
      return !video.paused && !video.ended;
    },
  };
}

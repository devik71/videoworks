// Мапа заняття: увесь запис згори донизу однією вузькою смугою. Де говорять —
// бліді риски, де викреслено — червоне, де головка — чорнильна лінія. Клік або
// протягування перемотує. Про аркуш і збереження цей файл не знає нічого.

const MIN_MARK = 2; // px: коротка позначка на сорокахвилинній мапі інакше зникла б
const HEAD = 2; // px: товщина лінії головки
const MARGIN = 5; // px: риски фраз відступають від країв, червоне йде на всю ширину

export function createMap(canvas, { onSeek }) {
  const g = canvas.getContext("2d");
  let duration = 0;
  let phrases = [];
  let spans = [];
  let live = null; // відрізок, що росте, поки тримають клавішу
  let headAt = 0;
  let headPx = -1;
  let width = 0;
  let height = 0;
  let ratio = 1;
  let paints = { tick: "", mark: "", head: "" };

  // Кольори живуть у стилях сторінки; тут їх лише читають, щоб мапа не розійшлася
  // з аркушем, коли токени зміняться.
  function readPaints() {
    const css = getComputedStyle(document.documentElement);
    const token = (name) => css.getPropertyValue(name).trim();
    paints = { tick: token("--hair"), mark: token("--brush"), head: token("--ink") };
  }

  const yOf = (t) => (duration > 0 ? (Math.min(Math.max(t, 0), duration) / duration) * height : 0);
  // Краї прямокутників — на цілих пікселях екрана, інакше риски розмиваються.
  const snap = (v) => Math.round(v * ratio) / ratio;

  function band(from, to, least, left, right) {
    let top = snap(yOf(from));
    let tall = snap(yOf(to)) - top;
    if (tall < least) {
      top = Math.min(Math.max(snap(top - (least - tall) / 2), 0), Math.max(height - least, 0));
      tall = least;
    }
    g.fillRect(left, top, right - left, tall);
  }

  function draw() {
    if (!g || !width || !height) return;
    g.setTransform(ratio, 0, 0, ratio, 0, 0);
    g.clearRect(0, 0, width, height);
    if (duration <= 0) return;
    g.fillStyle = paints.tick;
    // Риска не коротша за піксель екрана, але й не довша: паузи між фразами лишаються.
    for (const phrase of phrases) {
      band(phrase.start, phrase.end, 1 / ratio, MARGIN, width - MARGIN + 1);
    }
    g.fillStyle = paints.mark;
    for (const [from, to] of spans) band(from, to, MIN_MARK, 1, width);
    if (live) band(live[0], live[1], MIN_MARK, 1, width);
    g.fillStyle = paints.head;
    headPx = Math.round(yOf(headAt));
    g.fillRect(0, Math.min(Math.max(headPx - HEAD / 2, 0), height - HEAD), width, HEAD);
  }

  function fit() {
    const box = canvas.getBoundingClientRect();
    const scale = window.devicePixelRatio || 1;
    const w = Math.round(box.width);
    const h = Math.round(box.height);
    if (w === width && h === height && scale === ratio) return;
    width = w;
    height = h;
    ratio = scale;
    canvas.width = Math.round(w * scale);
    canvas.height = Math.round(h * scale);
    readPaints();
    draw();
  }

  // ---- перемотування ----

  let dragging = null;

  function seekTo(event) {
    const box = canvas.getBoundingClientRect();
    if (duration <= 0 || box.height <= 0) return;
    const part = Math.min(Math.max((event.clientY - box.top) / box.height, 0), 1);
    onSeek(part * duration);
  }

  canvas.addEventListener("pointerdown", (event) => {
    if (event.pointerType === "mouse" && event.button !== 0) return;
    dragging = event.pointerId;
    canvas.setPointerCapture(event.pointerId);
    seekTo(event);
  });
  canvas.addEventListener("pointermove", (event) => {
    if (event.pointerId === dragging) seekTo(event);
  });
  const drop = (event) => {
    if (event.pointerId === dragging) dragging = null;
  };
  canvas.addEventListener("pointerup", drop);
  canvas.addEventListener("pointercancel", drop);
  canvas.addEventListener("lostpointercapture", drop);

  new ResizeObserver(fit).observe(canvas);
  // Масштаб сторінки міняє щільність пікселів, а розмір у CSS лишає тим самим.
  window.addEventListener("resize", fit);
  readPaints();
  fit();

  return {
    // Нове заняття: фрази й тривалість. Викликається раз на завантаження.
    load(data) {
      duration = data.duration;
      phrases = data.phrases;
      draw();
    },
    // Перемальовує лише тоді, коли відрізки справді інші.
    marks(next) {
      if (JSON.stringify(next) === JSON.stringify(spans)) return;
      spans = next.map((pair) => [pair[0], pair[1]]);
      draw();
    },
    hold(span) {
      if (!span && !live) return;
      live = span;
      draw();
    },
    // Головка рухається весь час, а мапа — лише коли лінія зсунулась на піксель.
    head(t) {
      headAt = t;
      if (Math.round(yOf(t)) !== headPx) draw();
    },
  };
}

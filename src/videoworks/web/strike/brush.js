// Пензель: жест вказівника над словами й фарба поверх них. Тут немає ні стану
// сторінки, ні мережі, ні плеєра — лише «якого слова торкнулись» і «що намалювати».

import { crosses, fill, gestureMode, isStroke, seed, strokePath } from "./logic.js";

const NS = "http://www.w3.org/2000/svg";
const OVERHANG = 5; // px: мазок трохи виходить за краї слова, як справжній
const SINK = 0.06; // частка висоти слова
const GROW_MS = 160;
const FADE_MS = 140;
const SPLASH_MS = 300;
const SLACK = 12; // px: запас обрізання, щоб рвані краї не зрізало рамкою

const calm = window.matchMedia("(prefers-reduced-motion: reduce)");

// Що зараз намальовано в кожному <svg>: за цим видно, який мазок новий, який
// подовжився, а який зник, — і лише вони рухаються.
const painted = new WeakMap();

// ---- жест ----

// handlers:
//   wordAt(x, y) -> number | null   слово під точкою вікна
//   rectOf(i) -> DOMRect | null     де слово зараз у вікні: за цим видно, чи пройшов
//                                   над ним шлях вказівника
//   blotAt(x, y) -> number | null   відрізок без слів під точкою (необовʼязково)
//   struck() -> Set                 викреслене до жесту: за ним визначається режим
//   blank: Set                      порожні слова, яких пензель не бере
//   onPreview(touched, mode, blots) жест триває: показати, що вийде
//   onGesture(touched, mode, blots) жест завершено: записати
//   onCancel()                      жест зірвався: повернути, як було
//   onClick(index)                  натиск на слові без руху
//   onBlotClick(m)                  натиск без руху на рядку без слів
//   onDown()                        натиск на аркуші, ще до пошуку слова під ним
//   onLive(on)                      жест почався / скінчився (будь-чим): поки він
//                                   живий, під рукою ніщо не має рухатись
//   onStep(index, speed, step, mode)  пензель зайшов на нове слово: швидкість у
//                                   px/мс і котре це слово в жесті, від нуля
// blots — номери відрізків без слів, які жест стирає. Такий рядок — теж викреслене:
// жест, що почався на ньому, стирає, а мазок-викреслення, що йде повз, його не чіпає.
// Повертає { live } — чи триває жест просто зараз.
export function attach(root, handlers) {
  const blank = handlers.blank || new Set();
  const blotAt = handlers.blotAt || (() => null);
  let gesture = null;
  let swallow = -1; // час жесту, після якого клік під вказівником уже не клік

  root.addEventListener("pointerdown", (event) => {
    if (gesture || (event.pointerType === "mouse" && event.button !== 0)) return;
    // До того, як дивитись, що під вказівником: слухач може закінчити свої справи,
    // які зсувають рядки (закрити позначку без слів, яку ще тримали).
    if (handlers.onDown) handlers.onDown();
    const index = handlers.wordAt(event.clientX, event.clientY);
    const blot = index === null ? blotAt(event.clientX, event.clientY) : null;
    if (index === null && blot === null) return;
    if (handlers.onLive) handlers.onLive(true);
    gesture = {
      pointer: event.pointerId,
      x: event.clientX,
      y: event.clientY,
      first: index,
      blot,
      last: index,
      touched: null, // стає списком, коли рух уже точно мазок, а не клік
      seen: new Set(),
      blots: [],
      mode: null,
      at: event.timeStamp,
      px: event.clientX,
      py: event.clientY,
    };
    // Вказівник може вийти за аркуш — жест від цього не повинен обірватись, і його
    // кінець мусить дійти сюди, де б кнопку не відпустили. Тому захоплюємо завжди,
    // а клік по рядку без слів розпізнаємо самі.
    root.setPointerCapture(event.pointerId);
  });

  root.addEventListener("pointermove", (event) => {
    const g = gesture;
    if (!g || event.pointerId !== g.pointer) return;
    // Миша рухається без натиснутої кнопки — відпускання ми не побачили (воно
    // сталось поза вікном). Жест, що жив би далі, стирав би слова самим наведенням.
    if (event.pointerType === "mouse" && event.buttons === 0) {
      end(false, event.timeStamp);
      return;
    }
    const index = handlers.wordAt(event.clientX, event.clientY);
    const fromX = g.px;
    const fromY = g.py;
    const elapsed = Math.max(event.timeStamp - g.at, 1);
    const speed = Math.hypot(event.clientX - g.px, event.clientY - g.py) / elapsed;
    g.at = event.timeStamp;
    g.px = event.clientX;
    g.py = event.clientY;

    let changed = false;
    const take = (i) => {
      if (g.seen.has(i)) return;
      g.seen.add(i);
      g.touched.push(i);
      changed = true;
      if (handlers.onStep) handlers.onStep(i, speed, g.touched.length - 1, g.mode);
    };
    const wipe = (m) => {
      if (m === null || g.blots.includes(m)) return;
      g.blots.push(m);
      changed = true;
    };

    if (!g.touched) {
      const crossed = index !== null && index !== g.first;
      if (!isStroke(event.clientX - g.x, event.clientY - g.y, crossed)) return;
      g.mode = g.first === null ? "erase" : gestureMode(handlers.struck(), g.first);
      g.touched = [];
      root.classList.add("brushing");
      if (g.first === null) wipe(g.blot);
      else take(g.first);
    }
    if (index !== null && index !== g.last) {
      // Між попереднім словом і цим добираємо лише ті, над якими пройшов шлях
      // вказівника від минулої події до цієї (правило — у logic.js біля fill).
      const crossed = (i) => crosses(
        fromX, fromY, event.clientX, event.clientY, handlers.rectOf ? handlers.rectOf(i) : null,
      );
      for (const i of fill(g.last, index, blank, crossed)) take(i);
      g.last = index;
    } else if (index !== null) take(index); // рука рухається по слову — воно під пензлем
    if (g.mode === "erase" && index === null) wipe(blotAt(event.clientX, event.clientY));
    if (changed) handlers.onPreview(g.touched, g.mode, g.blots);
  });

  // Кінець жесту, хоч чим він скінчився. Скасований нічого не записує й повертає
  // аркуш до того, що було.
  function end(completed, stamp) {
    const g = gesture;
    if (!g) return;
    gesture = null;
    root.classList.remove("brushing");
    if (root.hasPointerCapture(g.pointer)) root.releasePointerCapture(g.pointer);
    // Спершу «жест скінчився»: клік і запис рухають аркуш, а це вже можна.
    if (handlers.onLive) handlers.onLive(false);
    if (!g.touched) {
      if (!completed) return;
      // Натиск поглинули ми, тож власний клік кнопки рядка не має спрацювати вдруге.
      swallow = stamp;
      if (g.first !== null) handlers.onClick(g.first);
      else if (handlers.onBlotClick) handlers.onBlotClick(g.blot);
    } else if (completed) {
      swallow = stamp;
      handlers.onGesture(g.touched, g.mode, g.blots);
    } else {
      handlers.onCancel();
    }
  }

  const finish = (event, completed) => {
    if (gesture && event.pointerId === gesture.pointer) end(completed, event.timeStamp);
  };

  // Аркуш поїхав під вказівником (коліщатко, клавіша, чужа прокрутка) або вікно
  // змінило розмір: слово під пензлем тепер інше, і «від попереднього до цього»
  // вже не шлях руки. Беремо за попереднє те, що під вказівником зараз, нічого не
  // добираючи; далі жест іде звідси.
  const reanchor = () => {
    if (gesture && gesture.touched) gesture.last = handlers.wordAt(gesture.px, gesture.py);
  };
  window.addEventListener("scroll", reanchor, true);
  window.addEventListener("resize", reanchor);
  // Вікно втратило увагу чи вкладку сховали — відпускання може сюди не дійти.
  window.addEventListener("blur", () => end(false, 0));
  document.addEventListener("visibilitychange", () => {
    if (document.hidden) end(false, 0);
  });

  root.addEventListener("pointerup", (event) => finish(event, true));
  root.addEventListener("pointercancel", (event) => finish(event, false));
  // Захоплення можуть забрати без жодної іншої події (вкладку сховали, системний
  // жест): без цього лишився б жест-привид, що чекає відпускання, якого не буде.
  // Після звичайного відпускання жесту вже немає, і ця подія нічого не робить.
  root.addEventListener("lostpointercapture", (event) => finish(event, false));
  // Мазок, що скінчився над кнопкою, — не натискання на неї.
  root.addEventListener("click", (event) => {
    if (swallow < 0 || event.timeStamp - swallow > 80 || event.detail === 0) return;
    swallow = -1;
    event.stopPropagation();
    event.preventDefault();
  }, true);

  return {
    get live() {
      return gesture !== null;
    },
  };
}

// ---- фарба ----

function inset(left, right) {
  return `inset(${-SLACK}px ${right}px ${-SLACK}px ${left}px)`;
}

// Відкриває мазок від уже намальованого краю: hidden px спершу сховано з того боку,
// куди пензель щойно дійшов.
function grow(el, hidden, fromRight) {
  const from = fromRight ? inset(hidden, -SLACK) : inset(-SLACK, hidden);
  const open = inset(-SLACK, -SLACK);
  el.animate({ clipPath: [from, open] }, { duration: GROW_MS, easing: "ease-out" });
}

function fade(el) {
  const gone = el.animate({ opacity: [1, 0] }, { duration: FADE_MS, easing: "ease-out" });
  gone.onfinish = () => el.remove();
  gone.oncancel = () => el.remove();
}

// Число 0..1, те саме для того самого слова й номера: бризки не стрибають між показами.
function chance(index, k) {
  return seed(index * 31 + k * 7919 + 17) / 4294967296;
}

// Три краплі там, де пензель торкнувся паперу.
function splash(svg, box, index) {
  for (let k = 0; k < 3; k++) {
    const drop = document.createElementNS(NS, "circle");
    const cy = box.y + box.h * (0.15 + 0.7 * chance(index, k + 3));
    drop.setAttribute("cx", String(Math.round(box.x - 2 + chance(index, k) * 10)));
    drop.setAttribute("cy", String(Math.round(cy)));
    drop.setAttribute("r", (1.4 + 2 * chance(index, k + 6)).toFixed(1));
    svg.append(drop);
    const dx = -(6 + 14 * chance(index, k + 9));
    const dy = (chance(index, k + 12) - 0.5) * 30;
    const flown = drop.animate(
      [
        { transform: "translate(0, 0) scale(0.6)", opacity: 0.9 },
        { transform: `translate(${dx.toFixed(1)}px, ${dy.toFixed(1)}px) scale(1)`, opacity: 0 },
      ],
      { duration: SPLASH_MS, easing: "cubic-bezier(0.2, 0.7, 0.3, 1)" },
    );
    flown.onfinish = () => drop.remove();
    flown.oncancel = () => drop.remove();
  }
}

function shape(el, box, first) {
  el.setAttribute("d", strokePath(box.w, box.h, seed(first)));
  el.setAttribute("transform", `translate(${box.x} ${box.y})`);
}

// Рамка мазка над словами first..last одного рядка, у координатах <svg>.
function boxOf(first, last, rectOf) {
  const a = rectOf(first);
  const b = rectOf(last);
  // Рамка рядка вища за літери згори, тож мазок опускаємо до середини малих літер.
  const y = a.y + a.h * SINK;
  return { x: a.x - OVERHANG, y, w: b.x + b.w - a.x + 2 * OVERHANG, h: a.h };
}

// Приводить <svg> фрази до списку мазків runs = [[перше, останнє слово], …].
// rectOf(i) -> { x, y, w, h } слова в координатах <svg>. quiet — без руху: так
// малюється те, що вже було викреслено до того, як фраза потрапила на очі.
export function paint(svg, runs, rectOf, { quiet = false } = {}) {
  const still = quiet || calm.matches;
  const before = painted.get(svg) || [];
  const kept = new Set();
  const after = [];
  const had = new Set();
  for (const s of before) for (let i = s.first; i <= s.last; i++) had.add(i);

  for (const [first, last] of runs) {
    const box = boxOf(first, last, rectOf);
    // Той самий початок — мазок подовжили чи вкоротили з хвоста; той самий
    // кінець — з голови. Інакше це новий мазок.
    const was =
      before.find((s) => !kept.has(s) && s.first === first) ||
      before.find((s) => !kept.has(s) && s.last === last);
    let el;
    if (was) {
      kept.add(was);
      el = was.el;
    } else {
      el = document.createElementNS(NS, "path");
      svg.append(el);
    }
    const same =
      was && was.first === first && was.last === last &&
      was.box.x === box.x && was.box.y === box.y && was.box.w === box.w && was.box.h === box.h;
    if (!same) {
      shape(el, box, first);
      if (!still) {
        if (!was) {
          // Залишок мазка, якому стерли середину, — не нова фарба: він уже лежав.
          let fresh = false;
          for (let i = first; i <= last; i++) fresh = fresh || !had.has(i);
          if (fresh) {
            grow(el, box.w + SLACK, false);
            splash(svg, box, first);
          }
        } else if (was.box.y === box.y && box.w > was.box.w) {
          grow(el, box.w - was.box.w, was.first !== first);
        }
      }
    }
    after.push({ first, last, box, el });
  }

  const covered = new Set();
  for (const [first, last] of runs) for (let i = first; i <= last; i++) covered.add(i);

  for (const was of before) {
    if (!kept.has(was)) {
      // Мазок зник цілком: тане він сам, тієї ж форми.
      if (still) was.el.remove();
      else fade(was.el);
      continue;
    }
    if (still) continue;
    // Мазок вкоротили: на звільнених словах лишається слід, що тане.
    let from = null;
    for (let i = was.first; i <= was.last + 1; i++) {
      const bare = i <= was.last && !covered.has(i);
      if (bare && from === null) from = i;
      if (!bare && from !== null) {
        const ghost = document.createElementNS(NS, "path");
        shape(ghost, boxOf(from, i - 1, rectOf), from);
        svg.append(ghost);
        fade(ghost);
        from = null;
      }
    }
  }

  painted.set(svg, after);
}

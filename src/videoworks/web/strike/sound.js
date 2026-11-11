// Звук пензля: три короткі голоси, зібрані з шуму й осциляторів, без жодного файлу.
// Це жарт інструмента, і він мусить лишатись приємним на двохсотому мазку: тихо,
// коротко, з мʼяким початком і спадом без клацання. Контекст дає плеєр — другий
// тут не створюється; сам файл нічого не знає ні про жести, ні про сторінку.

import { squeakPitch } from "./logic.js";

const MASTER = 0.18; // помітно тихіше за відео
const VOICES = 4; // більше одночасних голосів — це вже стіна звуку, зайві відкидаємо
const ATTACK = 0.006; // с: коротший початок чути як клацання
const FLOOR = 0.001; // до нуля експонента не доходить; це вже нечутно
const LAND = 0.004; // с: останній відтинок спаду — лінійно в нуль, щоб кінець не клацнув
const SQUEAK = 0.09;
const POP = 0.12;
const SPLAT = 0.15;
const STAGGER = 0.03; // с: швидка миша бере кілька слів за подію — граємо їх драбинкою
const BACKLOG = 0.12; // с: далі драбинка відстала б від руки, такі писки пропускаємо

export function createSound(ctx) {
  let out = null;
  let noise = null;
  let enabled = true;
  let lastSqueak = -1;
  let playing = []; // коли замовкне кожен голос, що ще звучить або чекає черги

  try {
    out = ctx.createGain();
    out.gain.value = MASTER;
    // Повз гучність відео: на викресленому воно мовчить, а пензель має бути чутно.
    out.connect(ctx.destination);
    noise = ctx.createBuffer(1, Math.round(ctx.sampleRate * 0.5), ctx.sampleRate);
    const samples = noise.getChannelData(0);
    for (let i = 0; i < samples.length; i++) samples[i] = Math.random() * 2 - 1;
  } catch {
    out = null; // звуку не буде, сторінка працює мовчки
  }

  // Займає місце для голосу або відмовляє, коли їх уже забагато.
  function claim(at, length) {
    const now = ctx.currentTime;
    playing = playing.filter((end) => end > now);
    if (playing.filter((end) => end > at).length >= VOICES) return false;
    playing.push(at + length);
    return true;
  }

  // Огинаюча голосу: лінійний підйом, експоненційний спад і посадка рівно в нуль
  // на останній мілісекунді тривалості — так не клацає ні початок, ні кінець.
  function envelope(at, length, peak) {
    const node = ctx.createGain();
    node.gain.setValueAtTime(0, at);
    node.gain.linearRampToValueAtTime(peak, at + ATTACK);
    node.gain.exponentialRampToValueAtTime(FLOOR, at + length - LAND);
    node.gain.linearRampToValueAtTime(0, at + length);
    node.connect(out);
    return node;
  }

  function hiss(at, length, filter, level, into) {
    const source = ctx.createBufferSource();
    const gain = ctx.createGain();
    source.buffer = noise;
    gain.gain.value = level;
    source.connect(filter).connect(gain).connect(into);
    // Щоразу інший шматок шуму: два писки поспіль не звучать як один семпл.
    source.start(at, Math.random() * 0.35, length);
  }

  function tone(type, at, length, from, to, level, into) {
    const osc = ctx.createOscillator();
    const gain = ctx.createGain();
    osc.type = type;
    osc.frequency.setValueAtTime(from, at);
    if (to !== from) osc.frequency.exponentialRampToValueAtTime(to, at + length);
    gain.gain.value = level;
    osc.connect(gain).connect(into);
    osc.start(at);
    osc.stop(at + length);
  }

  // Жоден голос не має права зламати жест: будь-яка помилка звуку — це тиша.
  function safely(make) {
    if (!out || !enabled) return;
    try {
      make();
    } catch {
      // без звуку
    }
  }

  return {
    get enabled() {
      return enabled;
    },
    set enabled(value) {
      enabled = Boolean(value);
    },

    // Маркерний писк: вузька смуга шуму й трикутний тон, що разом ковзають угору.
    // step — котре це слово в жесті, speed — швидкість пензля у px/мс.
    squeak(step, speed) {
      safely(() => {
        const now = ctx.currentTime;
        const at = Math.max(now, lastSqueak + STAGGER);
        if (at - now > BACKLOG || !claim(at, SQUEAK)) return;
        lastSqueak = at;
        // Трохи вбік від точної ноти: той самий мазок двічі не звучить однаково.
        const pitch = squeakPitch(step, speed) * (1 + (Math.random() - 0.5) * 0.024);
        const voice = envelope(at, SQUEAK, 1);
        const band = ctx.createBiquadFilter();
        band.type = "bandpass";
        band.Q.value = 9;
        band.frequency.setValueAtTime(pitch, at);
        band.frequency.exponentialRampToValueAtTime(pitch * 1.35, at + SQUEAK);
        // Висоту несе тон; шум лише дає йому шорсткість маркера по паперу.
        hiss(at, SQUEAK, band, 3, voice);
        tone("triangle", at, SQUEAK, pitch, pitch * 1.35, 0.55, voice);
      });
    },

    // Стирання: мʼякий «чпок», що падає вниз.
    pop() {
      safely(() => {
        const at = ctx.currentTime;
        if (!claim(at, POP)) return;
        const lean = 1 + (Math.random() - 0.5) * 0.08;
        tone("sine", at, POP, 420 * lean, 140 * lean, 1, envelope(at, POP, 0.9));
      });
    },

    // Ціла фраза одним рухом: жирний «шмяк» — глухий шум, що осідає, і низ під ним.
    splat() {
      safely(() => {
        const at = ctx.currentTime;
        if (!claim(at, SPLAT)) return;
        const voice = envelope(at, SPLAT, 1);
        const low = ctx.createBiquadFilter();
        low.type = "lowpass";
        low.frequency.setValueAtTime(900, at);
        low.frequency.exponentialRampToValueAtTime(200, at + SPLAT);
        hiss(at, SPLAT, low, 1.5, voice);
        tone("sine", at, SPLAT, 110, 110, 0.9, voice);
      });
    },
  };
}

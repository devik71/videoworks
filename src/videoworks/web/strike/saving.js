// Запис позначок на сервер: черга з однієї останньої правки, повтор після збою,
// «файл змінили деінде». Що саме записується й що робити з відповіддю на аркуші,
// каже app.js; тут — лише порядок запитів і слова про те, чим вони скінчились.

import { note, unnote, say } from "./notes.js";

const RETRY_DELAY = 3000;

// app:
//   state         стан сторінки: сюди лягають spans, counts і stamp із відповіді
//   snapshot()    поточні позначки
//   outbox()      черга незбереженого
//   applied()     відрізки й лічильники оновились — показати
//   settled()     записано все, черга порожня
//   reread()      людина обрала «перечитати»
export function createSaver(app) {
  const { state } = app;
  let timer = 0;
  let sending = null; // запит, що зараз у дорозі; ніколи не відхиляється
  let forceNext = false; // «записати моє» натиснули, поки попередній запит ще йшов
  let epoch = 0; // номер завантаження: відповідь на запит із минулого — чужа

  function schedule(delay) {
    clearTimeout(timer);
    timer = setTimeout(save, delay);
  }

  async function post(sent, force) {
    try {
      const response = await fetch("/strikes", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          words: sent.struck, manual: sent.manual, stamp: state.stamp,
          transcript: sent.transcript, force,
        }),
      });
      return { status: response.status, reply: await response.json() };
    } catch {
      // сервер мовчить або відповів не JSON — для людини це одне й те саме
      return { status: 0, reply: null };
    }
  }

  async function save({ force = false } = {}) {
    clearTimeout(timer);
    // Поки відповідь у дорозі, нова правка чекає в черзі: після відповіді її дошлють.
    // Примусовий запис теж чекає, але не губиться: натиснута кнопка мусить спрацювати.
    if (sending) {
      forceNext = forceNext || force;
      return;
    }
    const outbox = app.outbox();
    if (!outbox || !outbox.pending) return;
    const sent = app.snapshot();
    const born = epoch;
    say("зберігаю…");
    sending = post(sent, force);
    const { status, reply } = await sending;
    sending = null;
    // Аркуш тим часом перечитали: ця відповідь — про стан, якого на сторінці вже
    // немає. Вона не має права ні підняти плашку, ні підмінити відрізки й відбиток.
    if (born !== epoch) return;
    const again = forceNext;
    forceNext = false;

    if (reply && reply.saved) {
      state.spans = reply.spans;
      state.counts = reply.counts;
      state.stamp = reply.stamp;
      outbox.markSent(sent);
      unnote("save");
      app.applied();
      if (outbox.pending) {
        // Новіша правка в черзі зроблена поверх щойно записаного — оновлюємо їй
        // основу, інакше власний запис сторінки виглядав би чужою зміною файлу.
        outbox.set(app.snapshot());
        schedule(0);
      } else {
        say("збережено");
        app.settled();
      }
    } else if (reply && reply.moved) {
      say("не збережено", true);
      const reread = ["перечитати", app.reread];
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
        actions: [["перечитати", app.reread]],
      });
    } else {
      say("не збережено", true);
      // Сервер міг назвати причину (диск, права, зайнятий файл) — її варто побачити.
      const why = reply && typeof reply.error === "string" ? `: ${reply.error}` : "";
      note("save", `Не вдалося зберегти${why} — пробую ще. `
        + "Позначки лежать у цьому браузері й не загубляться.");
      schedule(RETRY_DELAY);
    }
    if (again) save({ force: true });
  }

  return {
    schedule,
    save,
    // Аркуш перечитують: відкладений запис скасовано, відповіді на старі запити
    // більше нікого не цікавлять. Запит, що вже в дорозі, спершу мусить дійти —
    // інакше він ліг би у файл після читання, і свіжий аркуш мав би застарілий
    // відбиток.
    async restart() {
      clearTimeout(timer);
      epoch += 1;
      forceNext = false;
      if (sending) await sending;
    },
  };
}

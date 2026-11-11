// Довідка по клавішах: паперова картка поверх аркуша. Текст картки лежить у
// розмітці; тут лише відкрити, закрити, втримати фокус усередині й повернути його.

const FOCUSABLE = "button:not(:disabled), [href], [tabindex]:not([tabindex='-1'])";

export function createHelp(card) {
  let back = null; // куди повернути фокус після закриття
  let keep = true; // чи повертати його взагалі

  // <dialog>, відкритий як модальний, сам робить решту сторінки недосяжною, закриває
  // на Esc і малює тло. Табуляцію замикаємо ще й самі: браузер випускає її з
  // картки в свій рядок адреси, а тут вона має ходити колом.
  card.addEventListener("keydown", (event) => {
    if (event.key !== "Tab") return;
    const stops = [...card.querySelectorAll(FOCUSABLE)];
    if (!stops.length) {
      event.preventDefault();
      return;
    }
    const first = stops[0];
    const last = stops[stops.length - 1];
    const at = document.activeElement;
    if (event.shiftKey && (at === first || !card.contains(at))) {
      event.preventDefault();
      last.focus();
    } else if (!event.shiftKey && (at === last || !card.contains(at))) {
      event.preventDefault();
      first.focus();
    }
  });

  // Клік по тлу влучає в сам <dialog>: уміст картки лежить у вкладеному блоці.
  card.addEventListener("click", (event) => {
    if (event.target === card || event.target.closest("[data-close]")) card.close();
  });

  card.addEventListener("close", () => {
    const target = back;
    back = null;
    if (keep && target && target.isConnected) target.focus({ preventScroll: true });
    // Після кліку мишею фокус не лишаємо на кнопці: пробіл має грати, а не тиснути її.
    else if (document.activeElement instanceof HTMLElement) document.activeElement.blur();
  });

  const api = {
    get shown() {
      return card.open;
    },
    // restore === false — відкрили мишею, повертати фокус на кнопку не треба.
    open({ restore = true } = {}) {
      if (card.open || typeof card.showModal !== "function") return;
      back = document.activeElement;
      keep = restore;
      card.showModal();
      const first = card.querySelector(FOCUSABLE);
      if (first) first.focus({ preventScroll: true });
    },
    close() {
      if (card.open) card.close();
    },
    toggle(options) {
      if (card.open) api.close();
      else api.open(options);
    },
  };
  return api;
}

// Памʼять сторінки між відвідинами: localStorage, який не має права її зламати.
// Приватний режим кидає вже на самому зверненні до сховища, повна квота — на записі.

export const storage = (() => {
  try {
    window.localStorage.getItem("strike");
    return window.localStorage;
  } catch {
    return { getItem: () => null, setItem() {}, removeItem() {} };
  }
})();

// Не вдалося запамʼятати — сторінка просто почне наступного разу з початку.
export function keep(key, value) {
  try {
    storage.setItem(key, value);
  } catch {
    // лишається лише памʼять вкладки
  }
}

// Зіпсований запис — те саме, що жодного.
export function recallObject(key) {
  try {
    const found = JSON.parse(storage.getItem(key));
    if (found && typeof found === "object") return found;
  } catch {
    // нижче
  }
  return {};
}

(() => {
  "use strict";
  const $ = id => document.getElementById(id);
  const state = {categories: [], busy: false};
  const errors = {
    catalog_category_exists: "Категория с таким названием уже существует.",
    catalog_category_not_found: "Категория больше не найдена. Обновите список.",
    catalog_category_changed: "Категорию уже изменили после открытия формы. Обновите список и повторите.",
    invalid_catalog_text: "Введите название категории длиной до 200 символов.",
    invalid_catalog_revision: "Данные формы устарели. Обновите список.",
    invalid_catalog_request: "Проверьте название категории.",
  };
  const node = (tag, text, className) => { const value = document.createElement(tag); if (text !== undefined) value.textContent = text; if (className) value.className = className; return value; };
  const number = value => Number(value).toLocaleString("ru-RU");
  function notice(text, error = false) { $("notice").textContent = text; $("notice").className = "notice" + (error ? " error" : ""); $("notice").hidden = !text; }
  function csrf() { const item = document.cookie.split("; ").find(value => value.startsWith("__Host-texnikach_monitoring_csrf=")); return item ? decodeURIComponent(item.slice(item.indexOf("=") + 1)) : ""; }
  function operationId() {
    if (globalThis.crypto && typeof globalThis.crypto.randomUUID === "function") return globalThis.crypto.randomUUID();
    return "xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx".replace(/[xy]/g, value => { const random = Math.random() * 16 | 0; return (value === "x" ? random : (random & 3 | 8)).toString(16); });
  }
  async function api(path, body) {
    const options = {credentials: "same-origin", headers: {Accept: "application/json"}};
    if (body !== undefined) Object.assign(options, {method: "POST", body: JSON.stringify(body), headers: {...options.headers, "Content-Type": "application/json", "X-CSRF-Token": csrf(), "Idempotency-Key": operationId()}});
    const response = await fetch("/monitoring/api/prices/admin/entry/" + path, options);
    let data = {}; try { data = await response.json(); } catch {}
    if (!response.ok) {
      if (response.status === 401) { $("login-panel").hidden = false; $("workspace").hidden = true; }
      const code = typeof data.detail === "object" ? data.detail.code : data.detail;
      const error = new Error(response.status === 401 ? "Войдите в портал и вернитесь к категориям." : response.status === 403 ? "Нет доступа или сессия устарела." : errors[code] || "Не удалось выполнить операцию.");
      error.code = code; throw error;
    }
    return data;
  }
  const button = (text, action, primary = false) => { const value = node("button", text, "button " + (primary ? "primary" : "secondary")); value.type = "button"; value.addEventListener("click", action); return value; };
  function modal(title, content, actions) { $("dialog-title").textContent = title; $("dialog-body").replaceChildren(...content); $("dialog-actions").replaceChildren(...actions); $("dialog").showModal(); }
  function render() {
    const words = $("search").value.toLocaleLowerCase("ru").trim().split(/\s+/).filter(Boolean);
    const filtered = state.categories.filter(item => words.every(word => `${item.name} ${item.category_id}`.toLocaleLowerCase("ru").includes(word)));
    $("categories").replaceChildren(...filtered.map(item => {
      const card = node("article", undefined, "category-card" + (item.variant_count ? "" : " empty-category"));
      const details = node("div"); details.append(node("h3", item.name), node("p", `${number(item.model_count)} моделей · ${number(item.variant_count)} вариантов`));
      card.append(details, node("small", `ID ${item.category_id}`, "category-id"), button("Переименовать", () => openForm(item)));
      return card;
    }));
    $("found-count").textContent = `${number(filtered.length)} категорий`;
    $("empty").hidden = !!filtered.length;
  }
  function openForm(category = null) {
    const editing = !!category;
    const form = node("div", undefined, "category-form");
    const label = node("label", editing ? "Новое название" : "Название категории");
    const input = node("input"); input.type = "text"; input.maxLength = 200; input.autocomplete = "off"; input.value = category ? category.name : ""; input.placeholder = "Например: Часы бренда Garmin";
    const note = node("p", editing ? `ID ${category.category_id} и все цены сохранятся. Новое название будет применено к ${number(category.variant_count)} вариантам.` : "Категория получит постоянный ID и сразу станет доступна при добавлении моделей.", "form-note");
    form.append(label, input, note);
    const save = button(editing ? "Сохранить название" : "Создать категорию", async () => {
      const name = input.value.trim();
      if (!name) { note.textContent = "Введите название категории."; note.classList.add("danger"); input.focus(); return; }
      if (editing && name === category.name) { $("dialog").close(); return; }
      save.disabled = true; note.textContent = editing ? "Переименовываем категорию…" : "Создаём категорию…"; note.classList.remove("danger");
      try {
        const result = await api(editing ? `categories/${category.category_id}` : "categories", editing ? {name, expected_revision: category.revision} : {name});
        $("dialog").close(); await load(); $("search").value = result.category.name; render();
        notice(editing ? `Категория переименована. Обновлено вариантов: ${number(result.updated_variant_count)}.` : `Категория «${result.category.name}» создана с ID ${result.category.category_id}.`);
      } catch (error) { note.textContent = error.message; note.classList.add("danger"); save.disabled = false; }
    }, true);
    modal(editing ? "Переименовать категорию" : "Добавить категорию", [form], [button("Отмена", () => $("dialog").close()), save]);
    input.focus(); input.select();
  }
  async function load() {
    if (state.busy) return; state.busy = true; notice("Загружаем категории…");
    try {
      const data = await api("categories"); state.categories = data.categories;
      $("category-count").textContent = number(state.categories.length);
      $("model-count").textContent = number(state.categories.reduce((sum, item) => sum + item.model_count, 0));
      $("variant-count").textContent = number(state.categories.reduce((sum, item) => sum + item.variant_count, 0));
      $("empty-count").textContent = number(state.categories.filter(item => !item.variant_count).length);
      $("workspace").hidden = false; $("login-panel").hidden = true; $("add-category").hidden = false; render(); notice("");
    } catch (error) { if (error.message) notice(error.message, true); }
    finally { state.busy = false; }
  }
  $("add-category").addEventListener("click", () => openForm());
  $("refresh").addEventListener("click", load);
  $("search").addEventListener("input", render);
  $("search").addEventListener("keydown", event => { if (event.key === "Escape") { event.currentTarget.value = ""; render(); } });
  document.addEventListener("keydown", event => { if (event.key === "/" && document.activeElement !== $("search") && !$("dialog").open) { event.preventDefault(); $("search").focus(); } });
  $("close-dialog").addEventListener("click", () => $("dialog").close());
  load();
})();

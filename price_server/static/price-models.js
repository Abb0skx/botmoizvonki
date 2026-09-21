(() => {
  "use strict";
  const $ = id => document.getElementById(id);
  const state = {models: [], categories: [], drafts: [], busy: false};
  const errors = {
    catalog_model_not_found: "Модель больше не найдена. Обновите список.",
    catalog_model_changed: "Модель уже изменили после открытия формы. Обновите данные и повторите.",
    catalog_variants_changed: "Состав вариантов изменился. Обновите данные.",
    catalog_product_exists: "Такой вариант уже существует в каталоге.",
    duplicate_catalog_variant: "Память и цвет не должны повторяться.",
    invalid_catalog_variants: "Добавьте от 1 до 100 вариантов.",
    invalid_catalog_request: "Проверьте категорию, название и варианты.",
    catalog_category_exists: "Такая категория уже есть — выберите её из списка.",
    model_draft_not_found: "Черновик уже обработан.",
  };
  const node = (tag, text, className) => { const value = document.createElement(tag); if (text !== undefined) value.textContent = text; if (className) value.className = className; return value; };
  const number = value => Number(value).toLocaleString("ru-RU");
  function notice(text, error = false) { $("notice").textContent = text; $("notice").className = "notice" + (error ? " error" : ""); $("notice").hidden = !text; }
  function csrf() { const item = document.cookie.split("; ").find(value => value.startsWith("__Host-texnikach_monitoring_csrf=")); return item ? decodeURIComponent(item.slice(item.indexOf("=") + 1)) : ""; }
  function operationId() {
    if (globalThis.crypto && typeof globalThis.crypto.randomUUID === "function") return globalThis.crypto.randomUUID();
    return "xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx".replace(/[xy]/g, value => {
      const random = Math.random() * 16 | 0;
      return (value === "x" ? random : (random & 3 | 8)).toString(16);
    });
  }
  async function api(path, body, operation = operationId()) {
    const options = {credentials: "same-origin", headers: {Accept: "application/json"}};
    if (body !== undefined) Object.assign(options, {method: "POST", body: JSON.stringify(body), headers: {...options.headers, "Content-Type": "application/json", "X-CSRF-Token": csrf(), "Idempotency-Key": operation}});
    const response = await fetch("/monitoring/api/prices/admin/entry/" + path, options);
    let data = {}; try { data = await response.json(); } catch {}
    if (!response.ok) {
      if (response.status === 401) { $("login-panel").hidden = false; $("workspace").hidden = true; }
      const code = typeof data.detail === "object" ? data.detail.code : data.detail;
      const error = new Error(response.status === 401 ? "Войдите в портал и вернитесь к моделям." : response.status === 403 ? "Нет доступа или сессия устарела." : errors[code] || "Не удалось выполнить операцию.");
      error.code = code; error.status = response.status; throw error;
    }
    return data;
  }
  const button = (text, action, primary = false) => { const value = node("button", text, "button " + (primary ? "primary" : "secondary")); value.type = "button"; value.addEventListener("click", action); return value; };
  function modal(title, content, actions) { $("dialog-title").textContent = title; $("dialog-body").replaceChildren(...content); $("dialog-actions").replaceChildren(...actions); $("dialog").showModal(); }
  function renderModels() {
    const words = $("search").value.toLowerCase().trim().split(/\s+/).filter(Boolean);
    const category = Number($("category").value || 0);
    const filtered = state.models.filter(item => (!category || item.category_id === category) && words.every(word => `${item.model_name} ${item.category_name} ${item.product_ids.join(" ")}`.toLowerCase().includes(word)));
    const cards = filtered.map(item => {
      const card = node("article", undefined, "model-card");
      const title = node("div"); title.append(node("h3", item.model_name), node("small", `${item.variant_count} вариантов`));
      card.append(title, node("p", item.category_name, "category"), node("small", `ID ${item.product_ids[0]}${item.product_ids.length > 1 ? "–" + item.product_ids[item.product_ids.length - 1] : ""}`, "ids"), button("Редактировать", () => editModel(item.anchor_product_id)));
      return card;
    });
    $("models").replaceChildren(...cards); $("found-count").textContent = `${number(filtered.length)} моделей`; $("empty").hidden = !!filtered.length;
  }
  function renderDrafts(configured) {
    if (!configured && !state.drafts.length) { $("drafts").replaceChildren(node("p", "Price-бот пока не подключён к группе Model Yegish. Добавьте его администратором, чтобы сообщения появлялись здесь.")); return; }
    if (!state.drafts.length) { $("drafts").replaceChildren(node("p", "Новых черновиков нет. Отправьте в Model Yegish список в привычном формате 0 / 1 / 2 / 3.")); return; }
    $("drafts").replaceChildren(...state.drafts.map(draft => {
      const card = node("article", undefined, "draft-card"), info = node("div"), actions = node("div", undefined, "draft-actions");
      if (draft.parsed) {
        const category = draft.parsed.category_name || state.categories.find(item => item.category_id === draft.parsed.category_id)?.name || (draft.parsed.category_id ? `Категория #${draft.parsed.category_id}` : "Категория не выбрана");
        info.append(node("strong", draft.parsed.model_name), node("span", category), node("small", `${draft.parsed.variants.length} вариантов · сообщение ${draft.source_message_id}`)); actions.append(button("Проверить", () => openEditor(draft.parsed, draft.draft_id), true));
      }
      else { info.append(node("strong", "Не удалось разобрать сообщение"), node("small", `Сообщение ${draft.source_message_id}`), node("div", "Нужны строки «Категория:», «Модель:» и варианты «память | цвет».", "draft-error")); }
      actions.append(button("Убрать", () => finishDraft(draft.draft_id, "dismissed"))); card.append(info, actions); return card;
    }));
  }
  function appendVariant(container, item = {}) {
    const row = node("div", undefined, "variant-row " + (item.product_id ? "existing" : "new")); row.dataset.productId = item.product_id || "";
    const id = node("span", item.product_id ? `ID ${item.product_id}` : "Новый", "variant-id");
    const memory = node("input"); memory.placeholder = "Память"; memory.maxLength = 100; memory.value = item.memory || ""; memory.setAttribute("aria-label", "Память");
    const color = node("input"); color.placeholder = "Цвет"; color.maxLength = 150; color.value = item.color || ""; color.setAttribute("aria-label", "Цвет");
    const remove = node("button", "×", "remove"); remove.type = "button"; remove.title = item.product_id ? "Существующий вариант нельзя удалить" : "Убрать новый вариант"; remove.addEventListener("click", () => { if (!item.product_id) row.remove(); });
    row.append(id, memory, color, remove); container.append(row);
  }
  function parseBulk(text) {
    const result = [];
    for (const raw of text.split(/\r?\n/).map(line => line.trim()).filter(Boolean)) {
      const line = raw.replace(/^[•·▪️\-–—]+\s*/, "");
      if (line.split("|").length !== 2) throw new Error("В каждой строке нужен один разделитель | между памятью и цветом.");
      const [memory, colors] = line.split("|").map(value => value.trim());
      for (const color of colors.split(",").map(value => value.trim())) result.push({memory, color});
    }
    return result;
  }
  function openEditor(initial = {}, draftId = null) {
    const editing = !!initial.anchor_product_id, wrap = node("div");
    const grid = node("div", undefined, "editor-grid");
    const categoryLabel = node("label"), category = node("select");
    category.append(...(editing ? [] : [new Option("Выберите категорию", "")]), ...state.categories.map(item => new Option(item.name, item.category_id)), ...(editing ? [] : [new Option("＋ Новая категория", "__new__")]));
    const match = initial.category_id || state.categories.find(item => item.name.toLowerCase() === String(initial.category_name || "").toLowerCase())?.category_id;
    if (match) category.value = String(match); categoryLabel.append(node("span", "Категория"), category);
    const newCategoryLabel = node("label"), newCategory = node("input"); newCategory.placeholder = "Название новой категории"; newCategory.maxLength = 200; newCategoryLabel.append(node("span", "Новая категория"), newCategory); newCategoryLabel.hidden = true;
    if (!editing && initial.category_name && !match) { category.value = "__new__"; newCategory.value = initial.category_name; newCategoryLabel.hidden = false; }
    category.addEventListener("change", () => { newCategoryLabel.hidden = category.value !== "__new__"; });
    const modelLabel = node("label"), model = node("input"); model.placeholder = "Название модели"; model.maxLength = 250; model.value = initial.model_name || ""; modelLabel.append(node("span", "Название модели"), model);
    grid.append(categoryLabel, modelLabel, newCategoryLabel); wrap.append(grid);
    const variantsWrap = node("div", undefined, "variant-editor"), head = node("div", undefined, "variant-editor-head"), list = node("div", undefined, "variant-list");
    head.append(node("strong", "Варианты памяти и цвета"), button("＋ Один вариант", () => appendVariant(list))); variantsWrap.append(head, list);
    (initial.variants || [{}]).forEach(item => appendVariant(list, item)); wrap.append(variantsWrap);
    const bulk = node("div", undefined, "bulk-add"), textarea = node("textarea"); textarea.placeholder = "256 GB | Black, Silver\n512 GB | Black";
    const bulkStatus = node("p", "Можно вставить несколько строк. Цвета через запятую автоматически станут отдельными вариантами.", "form-note");
    bulk.append(node("strong", "Быстро добавить списком"), textarea, bulkStatus, button("Добавить строки", () => { try { const values = parseBulk(textarea.value); values.forEach(item => appendVariant(list, item)); textarea.value = ""; bulkStatus.textContent = `Добавлено вариантов: ${values.length}`; bulkStatus.classList.remove("danger"); } catch (error) { bulkStatus.textContent = error.message; bulkStatus.classList.add("danger"); } })); wrap.append(bulk);
    const status = node("p", editing ? "Существующие ID и цены сохранятся. Новые варианты появятся у всех поставщиков без цены." : "После создания модель появится у всех поставщиков без цены.", "form-note"); wrap.append(status);
    const save = button(editing ? "Сохранить модель" : "Создать модель", async () => {
      const rows = [...list.querySelectorAll(".variant-row")].map(row => ({product_id: row.dataset.productId ? Number(row.dataset.productId) : null, memory: row.querySelectorAll("input")[0].value.trim(), color: row.querySelectorAll("input")[1].value.trim()}));
      if (!model.value.trim() || !category.value || !rows.length || rows.length > 500 || (category.value === "__new__" && !newCategory.value.trim())) { status.textContent = "Заполните модель, категорию и от 1 до 500 вариантов."; status.classList.add("danger"); return; }
      save.disabled = true; status.textContent = "Сохраняем…"; status.classList.remove("danger");
      try {
        const payload = editing ? {category_id: Number(category.value), model_name: model.value.trim(), variants: rows.filter(row => row.product_id).map(({product_id, memory, color}) => ({product_id, memory, color})), add_variants: rows.filter(row => !row.product_id).map(({memory, color}) => ({memory, color})), expected_revision: initial.revision} : {category_id: category.value === "__new__" ? null : Number(category.value), new_category_name: category.value === "__new__" ? newCategory.value.trim() : null, model_name: model.value.trim(), variants: rows.map(({memory, color}) => ({memory, color}))};
        const result = await api(editing ? `models/${initial.anchor_product_id}` : "products", payload);
        if (draftId) { try { await api(`model-inbox/${draftId}/applied`, {}); } catch {} }
        $("dialog").close(); await load(); $("search").value = model.value.trim(); renderModels(); notice(editing ? `Модель сохранена. Обновлено вариантов: ${result.updated_count}, добавлено: ${result.created_count}.` : `Модель создана. Добавлено вариантов: ${result.created_count}.`);
      } catch (error) { status.textContent = error.message; status.classList.add("danger"); save.disabled = false; }
    }, true);
    modal(editing ? "Редактировать модель" : "Добавить новую модель", [wrap], [button("Отмена", () => $("dialog").close()), save]); model.focus();
  }
  async function editModel(productId) { try { notice("Загружаем модель…"); const detail = await api("models/" + productId); notice(""); openEditor(detail); } catch (error) { notice(error.message, true); } }
  async function finishDraft(draftId, action) { try { await api(`model-inbox/${draftId}/${action}`, {}); await loadDrafts(); } catch (error) { notice(error.message, true); } }
  async function loadDrafts() { const data = await api("model-inbox"); state.drafts = data.drafts; renderDrafts(data.configured); }
  async function load() {
    if (state.busy) return; state.busy = true; notice("Загружаем каталог…");
    try {
      const [models, categories] = await Promise.all([api("models"), api("categories")]); state.models = models.models; state.categories = categories.categories;
      $("model-count").textContent = number(models.model_count); $("variant-count").textContent = number(models.variant_count); $("category-count").textContent = number(state.categories.length);
      const selected = $("category").value; $("category").replaceChildren(new Option("Все категории", ""), ...state.categories.map(item => new Option(item.name, item.category_id))); if ([...$("category").options].some(item => item.value === selected)) $("category").value = selected;
      $("workspace").hidden = false; $("add-model").hidden = false; renderModels(); await loadDrafts(); notice("");
    } catch (error) { notice(error.message, true); } finally { state.busy = false; }
  }
  $("add-model").addEventListener("click", () => openEditor()); $("refresh").addEventListener("click", load); $("search").addEventListener("input", renderModels); $("category").addEventListener("change", renderModels); $("close-dialog").addEventListener("click", () => $("dialog").close());
  document.addEventListener("keydown", event => { if (event.key === "/" && !["INPUT", "SELECT", "TEXTAREA"].includes(document.activeElement.tagName)) { event.preventDefault(); $("search").focus(); } });
  load();
})();

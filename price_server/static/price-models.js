(() => {
  "use strict";
  const $ = id => document.getElementById(id);
  const state = {models: [], categories: [], busy: false, importCategoryId: null, importPreview: null, importText: ""};
  const errors = {
    catalog_model_not_found: "Модель больше не найдена. Обновите список.",
    catalog_model_changed: "Модель уже изменили после открытия формы. Обновите данные и повторите.",
    catalog_variants_changed: "Состав вариантов изменился. Обновите данные.",
    catalog_product_exists: "Такой вариант уже существует в каталоге.",
    duplicate_catalog_variant: "Память и цвет не должны повторяться.",
    invalid_catalog_variants: "Добавьте от 1 до 100 вариантов.",
    invalid_catalog_request: "Проверьте категорию, название и варианты.",
    catalog_category_exists: "Такая категория уже есть — выберите её из списка.",
    catalog_model_exists: "Одна или несколько моделей уже есть в выбранной категории. Отредактируйте существующую модель отдельно.",
    duplicate_catalog_model: "Название модели повторяется в списке.",
    catalog_preview_changed: "Текст или категория изменились после проверки. Подготовьте результат заново.",
    model_draft_numbered_format: "Проверьте формат строк: 0 — категория, 1 — модель, 2 — память/размер, 3 — цвета.",
    model_draft_model_format: "У каждой записи должна быть строка 1 с названием модели.",
    model_draft_category_format: "После 0 укажите прежний номер категории. На сайте он будет проигнорирован.",
    invalid_model_draft: "Вставьте список моделей в указанном формате.",
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
  function matchingCategories(query) {
    const words = query.toLocaleLowerCase("ru").trim().split(/\s+/).filter(Boolean);
    return state.categories.filter(item => words.every(word => item.name.toLocaleLowerCase("ru").includes(word))).slice(0, 12);
  }
  function categoryPicker(initialId = null) {
    const wrap = node("div", undefined, "category-picker"), input = node("input"), suggestions = node("div", undefined, "category-suggestions");
    input.type = "search"; input.autocomplete = "off"; input.placeholder = "Начните вводить категорию…"; suggestions.hidden = true; suggestions.setAttribute("role", "listbox");
    let selected = initialId ? state.categories.find(item => item.category_id === Number(initialId)) || null : null;
    if (selected) input.value = selected.name;
    const render = () => {
      const matches = matchingCategories(input.value);
      suggestions.replaceChildren(...matches.map(item => {
        const option = node("button", item.name); option.type = "button"; option.setAttribute("role", "option");
        option.addEventListener("mousedown", event => { event.preventDefault(); selected = item; input.value = item.name; suggestions.hidden = true; input.setAttribute("aria-expanded", "false"); });
        return option;
      }));
      suggestions.hidden = !matches.length; input.setAttribute("aria-expanded", String(!!matches.length));
    };
    input.addEventListener("input", () => { selected = null; render(); });
    input.addEventListener("focus", render);
    input.addEventListener("blur", () => setTimeout(() => { suggestions.hidden = true; input.setAttribute("aria-expanded", "false"); }, 100));
    wrap.append(input, suggestions);
    return {element: wrap, input, value: () => selected ? selected.category_id : null, select: item => { selected = item; input.value = item.name; }};
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
  function openEditor(initial = {}) {
    const editing = !!initial.anchor_product_id, wrap = node("div");
    const grid = node("div", undefined, "editor-grid");
    const categoryLabel = node("label");
    const match = initial.category_id || state.categories.find(item => item.name.toLowerCase() === String(initial.category_name || "").toLowerCase())?.category_id;
    const category = categoryPicker(match || null); categoryLabel.append(node("span", "Категория"), category.element);
    const modelLabel = node("label"), model = node("input"); model.placeholder = "Название модели"; model.maxLength = 250; model.value = initial.model_name || ""; modelLabel.append(node("span", "Название модели"), model);
    grid.append(categoryLabel, modelLabel); wrap.append(grid);
    const variantsWrap = node("div", undefined, "variant-editor"), head = node("div", undefined, "variant-editor-head"), list = node("div", undefined, "variant-list");
    head.append(node("strong", "Варианты памяти и цвета"), button("＋ Один вариант", () => appendVariant(list))); variantsWrap.append(head, list);
    (initial.variants || [{}]).forEach(item => appendVariant(list, item)); wrap.append(variantsWrap);
    const bulk = node("div", undefined, "bulk-add"), textarea = node("textarea"); textarea.placeholder = "256 GB | Black, Silver\n512 GB | Black";
    const bulkStatus = node("p", "Можно вставить несколько строк. Цвета через запятую автоматически станут отдельными вариантами.", "form-note");
    bulk.append(node("strong", "Быстро добавить списком"), textarea, bulkStatus, button("Добавить строки", () => { try { const values = parseBulk(textarea.value); values.forEach(item => appendVariant(list, item)); textarea.value = ""; bulkStatus.textContent = `Добавлено вариантов: ${values.length}`; bulkStatus.classList.remove("danger"); } catch (error) { bulkStatus.textContent = error.message; bulkStatus.classList.add("danger"); } })); wrap.append(bulk);
    const status = node("p", editing ? "Существующие ID и цены сохранятся. Новые варианты появятся у всех поставщиков без цены." : "После создания модель появится у всех поставщиков без цены.", "form-note"); wrap.append(status);
    const save = button(editing ? "Сохранить модель" : "Создать модель", async () => {
      const rows = [...list.querySelectorAll(".variant-row")].map(row => ({product_id: row.dataset.productId ? Number(row.dataset.productId) : null, memory: row.querySelectorAll("input")[0].value.trim(), color: row.querySelectorAll("input")[1].value.trim()}));
      const categoryId = category.value();
      if (!model.value.trim() || !categoryId || !rows.length || rows.length > 500) { status.textContent = "Выберите категорию из подсказок и заполните от 1 до 500 вариантов."; status.classList.add("danger"); return; }
      save.disabled = true; status.textContent = "Сохраняем…"; status.classList.remove("danger");
      try {
        const payload = editing ? {category_id: categoryId, model_name: model.value.trim(), variants: rows.filter(row => row.product_id).map(({product_id, memory, color}) => ({product_id, memory, color})), add_variants: rows.filter(row => !row.product_id).map(({memory, color}) => ({memory, color})), expected_revision: initial.revision} : {category_id: categoryId, new_category_name: null, model_name: model.value.trim(), variants: rows.map(({memory, color}) => ({memory, color}))};
        const result = await api(editing ? `models/${initial.anchor_product_id}` : "products", payload);
        $("dialog").close(); await load(); $("search").value = model.value.trim(); renderModels(); notice(editing ? `Модель сохранена. Обновлено вариантов: ${result.updated_count}, добавлено: ${result.created_count}.` : `Модель создана. Добавлено вариантов: ${result.created_count}.`);
      } catch (error) { status.textContent = error.message; status.classList.add("danger"); save.disabled = false; }
    }, true);
    modal(editing ? "Редактировать модель" : "Добавить новую модель", [wrap], [button("Отмена", () => $("dialog").close()), save]); model.focus();
  }
  async function editModel(productId) { try { notice("Загружаем модель…"); const detail = await api("models/" + productId); notice(""); openEditor(detail); } catch (error) { notice(error.message, true); } }
  function invalidateImport() {
    state.importPreview = null; state.importText = ""; $("import-preview").hidden = true; $("import-panel").classList.remove("reviewing");
  }
  function setupImportCategory() {
    const input = $("import-category-search"), suggestions = $("import-category-suggestions");
    const render = () => {
      const matches = matchingCategories(input.value);
      suggestions.replaceChildren(...matches.map(item => {
        const option = node("button", item.name); option.type = "button"; option.setAttribute("role", "option");
        option.addEventListener("mousedown", event => {
          event.preventDefault(); state.importCategoryId = item.category_id; input.value = item.name;
          $("import-category-selected").textContent = `Выбрано: ${item.name}`; $("import-category-selected").classList.add("chosen");
          suggestions.hidden = true; input.setAttribute("aria-expanded", "false"); invalidateImport();
        }); return option;
      }));
      suggestions.hidden = !matches.length; input.setAttribute("aria-expanded", String(!!matches.length));
    };
    input.addEventListener("input", () => { state.importCategoryId = null; $("import-category-selected").textContent = "Выберите категорию из подсказок"; $("import-category-selected").classList.remove("chosen"); invalidateImport(); render(); });
    input.addEventListener("focus", render);
    input.addEventListener("blur", () => setTimeout(() => { suggestions.hidden = true; input.setAttribute("aria-expanded", "false"); }, 100));
    $("import-text").addEventListener("input", invalidateImport);
  }
  function renderImportPreview(data) {
    state.importPreview = data; state.importText = $("import-text").value;
    $("preview-title").textContent = data.category_name;
    $("preview-summary").textContent = `${number(data.model_count)} моделей · ${number(data.variant_count)} вариантов`;
    const warning = $("preview-warning");
    warning.hidden = !data.warnings.length;
    warning.textContent = data.warnings.includes("source_category_ignored") ? `Номера категории из строк 0 (${data.source_category_ids.join(", ")}) проигнорированы. Будет использована выбранная категория «${data.category_name}».` : "";
    $("preview-models").replaceChildren(...data.models.map(item => {
      const card = node("article", undefined, "preview-model");
      card.append(node("strong", item.model_name), node("span", `${item.variants.length} вариантов`)); return card;
    }));
    $("sorting-models").value = data.sorting.models.join("\n");
    $("sorting-memories").value = data.sorting.memories.join("\n");
    $("sorting-colors").value = data.sorting.colors.join("\n");
    $("import-preview").hidden = false; $("import-panel").classList.add("reviewing");
    $("import-preview").scrollIntoView({behavior: "smooth", block: "start"});
  }
  async function previewImport() {
    if (!state.importCategoryId) { notice("Выберите категорию из выпадающих подсказок.", true); $("import-category-search").focus(); return; }
    const text = $("import-text").value.trim();
    if (!text) { notice("Вставьте список моделей.", true); $("import-text").focus(); return; }
    const action = $("preview-import"); action.disabled = true; notice("Разбираем список…");
    try { renderImportPreview(await api("model-import/preview", {category_id: state.importCategoryId, text})); notice("Готовая версия подготовлена. Проверьте данные ниже."); }
    catch (error) { invalidateImport(); notice(error.message, true); }
    finally { action.disabled = false; }
  }
  async function applyImport() {
    if (!state.importPreview || state.importText !== $("import-text").value || state.importCategoryId !== state.importPreview.category_id) { notice("Данные изменились. Подготовьте результат заново.", true); invalidateImport(); return; }
    const action = $("apply-import"); action.disabled = true; notice("Добавляем все модели одной операцией…");
    try {
      const result = await api("model-import/apply", {category_id: state.importCategoryId, text: state.importText, preview_hash: state.importPreview.preview_hash});
      $("import-text").value = ""; invalidateImport(); await load();
      notice(`Добавлено моделей: ${number(result.model_count)}. Создано вариантов: ${number(result.created_count)}.`);
    } catch (error) { notice(error.message, true); }
    finally { action.disabled = false; }
  }
  async function load() {
    if (state.busy) return; state.busy = true; notice("Загружаем каталог…");
    try {
      const [models, categories] = await Promise.all([api("models"), api("categories")]); state.models = models.models; state.categories = categories.categories;
      $("model-count").textContent = number(models.model_count); $("variant-count").textContent = number(models.variant_count); $("category-count").textContent = number(state.categories.length);
      const selected = $("category").value; $("category").replaceChildren(new Option("Все категории", ""), ...state.categories.map(item => new Option(item.name, item.category_id))); if ([...$("category").options].some(item => item.value === selected)) $("category").value = selected;
      $("workspace").hidden = false; $("add-model").hidden = false; renderModels(); notice("");
    } catch (error) { notice(error.message, true); } finally { state.busy = false; }
  }
  setupImportCategory();
  $("preview-import").addEventListener("click", previewImport); $("apply-import").addEventListener("click", applyImport); $("reset-import").addEventListener("click", () => { invalidateImport(); $("import-text").focus(); });
  document.querySelectorAll(".copy-list").forEach(action => action.addEventListener("click", async () => { const field = $(action.dataset.copy); try { await navigator.clipboard.writeText(field.value); action.textContent = "Скопировано"; setTimeout(() => action.textContent = "Скопировать", 1200); } catch { field.select(); } }));
  $("add-model").addEventListener("click", () => openEditor()); $("refresh").addEventListener("click", load); $("search").addEventListener("input", renderModels); $("category").addEventListener("change", renderModels); $("close-dialog").addEventListener("click", () => $("dialog").close());
  document.addEventListener("keydown", event => { if (event.key === "/" && !["INPUT", "SELECT", "TEXTAREA"].includes(document.activeElement.tagName)) { event.preventDefault(); $("search").focus(); } });
  load();
})();

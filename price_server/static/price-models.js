(() => {
  "use strict";
  const $ = id => document.getElementById(id);
  const state = {models: [], categories: [], busy: false, mode: "single", importCategoryId: null, importPreview: null, pending: null, saving: false, uncertain: false, excluded: new Set()};
  const DRAFT_KEY = "texnikach:model-creator:v1";
  let creatorCategory;
  const errors = {
    catalog_model_not_found: "Модель больше не найдена. Обновите список.",
    catalog_model_changed: "Модель уже изменили после открытия формы. Обновите данные и повторите.",
    catalog_variants_changed: "Состав вариантов изменился. Обновите данные.",
    catalog_product_exists: "Такой вариант уже существует в каталоге.",
    duplicate_catalog_variant: "Память и цвет не должны повторяться.",
    invalid_catalog_variants: "Проверьте количество конфигураций: до 500 на модель и 5 000 на список.",
    invalid_catalog_request: "Проверьте категорию, название и варианты.",
    catalog_model_delete_confirmation_required: "Подтвердите удаление модели.",
    catalog_variant_not_found: "Конфигурация больше не найдена. Обновите модель.",
    catalog_variant_delete_confirmation_required: "Подтвердите удаление конфигурации.",
    catalog_last_variant_delete_forbidden: "Последнюю конфигурацию можно удалить только вместе с моделью.",
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
      error.code = code; error.status = response.status;
      error.details = typeof data.detail === "object" ? data.detail : {};
      if (error.details.message) error.message = error.details.message;
      if (error.details.model_names?.length) error.message += " Уже в каталоге: " + error.details.model_names.join(", ") + ".";
      if (error.details.model_name) error.message += " Модель: " + error.details.model_name + ".";
      if (error.details.line) error.message += " Строка: " + error.details.line + ".";
      throw error;
    }
    return data;
  }
  const button = (text, action, primary = false) => { const value = node("button", text, "button " + (primary ? "primary" : "secondary")); value.type = "button"; value.addEventListener("click", action); return value; };
  function modal(title, content, actions) { $("dialog-title").textContent = title; $("dialog-body").replaceChildren(...content); $("dialog-actions").replaceChildren(...actions); if (!$("dialog").open) $("dialog").showModal(); }
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
  let pickerSequence = 0;
  function categoryPicker(initialId = null, onChange = () => {}) {
    const wrap = node("div", undefined, "category-picker"), input = node("input"), suggestions = node("div", undefined, "category-suggestions");
    input.type = "search"; input.autocomplete = "off"; input.placeholder = "Например, Apple Watch или Samsung…"; suggestions.hidden = true; suggestions.setAttribute("role", "listbox");
    suggestions.id = "category-options-" + (++pickerSequence); input.setAttribute("role", "combobox"); input.setAttribute("aria-autocomplete", "list"); input.setAttribute("aria-controls", suggestions.id); input.setAttribute("aria-expanded", "false"); input.setAttribute("aria-label", "Категория");
    let active = -1, matches = [];
    let selected = initialId ? state.categories.find(item => item.category_id === Number(initialId)) || null : null;
    if (selected) input.value = selected.name;
    const close = () => { suggestions.hidden = true; input.setAttribute("aria-expanded", "false"); input.removeAttribute("aria-activedescendant"); active = -1; };
    const choose = item => { selected = item; input.value = item.name; close(); onChange(item); };
    const render = () => {
      matches = matchingCategories(input.value); active = -1;
      suggestions.replaceChildren(...matches.map(item => {
        const option = node("button", item.name); option.type = "button"; option.setAttribute("role", "option");
        option.id = suggestions.id + "-" + item.category_id; option.tabIndex = -1;
        option.addEventListener("mousedown", event => event.preventDefault());
        option.addEventListener("click", () => { choose(item); input.focus(); close(); });
        return option;
      }));
      if (!matches.length) suggestions.append(node("p", "Категория не найдена. Попробуйте другое название.", "category-empty"));
      suggestions.hidden = false; input.setAttribute("aria-expanded", "true");
    };
    input.addEventListener("input", () => { selected = null; onChange(null); render(); });
    input.addEventListener("focus", render);
    input.addEventListener("blur", () => setTimeout(close, 160));
    input.addEventListener("keydown", event => {
      if (event.key === "Escape") { close(); return; }
      if (["ArrowDown", "ArrowUp"].includes(event.key)) {
        event.preventDefault(); if (suggestions.hidden) render();
        if (!matches.length) return;
        active = (active + (event.key === "ArrowDown" ? 1 : -1) + matches.length) % matches.length;
        [...suggestions.querySelectorAll("button")].forEach((option, index) => { option.setAttribute("aria-selected", String(index === active)); if (index === active) { input.setAttribute("aria-activedescendant", option.id); option.scrollIntoView({block: "nearest"}); } });
      } else if (event.key === "Enter" && !suggestions.hidden) {
        event.preventDefault(); if (active >= 0 || matches.length === 1) choose(matches[Math.max(0, active)]);
      }
    });
    wrap.append(input, suggestions);
    return {element: wrap, input, value: () => selected ? selected.category_id : null, select: choose};
  }
  function appendVariant(container, item = {}, options = {}) {
    const row = node("div", undefined, "variant-row " + (item.product_id ? "existing" : "new")); row.dataset.productId = item.product_id || "";
    const id = node("span", item.product_id ? `ID ${item.product_id}` : "Новый", "variant-id");
    const memory = node("input"); memory.placeholder = "Память"; memory.maxLength = 100; memory.value = item.memory || ""; memory.setAttribute("aria-label", "Память");
    const color = node("input"); color.placeholder = "Цвет"; color.maxLength = 150; color.value = item.color || ""; color.setAttribute("aria-label", "Цвет");
    const remove = node("button", item.product_id ? "Удалить" : "×", "remove"); remove.type = "button";
    if (item.product_id) {
      remove.classList.add("existing-remove");
      remove.title = options.canDelete ? "Удалить эту конфигурацию" : "Последнюю конфигурацию можно удалить только вместе с моделью";
      remove.disabled = !options.canDelete;
      remove.addEventListener("click", () => options.onDelete?.(item));
    } else {
      remove.title = "Убрать новый вариант";
      remove.addEventListener("click", () => row.remove());
    }
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
    const initialVariants = initial.variants || [{}];
    initialVariants.forEach(item => appendVariant(list, item, {
      canDelete: editing && initialVariants.length > 1,
      onDelete: variant => confirmDeleteVariant(initial, variant),
    })); wrap.append(variantsWrap);
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
    const actions = [button("Отмена", () => $("dialog").close())];
    if (editing) {
      const remove = button("Удалить модель", () => confirmDeleteModel(initial));
      remove.classList.add("danger"); actions.push(remove);
    }
    actions.push(save);
    modal(editing ? "Редактировать модель" : "Добавить новую модель", [wrap], actions); model.focus();
  }
  function confirmDeleteModel(model) {
    const wrap = node("div", undefined, "delete-confirm");
    const warning = node("div", undefined, "delete-warning");
    warning.append(node("strong", "Это удалит модель из действующего каталога"), node("p", "Все варианты модели и введённые для них текущие цены поставщиков будут удалены. История прошлых изменений и использованные ID сохранятся."));
    const summary = node("div", undefined, "delete-model-summary");
    const modelCell = node("div"), variantsCell = node("div");
    modelCell.append(node("span", "Модель"), node("strong", model.model_name));
    variantsCell.append(node("span", "Будет удалено"), node("strong", `${number(model.variants.length)} вариантов`));
    summary.append(modelCell, variantsCell);
    const acknowledge = node("label", undefined, "delete-acknowledge"), checkbox = node("input"), acknowledgement = node("span", "Я понимаю, что модель исчезнет из каталога вместе с текущими ценами её вариантов.");
    checkbox.type = "checkbox"; acknowledge.append(checkbox, acknowledgement);
    const status = node("p", "После удаления каталог будет обновлён при следующем запуске импорта.", "form-note");
    wrap.append(warning, summary, acknowledge, status);
    const remove = button("Удалить безвозвратно", async () => {
      remove.disabled = true; checkbox.disabled = true; status.textContent = "Удаляем модель…"; status.classList.remove("danger");
      try {
        const result = await api(`models/${model.anchor_product_id}`, {action: "delete", expected_revision: model.revision, confirm: true});
        $("dialog").close(); await load();
        notice(`Модель «${result.model_name}» удалена. Удалено вариантов: ${number(result.deleted_variant_count)}, текущих цен: ${number(result.deleted_price_count)}.`);
      } catch (error) {
        status.textContent = error.message; status.classList.add("danger"); checkbox.disabled = false; remove.disabled = !checkbox.checked;
      }
    });
    remove.classList.add("danger-solid"); remove.disabled = true;
    checkbox.addEventListener("change", () => { remove.disabled = !checkbox.checked; });
    modal("Удалить модель?", [wrap], [button("Вернуться к редактированию", () => openEditor(model)), remove]);
  }
  function confirmDeleteVariant(model, variant) {
    const wrap = node("div", undefined, "delete-confirm");
    const warning = node("div", undefined, "delete-warning");
    warning.append(node("strong", "Это удалит только выбранную конфигурацию"), node("p", "Её текущие цены поставщиков будут удалены. Остальные конфигурации модели сохранятся, а использованные ID не будут выданы повторно."));
    const summary = node("div", undefined, "delete-model-summary variant-delete-summary");
    const modelCell = node("div"), idCell = node("div"), memoryCell = node("div"), colorCell = node("div");
    modelCell.append(node("span", "Модель"), node("strong", model.model_name));
    idCell.append(node("span", "ID товара"), node("strong", String(variant.product_id)));
    memoryCell.append(node("span", "Память / размер"), node("strong", variant.memory || "Не указано"));
    colorCell.append(node("span", "Цвет"), node("strong", variant.color || "Не указан"));
    summary.append(modelCell, idCell, memoryCell, colorCell);
    const acknowledge = node("label", undefined, "delete-acknowledge"), checkbox = node("input"), acknowledgement = node("span", "Я понимаю, что эта конфигурация исчезнет из каталога вместе с её текущими ценами.");
    checkbox.type = "checkbox"; acknowledge.append(checkbox, acknowledgement);
    const status = node("p", "Модель останется в каталоге с остальными конфигурациями.", "form-note");
    wrap.append(warning, summary, acknowledge, status);
    const remove = button("Удалить конфигурацию", async () => {
      remove.disabled = true; checkbox.disabled = true; status.textContent = "Удаляем конфигурацию…"; status.classList.remove("danger");
      try {
        const result = await api(`models/${variant.product_id}`, {action: "delete_variant", expected_revision: model.revision, confirm: true});
        $("dialog").close(); await load();
        notice(`Конфигурация ID ${result.product_id} удалена. Осталось вариантов: ${number(result.remaining_variant_count)}, удалено текущих цен: ${number(result.deleted_price_count)}.`);
      } catch (error) {
        status.textContent = error.message; status.classList.add("danger"); checkbox.disabled = false; remove.disabled = !checkbox.checked;
      }
    });
    remove.classList.add("danger-solid"); remove.disabled = true;
    checkbox.addEventListener("change", () => { remove.disabled = !checkbox.checked; });
    modal("Удалить конфигурацию?", [wrap], [button("Вернуться к редактированию", () => openEditor(model)), remove]);
  }
  async function editModel(productId) { try { notice("Загружаем модель…"); const detail = await api("models/" + productId); notice(""); openEditor(detail); } catch (error) { notice(error.message, true); } }
  const clean = value => value.normalize("NFC").trim().replace(/\s+/g, " ");
  const unique = values => [...new Map(values.map(value => [value.toLocaleLowerCase("ru"), value])).values()];
  function dimensionValues(raw) { return unique(raw.split(/[,;，\n]+/).map(clean).filter(Boolean)); }
  const variantKey = variant => JSON.stringify([variant.memory, variant.color]);
  function singleVariants() {
    const memories = dimensionValues($("new-memories").value), colors = dimensionValues($("new-colors").value);
    const total = (memories.length || 1) * (colors.length || 1);
    if (total > 500) return {variants: [], total, error: "Слишком много сочетаний. На одну модель можно добавить до 500 конфигураций."};
    if (memories.some(value => value.length > 100) || colors.some(value => value.length > 150)) return {variants: [], total, error: "Сократите память до 100 символов, цвет — до 150."};
    const variants = (memories.length ? memories : [""]).flatMap(memory => (colors.length ? colors : [""]).map(color => ({memory, color})));
    return {variants, total};
  }
  function renderSingle() {
    const {variants, total, error} = singleVariants();
    const selected = variants.filter(item => !state.excluded.has(variantKey(item)));
    $("single-count").textContent = number(selected.length);
    $("single-hint").textContent = total === 1 ? "Один товар. Память и цвет можно оставить пустыми." : `Выбрано ${selected.length} из ${total}. Снимите галочки с сочетаний, которых не будет.`;
    const groups = new Map();
    for (const item of variants) { if (!groups.has(item.memory)) groups.set(item.memory, []); groups.get(item.memory).push(item); }
    $("single-variants").replaceChildren(...[...groups].map(([memory, items]) => {
      const group = node("div", undefined, "generated-memory"), options = node("div", undefined, "variant-options");
      group.append(node("strong", memory || "Без памяти / размера"));
      for (const item of items) {
        const label = node("label", undefined, "choice"), input = node("input"); input.type = "checkbox"; input.checked = !state.excluded.has(variantKey(item));
        input.addEventListener("change", () => { if (input.checked) state.excluded.delete(variantKey(item)); else state.excluded.add(variantKey(item)); renderSingleCount(); persistDraft(); });
        label.append(input, node("span", item.color || "Без цвета")); options.append(label);
      }
      group.append(options); return group;
    }));
    const existing = state.models.find(item => item.category_id === state.importCategoryId && item.model_name.toLocaleLowerCase("ru") === clean($("new-model-name").value).toLocaleLowerCase("ru"));
    const status = $("single-validation"); status.replaceChildren(); status.classList.toggle("danger", !!error || !!existing);
    if (error) status.textContent = error;
    else if (existing) { status.append(node("span", "Такая модель уже есть. "), button("Редактировать её", () => editModel(existing.anchor_product_id))); }
  }
  function renderSingleCount() {
    const values = singleVariants(); const count = values.variants.filter(item => !state.excluded.has(variantKey(item))).length;
    $("single-count").textContent = number(count);
    $("single-hint").textContent = `Выбрано ${count} из ${values.total}. Снимите галочки с сочетаний, которых не будет.`;
  }
  function updateSuggestions() {
    const category = state.categories.find(item => item.category_id === state.importCategoryId)?.name || "";
    const memories = /watch|часы|браслет/i.test(category) ? ["40mm", "42mm", "44mm", "46mm", "49mm"] : ["128 GB", "256 GB", "512 GB", "1 TB"];
    for (const [target, field, values] of [["memory-suggestions", "new-memories", memories], ["color-suggestions", "new-colors", ["Black", "White", "Silver", "Blue", "Gold"]]]) {
      $(target).replaceChildren(...values.map(value => {
        const suggestion = button("＋ " + value, () => {
          $(field).value = unique([...dimensionValues($(field).value), value]).join(", ");
          state.excluded.clear(); renderSingle(); persistDraft();
        }); suggestion.className = "suggestion-chip"; return suggestion;
      }));
    }
  }
  function setMode(mode) {
    if (state.saving || state.uncertain) return;
    state.mode = mode;
    for (const name of ["single", "batch"]) {
      const active = mode === name; $(name + "-tab").classList.toggle("active", active); $(name + "-tab").setAttribute("aria-selected", String(active)); $(name + "-tab").tabIndex = active ? 0 : -1; $(name + "-form").hidden = !active;
    }
    $("preview-import").textContent = mode === "single" ? "Проверить модель →" : "Проверить список →";
    $("creator-help").textContent = mode === "single" ? "Сначала покажем результат. Цены можно заполнить после добавления." : "Все модели попадут в выбранную категорию. Сначала покажем каждую конфигурацию.";
    persistDraft();
  }
  function persistDraft() {
    const draft = {mode: state.mode, category: state.importCategoryId, name: $("new-model-name").value, memories: $("new-memories").value, colors: $("new-colors").value, text: $("import-text").value, excluded: [...state.excluded], pending: state.uncertain || state.saving ? state.pending : null};
    try { sessionStorage.setItem(DRAFT_KEY, JSON.stringify(draft)); $("draft-indicator").textContent = "Черновик сохранён в этой вкладке"; }
    catch { $("draft-indicator").textContent = "Не закрывайте вкладку: черновик не сохранён"; }
    $("batch-length").textContent = `${number(draft.text.length)} / 12 000 символов`;
  }
  function restoreDraft() {
    let draft;
    try { draft = JSON.parse(sessionStorage.getItem(DRAFT_KEY) || "null"); } catch {}
    if (!draft || typeof draft !== "object") { renderSingle(); updateSuggestions(); return; }
    for (const [id, key] of [["new-model-name", "name"], ["new-memories", "memories"], ["new-colors", "colors"], ["import-text", "text"]]) if (typeof draft[key] === "string") $(id).value = draft[key];
    state.excluded = new Set(Array.isArray(draft.excluded) ? draft.excluded : []);
    const category = state.categories.find(item => item.category_id === draft.category); if (category) creatorCategory.select(category);
    setMode(draft.mode === "batch" ? "batch" : "single"); renderSingle();
    if (draft.pending?.operation && draft.pending?.preview && ["products", "model-import/apply"].includes(draft.pending.path)) {
      state.pending = draft.pending; state.uncertain = true; renderImportPreview(draft.pending.preview); showUncertain(); persistDraft();
    }
  }
  function invalidateImport() {
    if (state.saving || state.uncertain) return;
    state.importPreview = null; state.pending = null; $("import-preview").hidden = true; $("creator-inputs").hidden = false; $("create-success").hidden = true; $("import-panel").classList.remove("reviewing");
  }
  function setupImportCategory() {
    creatorCategory = categoryPicker(null, item => {
      state.importCategoryId = item?.category_id || null;
      $("import-category-selected").textContent = item ? `Выбрано: ${item.name}` : "Выберите категорию из подсказок.";
      $("import-category-selected").classList.toggle("chosen", !!item);
      invalidateImport(); renderSingle(); updateSuggestions(); persistDraft();
    });
    creatorCategory.input.id = "import-category-search";
    $("import-category-picker").append(creatorCategory.element);
  }
  function renderImportPreview(data) {
    state.importPreview = data;
    $("preview-title").textContent = "Всё готово к добавлению";
    $("preview-summary").textContent = `${data.category_name} · моделей: ${number(data.model_count)} · конфигураций: ${number(data.variant_count)}`;
    const warning = $("preview-warning");
    const warnings = [];
    if (data.warnings.includes("source_category_ignored")) warnings.push(`Номера из строк 0 проигнорированы. Все модели попадут в «${data.category_name}».`);
    if (state.mode === "batch" && /^\s*[0123][.)]/m.test($("import-text").value) && data.models.some(item => item.variants.some(v => !v.color) && item.variants.some(v => v.color))) warnings.push("В привычном нумерованном формате также создаётся вариант без цвета. Он показан ниже. Для точных сочетаний используйте простой список из трёх строк.");
    warning.hidden = !warnings.length; warning.textContent = warnings.join("\n");
    $("preview-models").replaceChildren(...data.models.map(item => {
      const card = node("article", undefined, "review-model"), header = node("header");
      header.append(node("h4", item.model_name), node("span", `${item.variants.length} конфигураций`)); card.append(header);
      const groups = new Map();
      for (const variant of item.variants) { if (!groups.has(variant.memory)) groups.set(variant.memory, []); groups.get(variant.memory).push(variant.color); }
      const detail = node("details"); detail.open = data.model_count <= 6;
      detail.append(node("summary", "Память и цвета"));
      const list = node("div", undefined, "review-variants");
      for (const [memory, colors] of groups) { const row = node("div"), chips = node("div", undefined, "review-colors"); row.append(node("strong", memory || "Без памяти / размера")); chips.append(...colors.map(color => node("span", color || "Без цвета", "review-chip"))); row.append(chips); list.append(row); }
      detail.append(list); card.append(detail); return card;
    }));
    for (const field of ["models", "memories", "colors"]) $("sorting-" + field).value = data.sorting[field].join("\n");
    $("creator-inputs").hidden = true; $("create-success").hidden = true; $("import-preview").hidden = false; $("import-panel").classList.add("reviewing");
    $("apply-import").textContent = `Добавить в каталог · ${number(data.variant_count)} конфигураций`;
    $("reset-import").disabled = false;
    $("confirm-note").textContent = "Каждая конфигурация получит свой ID и появится во «Вводе цен» у всех поставщиков.";
    $("import-preview").scrollIntoView({behavior: "smooth", block: "start"});
  }
  async function previewImport() {
    if (state.saving || state.uncertain) return;
    if (!state.importCategoryId) { notice("Выберите категорию из подсказок.", true); creatorCategory.input.focus(); return; }
    const category = state.categories.find(item => item.category_id === state.importCategoryId);
    const mode = state.mode; let body, data, path;
    const action = $("preview-import");
    if (mode === "single") {
      const model_name = clean($("new-model-name").value), generated = singleVariants();
      const variants = generated.variants.filter(item => !state.excluded.has(variantKey(item)));
      if (!model_name) { notice("Введите название модели.", true); $("new-model-name").focus(); return; }
      if (generated.error || !variants.length) { notice(generated.error || "Выберите хотя бы одну конфигурацию.", true); return; }
      if (state.models.some(item => item.category_id === category.category_id && item.model_name.toLocaleLowerCase("ru") === model_name.toLocaleLowerCase("ru"))) { notice("Эта модель уже есть. Откройте её через кнопку «Редактировать её» под конфигурациями.", true); return; }
      body = {category_id: category.category_id, new_category_name: null, model_name, variants}; path = "products";
      data = {category_id: category.category_id, category_name: category.name, model_count: 1, variant_count: variants.length, models: [{model_name, variants}], warnings: [], sorting: {models: [model_name], memories: unique(variants.map(v => v.memory).filter(Boolean)), colors: unique(variants.map(v => v.color).filter(Boolean))}};
    } else {
      const text = $("import-text").value;
      if (!text.trim()) { notice("Вставьте список или нажмите «Вставить пример».", true); $("import-text").focus(); return; }
      action.disabled = true; notice("Проверяем список и повторы в каталоге…");
      try {
        data = await api("model-import/preview", {category_id: category.category_id, text});
        // A slow preview must never replace more recent input or a new mode.
        if (state.mode !== mode || state.importCategoryId !== category.category_id || $("import-text").value !== text) { notice("Данные изменились во время проверки. Проверьте список ещё раз."); return; }
        path = "model-import/apply"; body = {category_id: category.category_id, text, preview_hash: data.preview_hash};
      } catch (error) { notice(error.message, true); return; }
      finally { action.disabled = false; }
    }
    state.pending = {path, body, operation: operationId(), preview: data};
    renderImportPreview(data); notice("");
  }
  function showUncertain() {
    $("reset-import").disabled = true;
    $("apply-import").textContent = "Проверить результат и завершить";
    $("confirm-note").textContent = "Ответ сервера не получен. Нажмите кнопку, чтобы завершить ту же операцию: повторные товары не создадутся.";
    notice("Не удалось получить подтверждение. Черновик и номер операции сохранены в этой вкладке.\nОперация: " + state.pending.operation, true);
  }
  async function applyImport() {
    if (!state.pending || state.saving) return;
    const pending = state.pending, wasUncertain = state.uncertain; state.saving = true; persistDraft();
    $("apply-import").disabled = true; $("reset-import").disabled = true; notice("Добавляем модели…");
    let result;
    try {
      result = await api(pending.path, pending.body, pending.operation);
      if (!Number.isInteger(result.created_count) || result.created_count < 0) throw new Error("Некорректное подтверждение сервера.");
    } catch (error) {
      if (wasUncertain || !error.status || error.status >= 500) {
        state.uncertain = true; showUncertain();
        if (error.status && error.status < 500) notice(error.message + " Номер предыдущей операции сохранён. После входа обновите страницу и завершите её проверку.", true);
      }
      else { state.uncertain = false; $("reset-import").disabled = false; notice(error.message, true); }
      state.saving = false; $("apply-import").disabled = false; persistDraft(); return;
    }
    state.saving = false; state.uncertain = false; state.pending = null; state.importPreview = null;
    for (const id of ["new-model-name", "new-memories", "new-colors", "import-text"]) $(id).value = "";
    state.excluded.clear(); persistDraft();
    $("import-preview").hidden = true; $("create-success").hidden = false; $("import-panel").classList.remove("reviewing");
    $("success-title").textContent = pending.preview.model_count === 1 ? "Модель добавлена" : "Модели добавлены";
    $("success-detail").textContent = `${pending.preview.category_name} · моделей: ${number(pending.preview.model_count)} · новых конфигураций: ${number(result.created_count)}. Теперь можно заполнить цены.`;
    $("apply-import").disabled = false; notice("");
    await load(false);
    $("create-success").scrollIntoView({behavior: "smooth", block: "center"});
  }
  async function load(showNotice = true) {
    if (state.busy) return; state.busy = true; if (showNotice) notice("Загружаем каталог…");
    try {
      const [models, categories] = await Promise.all([api("models"), api("categories")]); state.models = models.models; state.categories = categories.categories;
      $("model-count").textContent = number(models.model_count); $("variant-count").textContent = number(models.variant_count); $("category-count").textContent = number(state.categories.length);
      const selected = $("category").value; $("category").replaceChildren(new Option("Все категории", ""), ...state.categories.map(item => new Option(item.name, item.category_id))); if ([...$("category").options].some(item => item.value === selected)) $("category").value = selected;
      $("login-panel").hidden = true; $("workspace").hidden = false; $("add-model").hidden = false; renderModels(); if (showNotice) notice(""); return true;
    } catch (error) { notice(showNotice ? error.message : "Модели уже сохранены, но список не обновился. Нажмите «Обновить» в каталоге.", true); return false; } finally { state.busy = false; }
  }
  setupImportCategory();
  $("preview-import").addEventListener("click", previewImport); $("apply-import").addEventListener("click", applyImport); $("reset-import").addEventListener("click", () => { invalidateImport(); $(state.mode === "single" ? "new-model-name" : "import-text").focus(); });
  document.querySelectorAll(".copy-list").forEach(action => action.addEventListener("click", async () => { const field = $(action.dataset.copy); try { await navigator.clipboard.writeText(field.value); action.textContent = "Скопировано"; setTimeout(() => action.textContent = "Скопировать", 1200); } catch { field.select(); } }));
  const openCreator = () => { if (state.saving || state.uncertain) { $("import-panel").scrollIntoView({behavior: "smooth"}); return; } invalidateImport(); renderSingle(); $("import-panel").scrollIntoView({behavior: "smooth", block: "start"}); $(state.mode === "single" ? "new-model-name" : "import-text").focus({preventScroll: true}); };
  $("add-model").addEventListener("click", openCreator); $("create-another").addEventListener("click", openCreator);
  $("show-catalog").addEventListener("click", () => { $("catalog-panel").scrollIntoView({behavior: "smooth", block: "start"}); $("search").focus({preventScroll: true}); });
  for (const mode of ["single", "batch"]) { $(mode + "-tab").addEventListener("click", () => setMode(mode)); $(mode + "-tab").addEventListener("keydown", event => { if (["ArrowLeft", "ArrowRight"].includes(event.key)) { event.preventDefault(); const next = mode === "single" ? "batch" : "single"; setMode(next); $(next + "-tab").focus(); } }); }
  for (const id of ["new-model-name", "new-memories", "new-colors"]) $(id).addEventListener("input", () => { if (id !== "new-model-name") state.excluded.clear(); renderSingle(); persistDraft(); });
  $("import-text").addEventListener("input", persistDraft);
  $("insert-example").addEventListener("click", () => { if ($("import-text").value.trim() && !confirm("Заменить текущий список примером?")) return; $("import-text").value = "Apple Watch Series 12 Aluminum\n42mm, 46mm\nBlack, Silver, Gold\n\nApple Watch Ultra 4\n49mm\nBlack, Natural"; persistDraft(); $("import-text").focus(); });
  $("clear-batch").addEventListener("click", () => { if ($("import-text").value && confirm("Очистить введённый список?")) { $("import-text").value = ""; persistDraft(); $("import-text").focus(); } });
  $("refresh").addEventListener("click", () => load()); $("search").addEventListener("input", renderModels); $("category").addEventListener("change", renderModels); $("close-dialog").addEventListener("click", () => $("dialog").close());
  document.addEventListener("keydown", event => { if (event.key === "/" && !["INPUT", "SELECT", "TEXTAREA"].includes(document.activeElement.tagName)) { event.preventDefault(); $("search").focus(); } });
  window.addEventListener("beforeunload", event => { if (state.saving) { event.preventDefault(); event.returnValue = ""; } });
  load().then(loaded => { if (loaded) restoreDraft(); });
})();

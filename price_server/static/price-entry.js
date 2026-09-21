(() => {
  "use strict";
  const $ = id => document.getElementById(id);
  const state = {suppliers: [], categories: [], sheet: null, rows: [], filtered: [], edits: new Map(), selected: new Set(), page: 0, busy: false, uncertain: false};
  const PAGE_SIZE = 50;
  const number = value => Number(value).toLocaleString("ru-RU");
  const shownPrice = value => value === null || value === "" ? "нет" : number(value);
  const fields = {price_1: "1 месяц", price_12: "12 месяцев"};
  const errors = {
    price_entry_unavailable: "Не удалось загрузить цены. Попробуйте ещё раз позже.",
    local_prices_not_initialized: "Серверный каталог ещё не подготовлен. Цены не изменены.",
    price_entry_maintenance: "Перенос цен на сервер. Сохранение временно приостановлено — не закрывайте черновик.",
    supplier_schema_changed: "Структура листа изменилась. Редактирование остановлено: нужно проверить заголовки в таблице.",
    duplicate_product_identity: "В таблице есть повторяющиеся ID товаров. Редактирование остановлено до исправления.",
    prices_changed: "Цены или данные товаров изменились после загрузки страницы. Ничего не записано. Сверьте правки и обновите данные.",
    cell_read_only: "Одна из ячеек теперь защищена или содержит формулу. Ничего не записано.",
    save_in_progress: "Другое сохранение ещё выполняется. Подождите и попробуйте снова.",
    invalid_price: "Цена должна быть целым числом от 0 до 100 000. Пустая ячейка означает отсутствие предложения.",
    save_outcome_unknown: "Не удалось подтвердить результат сохранения. Не отправляйте правки повторно. Проверьте историю сохранений и обновите данные перед новой попыткой.",
    supplier_not_found: "Лист поставщика не найден. Обновите страницу.",
    invalid_change_count: "За один раз можно сохранить от 1 до 200 цен.",
    catalog_management_not_initialized: "Добавление моделей ещё не подготовлено. Ввод цен продолжает работать.",
    invalid_catalog_request: "Проверьте название модели, категорию и варианты.",
    invalid_catalog_text: "Название, память или цвет заполнены некорректно.",
    invalid_catalog_variants: "Добавьте от 1 до 100 вариантов модели.",
    duplicate_catalog_variant: "Одинаковый вариант памяти и цвета указан дважды.",
    catalog_category_exists: "Такая категория уже существует. Выберите её из списка.",
    catalog_category_not_found: "Категория больше не существует. Обновите страницу.",
    catalog_product_exists: "Такой вариант модели уже существует в каталоге.",
  };

  function node(tag, text, className) {
    const n = document.createElement(tag);
    if (text !== undefined) n.textContent = text;
    if (className) n.className = className;
    return n;
  }
  function notice(text, error = false) {
    $("notice").textContent = text;
    $("notice").className = "notice" + (error ? " error" : "");
    $("notice").hidden = !text;
  }
  function csrf() {
    const cookie = document.cookie.split("; ").find(v => v.startsWith("__Host-texnikach_monitoring_csrf="));
    return cookie ? decodeURIComponent(cookie.slice(cookie.indexOf("=") + 1)) : "";
  }
  async function api(path, body, id) {
    const options = {credentials: "same-origin", headers: {Accept: "application/json"}};
    if (body) Object.assign(options, {method: "POST", body: JSON.stringify(body), headers: {...options.headers, "Content-Type": "application/json", "X-CSRF-Token": csrf(), "Idempotency-Key": id}});
    const response = await fetch("/monitoring/api/prices/admin/entry/" + path, options);
    let data;
    try { data = await response.json(); } catch { data = {}; }
    if (!response.ok) {
      if (response.status === 401) {
        $("login-panel").hidden = false;
        $("workspace").hidden = true;
      }
      const code = typeof data.detail === "object" ? data.detail.code : data.detail;
      const error = new Error(response.status === 401 ? "Войдите в портал, затем вернитесь к вводу цен." : response.status === 403 ? "Нет доступа к редактированию цен или сессия устарела. Войдите в портал заново." : errors[code] || "Сервис временно недоступен. Повторите загрузку позже.");
      error.code = code;
      error.status = response.status;
      throw error;
    }
    return data;
  }
  function validPrice(raw) { return raw === "" || (/^[0-9]{1,6}$/.test(raw) && Number(raw) <= 100000); }
  function value(raw) { return raw === "" ? null : Number(raw); }
  function key(row, field) { return row.key + "/" + field; }
  function edit(row, field, raw) {
    const k = key(row, field);
    if (validPrice(raw) && value(raw) === row[field]) state.edits.delete(k);
    else state.edits.set(k, {key: row.key, field, revision: row.revision, raw, row});
    updateSavebar();
  }
  function updateSavebar() {
    $("savebar").hidden = state.edits.size === 0;
    $("change-count").textContent = `Изменено цен: ${state.edits.size}`;
    $("review").disabled = state.busy || state.uncertain || state.edits.size > 200 || [...state.edits.values()].some(e => !validPrice(e.raw));
    $("discard").disabled = state.busy;
    $("selected-count").textContent = state.selected.size ? `Выбрано: ${state.selected.size}` : "";
    $("bulk").disabled = !state.selected.size || state.busy || state.uncertain;
    $("supplier").disabled = state.busy;
    $("refresh").disabled = state.busy;
  }
  function filter() {
    const words = $("search").value.toLowerCase().trim().split(/\s+/).filter(Boolean);
    const category = $("category").value, availability = $("availability").value;
    state.filtered = state.rows.filter(row => {
      if (category && row.category_name !== category) return false;
      const searchable = `${row.model_name} ${row.color} ${row.memory} ${row.product_id}`.toLowerCase();
      if (!words.every(w => searchable.includes(w))) return false;
      const priced = Number(row.price_1) > 0 || Number(row.price_12) > 0;
      if (availability === "priced" && !priced) return false;
      if (availability === "empty" && priced) return false;
      if (availability === "changed" && !["price_1", "price_12"].some(f => state.edits.has(key(row, f)))) return false;
      return true;
    });
    state.page = Math.min(state.page, Math.max(0, Math.ceil(state.filtered.length / PAGE_SIZE) - 1));
    renderRows();
  }
  function renderRows() {
    const slice = state.filtered.slice(state.page * PAGE_SIZE, (state.page + 1) * PAGE_SIZE);
    const fragment = document.createDocumentFragment();
    slice.forEach(row => {
      const tr = node("tr");
      if (state.selected.has(row.key)) tr.classList.add("row-selected");
      if (Object.keys(fields).some(f => state.edits.has(key(row, f)))) tr.classList.add("row-dirty");
      const checkCell = node("td"), check = node("input");
      check.type = "checkbox"; check.checked = state.selected.has(row.key);
      check.setAttribute("aria-label", "Выбрать " + row.model_name + " " + row.memory + " " + row.color);
      check.addEventListener("change", () => { check.checked ? state.selected.add(row.key) : state.selected.delete(row.key); renderRows(); });
      checkCell.append(check); tr.append(checkCell);
      const product = node("td"); product.append(node("div", row.model_name, "product-name"), node("div", "ID " + row.product_id + " · " + row.category_name, "product-meta")); tr.append(product);
      const variant = node("td"); variant.append(node("div", row.memory || "—", "variant-memory"), node("div", row.color || "—", "variant-color")); tr.append(variant);
      Object.keys(fields).forEach(field => {
        const td = node("td"), input = node("input");
        input.type = "text"; input.inputMode = "numeric"; input.autocomplete = "off"; input.maxLength = 6; input.placeholder = "—";
        input.setAttribute("aria-label", `${row.model_name} ${row.memory} ${row.color}, ${fields[field]}`);
        input.value = state.edits.get(key(row, field))?.raw ?? (row[field] ?? "");
        input.className = "price-input";
        input.classList.toggle("dirty", state.edits.has(key(row, field)));
        input.classList.toggle("invalid", !validPrice(input.value));
        input.disabled = row.locked.includes(field) || state.busy || state.uncertain;
        input.title = row.locked.includes(field) ? "Защищённая ячейка, формула или некорректные ID" : "Целая цена в USD. Пусто — нет предложения.";
        input.addEventListener("input", () => {
          edit(row, field, input.value.trim());
          input.classList.toggle("dirty", state.edits.has(key(row, field)));
          input.classList.toggle("invalid", !validPrice(input.value.trim()));
          tr.classList.toggle("row-dirty", Object.keys(fields).some(f => state.edits.has(key(row, f))));
        });
        input.addEventListener("keydown", event => { if (event.key === "Enter") { const all = [...document.querySelectorAll(".price-input:not(:disabled)")]; const next = all[all.indexOf(input) + 1]; if (next) next.focus(); } });
        input.addEventListener("paste", event => {
          const text = event.clipboardData.getData("text/plain");
          if (!/[\t\n]/.test(text.trim())) return;
          event.preventDefault();
          const lines = text.replace(/\r/g, "").replace(/\n$/, "").split("\n").map(line => line.split("\t"));
          const start = state.filtered.findIndex(r => r.key === row.key);
          const columns = field === "price_1" ? ["price_1", "price_12"] : ["price_12"];
          const pending = [], projected = new Set(state.edits.keys());
          for (let i = 0; i < lines.length; i++) {
            const target = state.filtered[start + i];
            if (!target || lines[i].length > columns.length) { notice("Вставка отменена: не хватает строк или выбрано больше ценовых колонок. Вставляйте только цены, без заголовков.", true); return; }
            for (let j = 0; j < lines[i].length; j++) {
              const raw = lines[i][j].trim(), col = columns[j];
              if (!validPrice(raw) || target.locked.includes(col)) { notice("Вставка отменена: некорректная цена или защищённая ячейка.", true); return; }
              pending.push([target, col, raw]); projected.add(key(target, col));
            }
          }
          if (projected.size > 200) { notice("Вставка отменена: за один раз можно изменить не больше 200 цен.", true); return; }
          pending.forEach(args => edit(...args)); renderRows();
          notice(`В черновик вставлено ячеек: ${pending.length}. Проверьте соответствие товарам перед сохранением.`);
        });
        const history = node("button", "◷ История", "cell-history-button");
        history.type = "button";
        history.setAttribute("aria-label", `История цены: ${row.model_name} ${row.memory} ${row.color}, ${fields[field]}`);
        history.title = "История сохранённых изменений этой цены";
        history.addEventListener("click", () => showCellHistory(row, field));
        td.append(input, history); tr.append(td);
      });
      tr.append(node("td", Number(row.min_price) > 0 ? number(row.min_price) : "—", "minimum"));
      fragment.append(tr);
    });
    $("rows").replaceChildren(fragment);
    $("found-count").textContent = `${number(state.filtered.length)} позиций`;
    $("empty").hidden = state.filtered.length !== 0;
    $("page-label").textContent = slice.length ? `${number(state.page * PAGE_SIZE + 1)}–${number(state.page * PAGE_SIZE + slice.length)} из ${number(state.filtered.length)}` : "0 позиций";
    $("previous").disabled = state.page === 0;
    $("next").disabled = (state.page + 1) * PAGE_SIZE >= state.filtered.length;
    $("select-page").checked = !!slice.length && slice.every(r => state.selected.has(r.key));
    $("select-page").indeterminate = slice.some(r => state.selected.has(r.key)) && !$("select-page").checked;
    updateSavebar();
  }
  async function load(sheetId) {
    state.busy = true; updateSavebar(); $("workspace").classList.add("loading"); notice("");
    try {
      const data = await api("catalog/" + sheetId);
      state.sheet = data.sheet_id; state.rows = data.rows; state.edits.clear(); state.selected.clear(); state.uncertain = false; state.page = 0;
      $("supplier").value = String(sheetId);
      $("total-count").textContent = number(data.rows.length);
      $("priced-count").textContent = number(data.rows.filter(r => Number(r.price_1) > 0 || Number(r.price_12) > 0).length);
      $("fetched-time").textContent = new Date(data.fetched_at).toLocaleTimeString("ru-RU", {hour: "2-digit", minute: "2-digit"});
      const previousCategory = $("category").value;
      $("category").replaceChildren(new Option("Все категории", ""), ...[...new Set(data.rows.map(r => r.category_name))].filter(Boolean).sort().map(c => new Option(c, c)));
      if ([...$("category").options].some(o => o.value === previousCategory)) $("category").value = previousCategory;
    } catch (error) {
      $("supplier").value = state.sheet === null ? "" : String(state.sheet);
      notice(error.message, true);
    } finally { state.busy = false; $("workspace").classList.remove("loading"); filter(); }
  }
  function mayDiscard() { return !state.edits.size || window.confirm("На странице есть несохранённые правки. Отменить их и загрузить актуальные цены?"); }
  function modal(title, content, buttons = []) {
    $("dialog-title").textContent = title;
    $("dialog-body").replaceChildren(...content);
    $("dialog-actions").replaceChildren(...buttons);
    $("dialog").showModal();
  }
  function button(text, action, primary = false) {
    const b = node("button", text, "button " + (primary ? "primary" : "secondary")); b.addEventListener("click", action); return b;
  }
  function showAddModel() {
    if (!mayDiscard()) return;
    const form = node("div", undefined, "catalog-form");
    const categoryLabel = node("label"), categoryTitle = node("span", "Категория"), category = node("select");
    category.append(...state.categories.map(item => new Option(item.name, String(item.category_id))), new Option("＋ Новая категория", "__new__"));
    categoryLabel.append(categoryTitle, category);
    const newCategoryLabel = node("label"), newCategoryTitle = node("span", "Название новой категории"), newCategory = node("input");
    newCategory.type = "text"; newCategory.maxLength = 200; newCategory.placeholder = "Например: Смартфоны бренда …";
    newCategoryLabel.append(newCategoryTitle, newCategory); newCategoryLabel.hidden = true;
    category.addEventListener("change", () => { newCategoryLabel.hidden = category.value !== "__new__"; if (!newCategoryLabel.hidden) newCategory.focus(); });
    const modelLabel = node("label"), modelTitle = node("span", "Название модели"), model = node("input");
    model.type = "text"; model.maxLength = 250; model.placeholder = "Например: Apple iPhone 18 Pro"; modelLabel.append(modelTitle, model);
    const variantsLabel = node("label"), variantsTitle = node("span", "Варианты: память | цвет"), variants = node("textarea");
    variants.rows = 7; variants.placeholder = "256 GB | Black\n256 GB | Silver\n512 GB | Black";
    variantsLabel.append(variantsTitle, variants, node("small", "Один вариант в строке. Если памяти или цвета нет, оставьте соответствующую сторону пустой:  | Black или 256 GB | ."));
    const status = node("p", "После создания модель появится у каждого поставщика без цены. ID товара и версий 1/12 месяцев создаст сервер.", "cell-history-note");
    form.append(categoryLabel, newCategoryLabel, modelLabel, variantsLabel, status);
    const create = button("Создать модель", async () => {
      const lines = variants.value.split(/\r?\n/).map(line => line.trim()).filter(Boolean);
      const parsed = [];
      for (const line of lines) {
        const separator = line.indexOf("|");
        if (separator < 0 || line.indexOf("|", separator + 1) >= 0) {
          status.textContent = "В каждой строке нужен один разделитель | между памятью и цветом."; status.classList.add("danger"); return;
        }
        parsed.push({memory: line.slice(0, separator).trim(), color: line.slice(separator + 1).trim()});
      }
      if (!model.value.trim() || !parsed.length || parsed.length > 100 || (category.value === "__new__" && !newCategory.value.trim())) {
        status.textContent = "Заполните модель, категорию и от 1 до 100 вариантов."; status.classList.add("danger"); return;
      }
      create.disabled = true; status.classList.remove("danger"); status.textContent = "Создаём модель…";
      try {
        const result = await api("products", {
          category_id: category.value === "__new__" ? null : Number(category.value),
          new_category_name: category.value === "__new__" ? newCategory.value.trim() : null,
          model_name: model.value.trim(), variants: parsed,
        }, crypto.randomUUID());
        $("dialog").close();
        const createdName = model.value.trim();
        const catalog = await api("categories"); state.categories = catalog.categories;
        await load(state.sheet);
        $("search").value = createdName; $("availability").value = "empty"; state.page = 0; filter();
        notice(`Создано вариантов: ${result.created_count}. Модель добавлена всем поставщикам с пустыми ценами и будет передана в worker при очередном импорте.`);
      } catch (error) { status.textContent = error.message; status.classList.add("danger"); create.disabled = false; }
    }, true);
    modal("Добавить новую модель", [form], [button("Отмена", () => $("dialog").close()), create]);
    model.focus();
  }
  function showCellHistory(row, field) {
    const sheet = state.sheet;
    const supplier = state.suppliers.find(s => s.sheet_id === sheet)?.name || String(sheet);
    const description = node("p", `${supplier} · ${row.model_name} · ${row.memory || "—"} · ${row.color || "—"} · ${fields[field]}`, "cell-history-description");
    const scope = node("p", "Только сохранённые изменения с сайта. Правки напрямую в Google Price до перехода на сайт в этот журнал не входили.", "cell-history-note");
    const saved = node("p", `Цена в загруженном каталоге: ${shownPrice(row[field])}${row[field] === null ? "" : " $"}.`, "cell-history-note");
    if (state.edits.has(key(row, field))) saved.append(node("strong", " Несохранённый черновик в историю не входит."));
    const list = node("div", undefined, "cell-history-list");
    const status = node("p", "Загружаем историю…", "cell-history-note");
    status.setAttribute("role", "status");
    const time = node("p", "", "cell-history-note");
    let cursor = 0, total = 0;
    const more = button("Показать ещё", loadPage);
    more.hidden = true;
    const close = button("Закрыть", () => $("dialog").close());
    modal("История цены", [description, saved, scope, time, list, status], [more, close]);
    async function loadPage() {
      more.disabled = true; status.textContent = "Загружаем историю…";
      try {
        const data = await api(`cell-history/${sheet}/${encodeURIComponent(row.key)}/${field}/${cursor}`);
        // The user may close this dialog and open a different cell while waiting.
        if (!list.isConnected || !$("dialog").open) return;
        time.textContent = "Часовой пояс: " + data.timezone;
        for (const entry of data.entries) {
          const item = node("div", undefined, "change-item cell-history-entry");
          const label = node("div", new Date(entry.created_at).toLocaleString("ru-RU", {timeZone: data.timezone}));
          const applied = entry.status === "applied";
          label.append(node("small", applied ? "Сохранено" : entry.status === "sending" ? "Результат пока не подтверждён" : "Сохранение не подтверждено — требуется проверка"));
          if (!applied) item.classList.add("history-unconfirmed");
          const values = node("div", undefined, "change-values");
          values.append(node("del", shownPrice(entry.before)), node("span", " → "), node("strong", shownPrice(entry.after)));
          item.append(label, values); list.append(item);
        }
        total += data.entries.length; cursor = data.next_before;
        status.textContent = total ? `Показано записей: ${total}.${cursor === null ? " Это вся сохранённая история ячейки." : ""}` : "Эту цену на сайте ещё не меняли.";
        more.textContent = "Показать ещё"; more.hidden = cursor === null;
      } catch (error) {
        if (!list.isConnected || !$("dialog").open) return;
        status.textContent = error.message; more.textContent = "Повторить загрузку"; more.hidden = false;
      } finally { more.disabled = false; }
    }
    loadPage();
  }
  function changesList(edits) {
    return edits.map(e => {
      const item = node("div", undefined, "change-item"), name = node("div", e.row.model_name);
      name.append(node("small", `${e.row.memory} · ${e.row.color} · ${fields[e.field]}`));
      const values = node("div", undefined, "change-values"); values.append(node("del", shownPrice(e.row[e.field])), node("span", " → "), node("strong", shownPrice(value(e.raw))));
      item.append(name, values); return item;
    });
  }
  $("review").addEventListener("click", () => {
    const edits = [...state.edits.values()];
    if (!edits.length || edits.length > 200 || edits.some(e => !validPrice(e.raw))) return;
    const operation = crypto.randomUUID();
    const save = button("Сохранить цены", async () => {
      save.disabled = true; state.busy = true; updateSavebar(); $("dialog").close(); renderRows();
      try {
        const result = await api("save/" + state.sheet, {changes: edits.map(e => ({key: e.key, field: e.field, revision: e.revision, value: value(e.raw)}))}, operation);
        state.edits.clear(); state.busy = false;
        await load(state.sheet);
        notice(`Сохранено цен: ${result.changed}. ${state.source === "sqlite" ? "Цены записаны на сервере, Google Price не используется." : "Google Sheets обновлена."} Прайс подхватит изменения при очередном плановом импорте.`);
      } catch (error) {
        // A proxy timeout/network loss may follow a successful Sheets write.
        if (!error.status || error.status >= 500 || error.code === "save_outcome_unknown") {
          state.uncertain = true;
          notice(errors.save_outcome_unknown + "\nНомер операции: " + operation, true);
        } else notice(error.message, true);
      } finally { state.busy = false; renderRows(); }
    }, true);
    modal(`Проверка · ${edits.length} цен`, [node("p", "Будут изменены только перечисленные цены выбранного поставщика. Остальные ячейки останутся без изменений."), ...changesList(edits)], [button("Вернуться", () => $("dialog").close()), save]);
  });
  $("bulk").addEventListener("click", () => {
    const wrap = node("div", undefined, "bulk-fields"), label = node("label", "Гарантия"), field = node("select");
    field.append(new Option("1 месяц", "price_1"), new Option("12 месяцев", "price_12")); label.append(field);
    const priceLabel = node("label", "Новая цена, USD"), input = node("input"); input.type = "text"; input.inputMode = "numeric"; input.placeholder = "Пусто — убрать предложение"; priceLabel.append(input); wrap.append(label, priceLabel);
    const warning = node("p", `Выбрано товаров: ${state.selected.size}. Изменения сначала появятся в черновике. Защищённые ячейки будут пропущены.`);
    modal("Одна цена для выбранных", [warning, wrap], [button("Отмена", () => $("dialog").close()), button("Применить к черновику", () => {
      const raw = input.value.trim(); if (!validPrice(raw)) { input.setCustomValidity(errors.invalid_price); input.reportValidity(); return; }
      const targets = state.rows.filter(r => state.selected.has(r.key) && !r.locked.includes(field.value));
      const projected = new Set(state.edits.keys()); targets.forEach(r => projected.add(key(r, field.value)));
      if (projected.size > 200) { warning.textContent = "Слишком много изменений. Выберите не более 200 цен за один раз."; return; }
      targets.forEach(r => edit(r, field.value, raw)); $("dialog").close(); filter();
    }, true)]);
  });
  $("history").addEventListener("click", async () => {
    try {
      const data = await api("history");
      const content = data.operations.map(op => {
        const detail = node("details", undefined, "history-item");
        const supplier = state.suppliers.find(s => s.sheet_id === op.sheet_id)?.name || String(op.sheet_id);
        const status = op.status === "applied" ? "Сохранено" : "Результат требует проверки";
        detail.append(node("summary", `${new Date(op.created_at).toLocaleString("ru-RU")} · ${supplier} · ${op.changes.length} цен · ${status}`));
        detail.append(node("small", "Операция: " + op.operation_id));
        op.changes.forEach(c => detail.append(node("div", `${c.model} · ${c.memory} · ${c.color} · ${fields[c.field]}: ${shownPrice(c.before)} → ${shownPrice(c.after)}`)));
        return detail;
      });
      modal("История сохранений сайта", [node("p", "Последние 100 операций: поставщик, прежняя и новая цена, время сохранения."), ...(content.length ? content : [node("p", "Сохранений с сайта пока нет.")])], [button("Закрыть", () => $("dialog").close())]);
    } catch (error) { notice(error.message, true); }
  });
  $("close-dialog").addEventListener("click", () => $("dialog").close());
  $("history-mobile").addEventListener("click", () => $("history").click());
  $("add-model").addEventListener("click", showAddModel);
  $("supplier").addEventListener("change", () => { if (mayDiscard()) load(Number($("supplier").value)); else $("supplier").value = String(state.sheet); });
  $("refresh").addEventListener("click", () => { if (state.sheet !== null && mayDiscard()) load(state.sheet); });
  $("discard").addEventListener("click", () => { if (mayDiscard()) { state.edits.clear(); filter(); } });
  ["search", "category", "availability"].forEach(id => $(id).addEventListener(id === "search" ? "input" : "change", () => { state.page = 0; filter(); }));
  $("previous").addEventListener("click", () => { state.page--; renderRows(); });
  $("next").addEventListener("click", () => { state.page++; renderRows(); });
  $("select-page").addEventListener("change", () => { state.filtered.slice(state.page * PAGE_SIZE, (state.page + 1) * PAGE_SIZE).forEach(r => $("select-page").checked ? state.selected.add(r.key) : state.selected.delete(r.key)); renderRows(); });
  window.addEventListener("beforeunload", e => { if (state.edits.size || state.busy) { e.preventDefault(); e.returnValue = ""; } });
  document.addEventListener("keydown", e => { if (e.key === "/" && !["INPUT", "SELECT", "TEXTAREA"].includes(document.activeElement.tagName)) { e.preventDefault(); $("search").focus(); } });
  async function init() {
    notice("Загружаем цены…");
    try {
      const data = await api("suppliers"); state.suppliers = data.suppliers;
      state.source = data.source;
      $("source-mode").textContent = data.source === "sqlite" ? "Самостоятельный режим" : "Переходный режим";
      $("source-title").textContent = data.source === "sqlite" ? "Цены на сервере" : "Сайт + Google Sheets";
      $("source-note").textContent = data.source === "sqlite" ? "Google Price больше не нужен. Цены и история сохраняются в базе с резервным копированием." : "Два интерфейса. Один источник цен. Таблица продолжает работать.";
      if (!data.suppliers.length) throw new Error("Поставщики не найдены.");
      $("supplier").replaceChildren(...data.suppliers.map(s => new Option(s.name, s.sheet_id)));
      try {
        const catalog = await api("categories");
        state.categories = catalog.categories;
        $("add-model").hidden = false;
      } catch (error) {
        $("add-model").hidden = true;
        if (error.code !== "catalog_management_not_initialized") throw error;
      }
      $("workspace").hidden = false;
      await load(data.suppliers[0].sheet_id);
    } catch (error) { notice(error.message, true); }
  }
  init();
})();

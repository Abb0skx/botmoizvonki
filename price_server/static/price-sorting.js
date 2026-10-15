(() => {
  "use strict";
  const $ = id => document.getElementById(id);
  const KEY = "texnikach:sorting-draft:v1";
  const RECOVERY_KEY = "texnikach:sorting-recovery:v1";
  const kinds = {
    models: {groups: "КАТЕГОРИИ МОДЕЛЕЙ", item: "МОДЕЛИ В КАТЕГОРИИ", placeholder: "Найти категорию…"},
    memories: {groups: "ГРУППЫ ПАМЯТИ", item: "ПАМЯТЬ И РАЗМЕРЫ", placeholder: "Найти группу…"},
    colors: {groups: "СЕМЕЙСТВА ЦВЕТОВ", item: "ЦВЕТА В СЕМЕЙСТВЕ", placeholder: "Найти семейство…"},
    categories: {groups: "ГРУППЫ КАТЕГОРИЙ", item: "КАТЕГОРИИ В ПРАЙСЕ", placeholder: "Найти группу…"},
  };
  const state = {data: null, kind: "models", groupId: null, orders: new Map(), reviewed: new Set(), undo: [], pending: null, conflict: false, busy: false, loading: false, refreshing: false, onlyNew: false, moved: null, recovery: null, drag: null};
  const node = (tag, text, className) => { const result = document.createElement(tag); if (text !== undefined) result.textContent = text; if (className) result.className = className; return result; };
  const number = value => Number(value || 0).toLocaleString("ru-RU");
  const equal = (first, second) => first.length === second.length && first.every((value, index) => value === second[index]);
  const normalize = value => String(value ?? "").toLocaleLowerCase("ru").replace(/ё/g, "е").trim();
  const matches = (text, query) => normalize(query).split(/\s+/).filter(Boolean).every(word => normalize(text).includes(word));
  const groups = () => (state.data?.groups || []).filter(group => group.kind === state.kind).sort((a, b) => a.position - b.position);
  const group = () => state.data?.groups.find(value => value.id === state.groupId);
  const items = groupId => (state.data?.items || []).filter(item => item.group_id === groupId).sort((a, b) => a.position - b.position);
  const baseIds = groupId => items(groupId).map(item => item.id);
  const ids = groupId => state.orders.get(groupId) || baseIds(groupId);
  const dirtyGroups = () => new Set([...state.orders.keys(), ...state.reviewed]);
  const locked = () => state.busy || state.loading || !!state.pending;
  function stored(key) { try { return JSON.parse(sessionStorage.getItem(key) || "null"); } catch { return null; } }
  function store(key, value) { try { if (value) sessionStorage.setItem(key, JSON.stringify(value)); else sessionStorage.removeItem(key); } catch { /* Sorting remains usable when session storage is unavailable. */ } }
  function notice(text, error = false) { $("notice").textContent = text; $("notice").className = "notice" + (error ? " error" : ""); $("notice").hidden = !text; }
  function announce(text) { $("announcement").textContent = text; }
  function date(value) { if (!value) return "—"; const parsed = new Date(value); return Number.isNaN(parsed.valueOf()) ? String(value) : parsed.toLocaleString("ru-RU", {day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit"}); }
  function operationId() {
    if (globalThis.crypto?.randomUUID) return crypto.randomUUID();
    return "xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx".replace(/[xy]/g, value => { const random = Math.random() * 16 | 0; return (value === "x" ? random : random & 3 | 8).toString(16); });
  }
  function csrf() { const value = document.cookie.split("; ").find(item => item.startsWith("__Host-texnikach_monitoring_csrf=")); return value ? decodeURIComponent(value.slice(value.indexOf("=") + 1)) : ""; }
  async function api(body, operation) {
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 40000);
    const options = {credentials: "same-origin", cache: "no-store", signal: controller.signal, headers: {Accept: "application/json", "X-Requested-With": "XMLHttpRequest"}};
    if (body !== undefined) Object.assign(options, {method: "POST", body: JSON.stringify(body), headers: {...options.headers, "Content-Type": "application/json", "X-CSRF-Token": csrf(), "Idempotency-Key": operation}});
    try {
      const response = await fetch("/monitoring/api/prices/admin/entry/sorting", options);
      let data;
      try { data = await response.json(); } catch { const error = new Error("Сервер вернул неполный ответ."); error.uncertain = true; throw error; }
      if (!response.ok) {
        const detail = data.detail || {}, code = typeof detail === "string" ? detail : detail.code;
        const error = new Error(response.status === 401 ? "Войдите в рабочий кабинет. Черновик сохранён в этой вкладке." : response.status === 403 ? "Сессия устарела или нет права управления прайсом. Войдите в кабинет заново." : response.status === 409 ? "Каталог или порядок уже изменились. Ваш черновик сохранён; сначала загрузите свежие данные." : typeof detail.message === "string" ? detail.message : "Не удалось сохранить порядок. Проверьте соединение и повторите.");
        error.code = code; error.status = response.status; error.uncertain = response.status >= 500;
        if (response.status === 401) { $("login-panel").hidden = false; $("workspace").hidden = true; $("savebar").hidden = true; }
        throw error;
      }
      return data;
    } finally { clearTimeout(timeout); }
  }
  function button(text, action, primary = false) { const value = node("button", text, "button " + (primary ? "primary" : "secondary")); value.type = "button"; value.addEventListener("click", action); return value; }
  function modal(title, content, actions) { $("dialog-title").textContent = title; $("dialog-body").replaceChildren(...content); $("dialog-actions").replaceChildren(...actions); if (!$("dialog").open) $("dialog").showModal(); }
  function persist() {
    store(KEY, state.data && (dirtyGroups().size || state.pending) ? {revision: state.data.revision, orders: [...state.orders], reviewed: [...state.reviewed], pending: state.pending, conflict: state.conflict, kind: state.kind, groupId: state.groupId} : null);
    store(RECOVERY_KEY, state.recovery);
  }
  function undoSnapshot() { state.undo.push({orders: [...state.orders].map(([key, value]) => [key, [...value]]), reviewed: [...state.reviewed]}); if (state.undo.length > 30) state.undo.shift(); }
  function setOrder(groupId, values) { if (equal(values, baseIds(groupId))) state.orders.delete(groupId); else state.orders.set(groupId, values); }
  function renderStatus() {
    const changed = dirtyGroups().size;
    $("change-count").textContent = state.busy ? "Сохраняем порядок…" : state.pending ? "Нужно подтвердить сохранение" : state.conflict ? "Список изменился · черновик сохранён" : changed ? `Есть правки · групп: ${number(changed)}` : "Все изменения сохранены";
    $("save-hint").textContent = state.pending ? "Повторная проверка использует тот же номер операции" : state.conflict ? "Нажмите «Обновить список», затем проверьте прежние правки" : changed ? "Черновик в этой вкладке · на сервере порядок пока прежний" : `Последнее обновление: ${date(state.data?.updated_at)} · порядок на сервере`;
    $("save-status-mark").classList.toggle("dirty", !!changed || !!state.pending);
    $("save").disabled = state.busy || state.loading || state.conflict || (!changed && !state.pending);
    $("save").textContent = state.busy ? "Сохраняем…" : state.pending ? "Проверить сохранение" : "Сохранить порядок";
    $("undo").disabled = locked() || !state.undo.length;
    $("discard").disabled = locked() || !changed;
    $("refresh").disabled = state.busy || state.loading || !!state.pending;
    $("reset-group").disabled = locked() || !dirtyGroups().has(state.groupId);
    $("group-saved").textContent = dirtyGroups().has(state.groupId) ? "Есть правки" : "Сохранено";
    $("group-saved").classList.toggle("dirty", dirtyGroups().has(state.groupId));
    $("recovery-panel").hidden = !state.recovery;
    $("recover-draft").disabled = locked() || state.conflict;
    $("dismiss-recovery").disabled = locked();
  }
  function renderGroups() {
    const current = groups(), query = $("group-search").value;
    $("groups-label").textContent = kinds[state.kind].groups;
    $("group-search").placeholder = kinds[state.kind].placeholder;
    $("groups-count").textContent = number(current.length);
    const filtered = current.filter(value => matches(value.title, query));
    $("group-list").replaceChildren(...filtered.map(value => {
      const row = node("button", undefined, "group-button" + (state.groupId === value.id ? " active" : "")); row.type = "button";
      row.setAttribute("aria-current", state.groupId === value.id ? "true" : "false");
      row.append(node("span", value.title, "group-name"));
      const newCount = state.reviewed.has(value.id) ? 0 : items(value.id).filter(item => item.is_new).length;
      if (newCount) { const dot = node("span", undefined, "new-dot"); dot.title = `${newCount} новых позиций`; dot.setAttribute("aria-label", `${newCount} новых позиций`); row.append(dot); }
      if (dirtyGroups().has(value.id)) { const mark = node("span", "•", "changed-mark"); mark.title = "Есть несохранённые правки"; row.append(mark); }
      row.append(node("span", number(value.count ?? items(value.id).length), "group-count"));
      row.addEventListener("click", () => { state.groupId = value.id; state.onlyNew = false; $("item-search").value = ""; renderGroups(); renderItems(); persist(); });
      return row;
    }));
    $("groups-empty").hidden = !!filtered.length;
    for (const tab of document.querySelectorAll("[data-kind]")) {
      const active = tab.dataset.kind === state.kind; tab.classList.toggle("active", active); tab.setAttribute("aria-selected", String(active)); tab.tabIndex = active ? 0 : -1;
    }
    for (const count of document.querySelectorAll("[data-count]")) { const groupIds = new Set((state.data?.groups || []).filter(value => value.kind === count.dataset.count).map(value => value.id)); count.textContent = number((state.data?.items || []).filter(item => groupIds.has(item.group_id)).length); }
    $("sorting-panel").setAttribute("aria-labelledby", `tab-${state.kind}`);
  }
  function rowAction(label, title, action, disabled = false) { const value = node("button", label, "row-button"); value.type = "button"; value.title = title; value.setAttribute("aria-label", title); value.disabled = disabled || locked(); value.addEventListener("click", action); return value; }
  function move(itemId, position, focus = true) {
    if (locked()) return;
    const values = [...ids(state.groupId)], from = values.indexOf(itemId), to = Math.max(0, Math.min(values.length - 1, position));
    if (from < 0 || from === to) return;
    undoSnapshot(); values.splice(from, 1); values.splice(to, 0, itemId); setOrder(state.groupId, values); state.moved = itemId; persist(); renderGroups(); renderItems();
    const item = state.data.items.find(value => value.id === itemId); announce(`${item?.name || "Позиция"}: место ${to + 1} из ${values.length}.`);
    if (focus) requestAnimationFrame(() => [...$("order-list").children].find(row => row.dataset.id === itemId)?.querySelector(".position-button")?.focus({preventScroll: true}));
  }
  function moveDialog(item) {
    if (locked()) return;
    const values = ids(state.groupId), current = values.indexOf(item.id) + 1;
    const name = node("p", item.name, "move-name"), field = node("label", undefined, "move-field"), input = node("input"), error = node("p", "", "dialog-error");
    input.type = "number"; input.min = "1"; input.max = String(values.length); input.step = "1"; input.value = String(current); input.inputMode = "numeric"; input.id = "move-position";
    field.htmlFor = input.id; field.append(node("span", `Новая позиция от 1 до ${values.length}`), input);
    const quick = node("div", undefined, "move-quick"); quick.append(button("В начало ↑", () => { input.value = "1"; input.focus(); }), button("В конец ↓", () => { input.value = String(values.length); input.focus(); }));
    const apply = () => { const position = Number(input.value); if (!Number.isInteger(position) || position < 1 || position > values.length) { error.textContent = `Укажите целое число от 1 до ${values.length}.`; input.focus(); return; } $("dialog").close(); move(item.id, position - 1); };
    input.addEventListener("keydown", event => { if (event.key === "Enter") { event.preventDefault(); apply(); } });
    modal("Переместить позицию", [name, field, quick, node("p", "Перемещение действует только внутри выбранной группы. Остальные позиции сдвинутся автоматически.", "move-note"), error], [button("Отмена", () => $("dialog").close()), button("Переместить", apply, true)]);
    input.focus(); input.select();
  }
  function renderItems() {
    const selected = group(), groupItems = items(state.groupId), lookup = new Map(groupItems.map(item => [item.id, item]));
    const order = ids(state.groupId), query = $("item-search").value;
    const newCount = state.reviewed.has(state.groupId) ? 0 : groupItems.filter(item => item.is_new).length;
    const filtered = order.map(id => lookup.get(id)).filter(Boolean).filter(item => matches(item.name + " " + (item.canonical || ""), query) && (!state.onlyNew || (item.is_new && !state.reviewed.has(state.groupId))));
    const draggable = !locked() && !query.trim() && !state.onlyNew;
    $("group-kind").textContent = kinds[state.kind].item;
    $("category-scope-note").hidden = state.kind !== "categories";
    $("group-title").textContent = selected?.title || "Выберите группу";
    $("group-meta").textContent = `${number(order.length)} позиций${newCount ? ` · ${number(newCount)} новых для проверки` : " · ваш порядок отображения"}`;
    $("group-new-count").textContent = number(newCount);
    $("only-new").setAttribute("aria-pressed", String(state.onlyNew));
    $("new-items-note").hidden = !newCount;
    $("new-items-text").textContent = `Новых позиций: ${number(newCount)}. Переместите их или подтвердите текущий порядок.`;
    $("acknowledge-group").disabled = locked();
    $("order-list").replaceChildren(...filtered.map(item => {
      const index = order.indexOf(item.id), fresh = item.is_new && !state.reviewed.has(state.groupId);
      const row = node("li", undefined, "sort-row" + (fresh ? " new-row" : "") + (state.moved === item.id ? " moved" : "")); row.dataset.id = item.id;
      const handle = node("button", "⠿", "drag-handle"); handle.type = "button"; handle.draggable = draggable; handle.disabled = !draggable;
      handle.title = draggable ? "Перетащить. С клавиатуры: Alt + ↑ / ↓" : "Перетаскивание доступно без фильтров"; handle.setAttribute("aria-label", `Переместить ${item.name}. Alt и стрелка вверх или вниз`);
      handle.addEventListener("keydown", event => { if (event.altKey && ["ArrowUp", "ArrowDown"].includes(event.key)) { event.preventDefault(); move(item.id, index + (event.key === "ArrowUp" ? -1 : 1)); } });
      handle.addEventListener("dragstart", event => { state.drag = {id: item.id, groupId: state.groupId}; event.dataTransfer.effectAllowed = "move"; event.dataTransfer.setData("text/plain", item.id); event.dataTransfer.setDragImage(row, 30, 24); row.classList.add("dragging"); });
      handle.addEventListener("dragend", () => { state.drag = null; document.querySelectorAll(".dragging,.drop-before").forEach(value => value.classList.remove("dragging", "drop-before")); });
      row.addEventListener("dragover", event => { if (!draggable || state.drag?.groupId !== state.groupId || state.drag.id === item.id) return; event.preventDefault(); event.dataTransfer.dropEffect = "move"; row.classList.add("drop-before"); });
      row.addEventListener("dragleave", event => { if (!row.contains(event.relatedTarget)) row.classList.remove("drop-before"); });
      row.addEventListener("drop", event => { if (!draggable || state.drag?.groupId !== state.groupId) return; event.preventDefault(); const source = state.drag.id, from = order.indexOf(source); state.drag = null; move(source, index - (from < index ? 1 : 0)); });
      const position = node("button", String(index + 1).padStart(2, "0"), "position-button"); position.type = "button"; position.title = "Указать номер позиции"; position.setAttribute("aria-label", `${item.name}: позиция ${index + 1}. Указать другое место`); position.disabled = locked(); position.addEventListener("click", () => moveDialog(item));
      const content = node("div"), name = node("div", undefined, "item-name"); name.append(node("span", item.name)); if (fresh) name.append(node("span", "Новое", "new-label")); content.append(name);
      if (item.canonical && item.canonical !== item.name) content.append(node("small", `В прайсе: ${item.canonical}`, "item-secondary"));
      if (item.active === false) content.append(node("small", "Пока нет в действующем каталоге", "item-secondary"));
      const actions = node("div", undefined, "row-actions"); actions.append(rowAction("↑", `Поднять ${item.name}`, () => move(item.id, index - 1), index === 0), rowAction("↓", `Опустить ${item.name}`, () => move(item.id, index + 1), index === order.length - 1), rowAction("⋯", `Указать место для ${item.name}`, () => moveDialog(item)));
      row.append(handle, position, content, actions); return row;
    }));
    $("items-empty").hidden = !!filtered.length;
    $("empty-title").textContent = order.length ? "Ничего не найдено" : "Здесь пока нет позиций";
    $("empty-text").textContent = order.length ? "Измените запрос или выключите фильтр «Только новые»." : "Когда в каталоге появится новая позиция, она автоматически попадёт в конец этой группы.";
    $("clear-filters").hidden = !order.length;
    $("list-total").textContent = filtered.length === order.length ? `Всего позиций: ${number(order.length)}` : `Показано ${number(filtered.length)} из ${number(order.length)} · номер — место в полном списке`;
    state.moved = null; renderStatus();
  }
  function validateData(data) {
    if (!data || !Number.isInteger(data.revision) || !Array.isArray(data.groups) || !Array.isArray(data.items)
      || data.groups.some(value => !value || typeof value.id !== "string" || typeof value.title !== "string" || !kinds[value.kind] || !Number.isFinite(value.position))
      || data.items.some(value => !value || typeof value.id !== "string" || typeof value.group_id !== "string" || typeof value.name !== "string" || !Number.isFinite(value.position))) throw new Error("Получен неполный список сортировки. Обновите страницу позже.");
    const groupIds = new Set(data.groups.map(value => value.id));
    if (groupIds.size !== data.groups.length || new Set(data.items.map(value => value.id)).size !== data.items.length || data.items.some(value => !groupIds.has(value.group_id))) throw new Error("Получен несогласованный список сортировки. Черновик не изменён.");
  }
  function acceptData(data) {
    validateData(data);
    state.data = data;
    if (!kinds[state.kind]) state.kind = "models";
    if (!groups().some(value => value.id === state.groupId)) state.groupId = groups()[0]?.id || null;
    $("loading").hidden = true; $("login-panel").hidden = true; $("workspace").hidden = false; $("savebar").hidden = false;
  }
  function restoreDraft(draft) {
    if (!draft || !Array.isArray(draft.orders)) return;
    state.kind = kinds[draft.kind] ? draft.kind : "models"; state.groupId = draft.groupId;
    const valid = draft.orders.every(([groupId, order]) => Array.isArray(order) && equal([...order].sort(), baseIds(groupId).sort()));
    if (draft.pending?.operation && draft.pending?.body) {
      state.pending = draft.pending;
      if (valid) { state.orders = new Map(draft.orders); state.reviewed = new Set(draft.reviewed || []); }
      else { state.recovery = draft; store(RECOVERY_KEY, draft); }
      notice(`Есть операция без подтверждённого ответа. Нажмите «Проверить сохранение»: запрос будет повторён с тем же номером и не создаст повторное изменение.\nОперация: ${state.pending.operation}`, true);
    } else if (!draft.conflict && draft.revision === state.data.revision && valid) { state.orders = new Map(draft.orders); state.reviewed = new Set(draft.reviewed || []); notice("Черновик восстановлен. Проверьте порядок и сохраните, когда будете готовы."); }
    else { state.recovery = draft; store(RECOVERY_KEY, draft); store(KEY, null); }
    if (!groups().some(value => value.id === state.groupId)) state.groupId = groups()[0]?.id || null;
  }
  async function load(restore = false) {
    if (state.loading) return;
    state.loading = true;
    if (state.data) renderItems();
    try {
      const data = await api(); acceptData(data);
      if (restore) { state.recovery = stored(RECOVERY_KEY); restoreDraft(stored(KEY)); }
      renderGroups(); renderItems(); persist();
    } catch (error) { $("loading").hidden = true; notice(error.message || "Не удалось загрузить сортировку. Попробуйте обновить список.", true); }
    finally { state.loading = false; if (state.data) { renderGroups(); renderItems(); } }
  }
  async function refreshQuietly() {
    if (!state.data || state.busy || state.loading || state.refreshing || state.pending || dirtyGroups().size || document.hidden || $("dialog").open || state.drag) return;
    const revision = state.data.revision;
    state.refreshing = true;
    try {
      const data = await api();
      // Never replace edits made while the background request was in flight.
      if (state.busy || state.loading || state.pending || dirtyGroups().size || state.data.revision !== revision || state.drag || $("dialog").open) return;
      if (data.revision !== revision) { acceptData(data); renderGroups(); renderItems(); announce("Список обновлён. Новые позиции добавлены в конец своих групп."); }
    } catch { /* A background refresh must not interrupt work or discard the draft. */ }
    finally { state.refreshing = false; }
  }
  function clearDraft() { state.orders.clear(); state.reviewed.clear(); state.undo = []; state.pending = null; state.conflict = false; persist(); }
  function refresh() {
    if (locked()) return;
    if (!dirtyGroups().size) { state.conflict = false; notice(""); load(); return; }
    modal("Обновить список?", [node("p", "Несохранённый порядок останется в черновике. После загрузки свежего списка его можно будет проверить и вернуть.")], [button("Продолжить редактировать", () => $("dialog").close()), button("Обновить список", async () => { state.recovery = {revision: state.data.revision, orders: [...state.orders], reviewed: [...state.reviewed], kind: state.kind, groupId: state.groupId}; clearDraft(); $("dialog").close(); await load(); }, true)]);
  }
  function showConflict() {
    modal("Порядок уже изменился", [node("p", "В другой вкладке изменили сортировку или в каталог поступили новые позиции. Мы не перезапишем свежие данные автоматически."), node("p", "Загрузите актуальный список: ваши правки останутся отдельным черновиком. Затем можно проверить и вернуть их перед сохранением.")], [button("Закрыть", () => $("dialog").close()), button("Обновить, сохранив черновик", () => { $("dialog").close(); refresh(); }, true)]);
  }
  async function save() {
    if (state.busy || state.loading || state.conflict || (!dirtyGroups().size && !state.pending)) return;
    if (!state.pending) state.pending = {operation: operationId(), body: {revision: state.data.revision, groups: [...dirtyGroups()].map(groupId => ({group_id: groupId, item_ids: ids(groupId)}))}};
    state.busy = true; persist(); renderItems();
    try {
      const result = await api(state.pending.body, state.pending.operation);
      validateData(result);
      if (state.recovery?.pending?.operation === state.pending.operation) state.recovery = null;
      clearDraft(); acceptData(result); notice("Порядок сохранён. Он будет использован при следующем формировании прайса. Цены и ID товаров не изменены.");
      // A replay can return the original saved revision, so refresh current catalogue membership.
      try { acceptData(await api()); } catch { notice("Порядок сохранён. Свежий список пока не удалось загрузить — обновите его позже."); }
    } catch (error) {
      const pendingId = state.pending?.operation;
      if (error.status === 409) { state.pending = null; state.conflict = true; notice(error.message, true); showConflict(); }
      else if ([401, 403].includes(error.status)) notice(error.message, true);
      else if (error.status && error.status < 500 && !error.uncertain) { state.pending = null; notice(error.message, true); }
      else notice(`Не удалось подтвердить сохранение. Правки не потеряны. Нажмите «Проверить сохранение» — будет проверена та же операция.\nНомер операции: ${pendingId}`, true);
    } finally { state.busy = false; persist(); renderGroups(); renderItems(); }
  }
  function recover() {
    if (!state.recovery || locked()) return;
    const draft = state.recovery, restored = new Map(), missing = [];
    for (const [groupId, order] of draft.orders || []) {
      const current = baseIds(groupId); if (!current.length) { missing.push(groupId); continue; }
      const kept = order.filter(id => current.includes(id)), seen = new Set(kept);
      restored.set(groupId, [...kept, ...current.filter(id => !seen.has(id))]);
    }
    const summary = node("p", `Будут возвращены ваши правки в ${restored.size} группах. Новые позиции останутся в конце; удалённые из каталога позиции не восстанавливаются.`);
    const warning = node("p", "Порядок в этих группах может отличаться от изменений другого сотрудника. После восстановления проверьте список: на сервер ничего не отправится до нажатия «Сохранить порядок».");
    const actions = [button("Отмена", () => $("dialog").close())];
    if (restored.size || draft.reviewed?.length) actions.push(button("Вернуть в черновик", () => { undoSnapshot(); for (const [groupId, order] of restored) setOrder(groupId, order); for (const groupId of draft.reviewed || []) if (state.data.groups.some(value => value.id === groupId)) state.reviewed.add(groupId); state.recovery = null; persist(); $("dialog").close(); renderGroups(); renderItems(); notice("Правки возвращены в черновик. Проверьте порядок перед сохранением."); }, true));
    modal("Проверить прежний черновик", [summary, warning, ...(missing.length ? [node("p", `Групп больше нет в каталоге: ${missing.length}. Они пропущены.`, "move-note")] : [])], actions);
  }
  function history() {
    const entries = state.data?.history || [];
    const content = entries.length ? entries.slice(0, 30).map(item => {
      const row = node("article", undefined, "sorting-history-row"); row.append(node("strong", date(item.created_at)), node("p", typeof item.summary === "string" ? item.summary : item.group_titles?.join(", ") || (item.action === "save" ? "Порядок обновлён" : "Список сортировки обновлён")));
      if (item.operation_id) row.append(node("small", "Операция: " + item.operation_id));
      return row;
    }) : [node("p", "История появится после первых изменений сортировки.")];
    modal("История сортировки", content, [button("Закрыть", () => $("dialog").close())]);
  }
  function setKind(kind) { state.kind = kind; state.groupId = groups()[0]?.id || null; state.onlyNew = false; $("group-search").value = ""; $("item-search").value = ""; renderGroups(); renderItems(); persist(); }
  for (const tab of document.querySelectorAll("[data-kind]")) {
    tab.addEventListener("click", () => setKind(tab.dataset.kind));
    tab.addEventListener("keydown", event => {
      if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
      event.preventDefault(); const keys = Object.keys(kinds), index = keys.indexOf(state.kind), next = event.key === "Home" ? 0 : event.key === "End" ? keys.length - 1 : (index + (event.key === "ArrowRight" ? 1 : -1) + keys.length) % keys.length;
      setKind(keys[next]); $(`tab-${keys[next]}`).focus();
    });
  }
  $("group-search").addEventListener("input", renderGroups);
  $("item-search").addEventListener("input", renderItems);
  $("only-new").addEventListener("click", () => { state.onlyNew = !state.onlyNew; renderItems(); });
  $("clear-filters").addEventListener("click", () => { $("item-search").value = ""; state.onlyNew = false; renderItems(); });
  $("acknowledge-group").addEventListener("click", () => { if (locked() || !state.groupId) return; undoSnapshot(); state.reviewed.add(state.groupId); state.onlyNew = false; persist(); renderGroups(); renderItems(); announce("Группа отмечена проверенной. Сохраните изменения."); });
  $("reset-group").addEventListener("click", () => { if (locked()) return; undoSnapshot(); state.orders.delete(state.groupId); state.reviewed.delete(state.groupId); persist(); renderGroups(); renderItems(); });
  $("undo").addEventListener("click", () => { if (locked() || !state.undo.length) return; const previous = state.undo.pop(); state.orders = new Map(previous.orders); state.reviewed = new Set(previous.reviewed); persist(); renderGroups(); renderItems(); announce("Последнее изменение отменено."); });
  $("discard").addEventListener("click", () => { if (locked()) return; modal("Сбросить все правки?", [node("p", "Все несохранённые перемещения будут отменены. На сервере порядок останется прежним.")], [button("Продолжить редактировать", () => $("dialog").close()), button("Сбросить правки", () => { clearDraft(); $("dialog").close(); renderGroups(); renderItems(); })]); });
  $("refresh").addEventListener("click", refresh);
  $("save").addEventListener("click", save);
  $("show-history").addEventListener("click", history);
  $("recover-draft").addEventListener("click", recover);
  $("dismiss-recovery").addEventListener("click", () => { state.recovery = null; persist(); renderStatus(); });
  $("close-dialog").addEventListener("click", () => $("dialog").close());
  window.addEventListener("beforeunload", event => { if (dirtyGroups().size || state.pending) { event.preventDefault(); event.returnValue = ""; } });
  document.addEventListener("keydown", event => { if (event.key === "/" && !$("dialog").open && !["INPUT", "TEXTAREA", "SELECT"].includes(document.activeElement?.tagName)) { event.preventDefault(); $("item-search").focus(); } });
  document.addEventListener("visibilitychange", () => { if (!document.hidden) refreshQuietly(); });
  setInterval(refreshQuietly, 60000);
  load(true);
})();

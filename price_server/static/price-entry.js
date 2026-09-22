(() => {
  "use strict";
  const $ = id => document.getElementById(id);
  const state = {suppliers: [], categories: [], sheet: null, rows: [], filtered: [], edits: new Map(), selected: new Set(), page: 0, busy: false, uncertain: false, catalogFilter: "all"};
  const PAGE_SIZE = 50;
  const number = value => Number(value).toLocaleString("ru-RU");
  const shownPrice = value => value === null || value === "" ? "нет" : number(value);
  const PRICE_FIELDS = ["price_1", "price_12"];
  const fields = {price_1: "1 месяц", price_12: "12 месяцев", min_price: "минимальная цена"};
  const warranty = field => field === "price_1" ? "1 мес." : field === "price_12" ? "12 мес." : "";
  const minimumText = (value, supplier, field) => value === null || value === "" || Number(value) <= 0
    ? "нет" : `${number(value)} $ · ${supplier || "поставщик не указан"}${field ? ` · ${warranty(field)}` : ""}`;
  const CATALOG_FILTER_GROUPS = [
    {id: "smartphones", label: "Телефоны", items: [
      {id: "smartphones-xiaomi-poco", label: "Xiaomi, Redmi, Poco", categories: ["Смартфоны бренда Xiaomi", "Смартфоны бренда Poco"], exclude: /^7Tech Connect U7(?:\s|$)/i},
      {id: "smartphones-samsung", label: "Samsung", categories: ["Смартфоны бренда Samsung"]},
      {id: "smartphones-iphone-18-duo", label: "iPhone 18 / Duo", categories: ["Смартфоны бренда Apple"], include: /^Apple iPhone (?:18(?:\s|$)|Duo(?:\s|$))/i},
      {id: "smartphones-iphone-air-17", label: "iPhone Air / 17 Series", categories: ["Смартфоны бренда Apple"], include: /^Apple iPhone (?:Air(?:\s|$)|17(?:e)?(?:\s|$))/i},
      {id: "smartphones-iphone-13-16", label: "iPhone 13–16 Series", categories: ["Смартфоны бренда Apple"], include: /^Apple iPhone (?:13|14|15|16)(?:e)?(?:\s|$)/i},
      {id: "smartphones-honor-huawei", label: "Honor / Huawei", categories: ["Смартфоны бренда Honor", "Смартфоны бренда Huawei"]},
      {id: "smartphones-google-pixel", label: "Google Pixel", categories: ["Смартфоны бренда Google Pixel"]},
      {id: "smartphones-infinix", label: "Infinix", categories: ["Смартфоны бренда Infinix"]},
      {id: "smartphones-tecno", label: "Tecno", categories: ["Смартфоны бренда Tecno"]},
      {id: "smartphones-7tech-connect-u7", label: "7Tech Connect U7", categories: ["Смартфоны бренда 7TECH", "Смартфоны бренда 7Tech Connect U7", "7Tech Connect U7", "Смартфоны бренда Xiaomi"], include: /^7Tech Connect U7(?:\s|$)/i},
      {id: "smartphones-keypad", label: "Кнопочные — Nokia / Samsung / Novey", categories: ["Кнопочные телефоны бренда Nokia", "Кнопочные телефоны бренда Samsung", "Кнопочные телефоны бренда Novey", "Кнопочные телефоны бренда Duoqin", "Кнопочные телефоны бренда LG"]},
    ]},
    {id: "tablets", label: "Планшеты", items: [
      {id: "tablets-apple", label: "iPad", categories: ["Планшеты бренда Apple"]},
      {id: "tablets-samsung", label: "Samsung", categories: ["Планшеты бренда Samsung"]},
      {id: "tablets-xiaomi", label: "Xiaomi", categories: ["Планшеты бренда Xiaomi"]},
      {id: "tablets-honor-huawei", label: "Honor / Huawei", categories: ["Планшеты бренда Honor", "Планшеты бренда Huawei"]},
    ]},
    {id: "audio", label: "Наушники, колонки", items: [
      {id: "audio-apple", label: "AirPods, EarPods, HomePod", categories: ["Наушники бренда Apple", "Колонка бренда Apple"]},
      {id: "audio-samsung", label: "Samsung Buds", categories: ["Наушники бренда Samsung"]},
      {id: "audio-xiaomi", label: "Xiaomi Buds", categories: ["Наушники бренда Xiaomi"]},
      {id: "audio-sony", label: "Sony", categories: ["Наушники бренда Sony"]},
      {id: "audio-huawei-honor", label: "Huawei / Honor", categories: ["Наушники бренда Huawei", "Наушники бренда Honor"]},
      {id: "audio-jbl", label: "JBL", categories: ["Наушники бренда JBL", "Колонка бренда JBL"]},
      {id: "audio-nothing", label: "CMF (Nothing)", categories: ["Наушники бренда Nothing"]},
      {id: "audio-marshall", label: "Marshall", categories: ["Наушники бренда Marshall"]},
      {id: "audio-anker", label: "Anker", categories: ["Наушники бренда Anker"]},
      {id: "audio-beats-dyson", label: "Beats / Dyson", categories: ["Наушники бренда Beats", "Наушники бренда Dyson"]},
      {id: "audio-shokz", label: "Shokz", categories: ["Наушники бренда Shokz"]},
      {id: "audio-yandex", label: "Яндекс", categories: ["Колонка бренда Яндекс"]},
    ]},
    {id: "wearables", label: "Часы, фитнес-браслеты, кольца", items: [
      {id: "wearables-apple", label: "Apple Watch", categories: ["Часы бренда Apple"]},
      {id: "wearables-samsung", label: "Samsung Watch", categories: ["Часы бренда Samsung"]},
      {id: "wearables-xiaomi", label: "Xiaomi Watch", categories: ["Часы бренда Xiaomi"]},
      {id: "wearables-amazfit-haylou-mibro", label: "Amazfit, Haylou, MiBro", categories: ["Часы бренда Amazfit", "Часы бренда Haylou", "Часы бренда Mibro"]},
      {id: "wearables-huawei", label: "Huawei Watch", categories: ["Часы бренда Huawei"]},
      {id: "wearables-nothing", label: "CMF Watch (Nothing)", categories: ["Часы бренда Nothing"]},
      {id: "wearables-porodo", label: "Porodo — детские часы", categories: ["Часы бренда Porodo"]},
      {id: "wearables-whoop-fitbit", label: "Whoop / Fitbit", categories: ["Часы бренда Whoop", "Фитнес-браслеты Fitbit"]},
      {id: "wearables-iqibla", label: "iQibla", categories: ["Умное кольцо Бренда iQibla"]},
    ]},
    {id: "apple-computers", label: "MacBook, iMac, Mac mini", standalone: true, items: [
      {id: "apple-computers-all", label: "MacBook, iMac, Mac mini", categories: ["Ноутбуки бренда Apple", "Комаютеры бренда Apple"]},
    ]},
    {id: "photo-video", label: "Фото, видео и блогинг", items: [
      {id: "photo-dji", label: "Техника DJI", categories: ["Стабилизатор камеры Бренда Dji"]},
      {id: "photo-hollyland", label: "Микрофоны Hollyland", categories: ["Диктофон бренда Hollyland"]},
      {id: "photo-insta360", label: "Insta360", categories: ["Продукция Бренда Insta360"]},
      {id: "photo-gopro", label: "GoPro", categories: ["Камеры Бренда GoPro"]},
      {id: "photo-instax", label: "Instax", categories: ["Моментальные фотоаппараты"]},
    ]},
    {id: "smart-glasses-vr", label: "VR-очки / Умные очки", items: [
      {id: "glasses-ray-ban-meta", label: "Ray-Ban Meta", categories: ["Очки бренда Ray-Ban Meta"]},
      {id: "glasses-oakley-meta", label: "Oakley Meta", categories: ["Очки бренда Oakley Meta"]},
      {id: "vr-meta-quest", label: "Meta Quest", categories: ["VR Очки Бренда Meta"]},
    ]},
    {id: "home-office", label: "Техника для дома и офиса", items: [
      {id: "home-tv-boxes", label: "ТВ-приставки", categories: ["ТВ Бокс бренда Apple", "ТВ Бокс бренда Xiaomi", "ТВ Бокс Бренда Яндекс"]},
      {id: "home-wifi", label: "Wi-Fi-оборудование", categories: ["Wifi бренда Tp - Link", "Wifi бренда Xiaomi", "Wifi бренда D-Link", "Wifi бренда D - Link"]},
      {id: "home-cameras", label: "Камеры", categories: ["Камера Бренда Xiaomi"]},
      {id: "home-yandex-sensors", label: "Датчики для Яндекс Станции", categories: ["Датчик умного дома Бренда Яндекс"]},
      {id: "home-vacuums", label: "Пылесосы", categories: ["Пылесосы бренда Xiaomi", "Пылесосы Dyson", "Пылесосы бренда Deerma"]},
      {id: "home-air", label: "Очистители / увлажнители воздуха", categories: ["Очиститель воздуха Dyson", "Очиститель/Увложнитель воздуха Xiaomi", "Увложнитель воздуха Xiaomi", "Увложнитель воздуха Deerma", "Увлажнитель воздуха бренда Deerma"]},
    ]},
    {id: "charging", label: "Зарядные устройства и Power Bank", items: [
      {id: "charging-adapters-cables", label: "Адаптеры и USB-кабели", categories: ["Adapter и USB Бренда Apple", "Adapter и USB Бренда Samsung", "Adapter и USB Бренда Xiaomi"]},
      {id: "charging-car", label: "Car Adapter", categories: ["Car Charger Бренда Samsung", "Car Charger Бренда Xiaomi"]},
      {id: "charging-power-bank", label: "Power Bank", categories: ["Power Bank бренда Apple", "Power Bank бренда Belkin", "Power Bank бренда Samsung", "Power Bank бренда Xiaomi"]},
      {id: "charging-stations", label: "Зарядные станции", categories: ["Belkin Зарядные станции"]},
    ]},
    {id: "gaming", label: "PlayStation / Xbox", standalone: true, items: [
      {id: "gaming-playstation-xbox", label: "PlayStation / Xbox", categories: ["PlayStation Store", "Приставка Xbox"]},
    ]},
    {id: "dyson-beauty", label: "Dyson — фены и стайлеры", standalone: true, items: [
      {id: "dyson-hair", label: "Dyson — фены и стайлеры", categories: ["Фен Dyson", "Стайлер (Airwrap) Dyson", "Стайлер-выпрямитель (Airstrait) Dyson", "Расческа", "Выпрямитель (Corrale) Dyson", "Утюжок (Corrale) Dyson"]},
    ]},
    {id: "voice-recorders", label: "Диктофоны Plaud", standalone: true, items: [
      {id: "voice-recorders-plaud", label: "Диктофоны Plaud", categories: ["Диктофон бренда Plaud"]},
    ]},
    {id: "storage", label: "HDD, SSD, USB, MicroSD", standalone: true, items: [
      {id: "storage-all", label: "HDD, SSD, USB, MicroSD", categories: ["Хард Бренда Seagate", "Хард Бренда Toshiba", "Хард БрендаTranscend", "SSD Бренда Lexar", "SSD Бренда SanDisk", "SSD бренда Ares", "MicroSD Бренда SanDisk", "USB Бренда SanDisk"]},
    ]},
    {id: "accessories", label: "AirTag, SmartTag, Pencil, Keyboard, Mouse", standalone: true, items: [
      {id: "accessories-combined", label: "AirTag, SmartTag, Pencil, Keyboard, Mouse", categories: ["Tag Бренда Apple", "Tag Бренда Samsung", "Pencil бренда Apple", "Pencil бренда Xiaomi", "Мышь Бренда Apple", "Мышь Бренда Xiaomi", "Клавиатура бренда Apple", "Клавиатура бренда Samsung", "Клавиатура бренда Xiaomi"]},
    ]},
  ];
  const CATALOG_FILTER_RULES = CATALOG_FILTER_GROUPS.flatMap(group => group.items);
  const CATALOG_OTHER_FILTER = {id: "other", label: "Остальные"};
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
  function catalogRuleMatches(row, rule) {
    if (!rule.categories.includes(row.category_name)) return false;
    if (rule.include && !rule.include.test(row.model_name)) return false;
    if (rule.exclude && rule.exclude.test(row.model_name)) return false;
    return true;
  }
  function catalogFilterMatches(row) {
    if (state.catalogFilter === "all") return true;
    if (state.catalogFilter === CATALOG_OTHER_FILTER.id) {
      return !CATALOG_FILTER_RULES.some(rule => catalogRuleMatches(row, rule));
    }
    const group = CATALOG_FILTER_GROUPS.find(item => item.id === state.catalogFilter);
    if (group) return group.items.some(rule => catalogRuleMatches(row, rule));
    const rule = CATALOG_FILTER_RULES.find(item => item.id === state.catalogFilter);
    return rule ? catalogRuleMatches(row, rule) : true;
  }
  function updateCatalogNavigation() {
    const nav = $("catalog-nav");
    if (!nav) return;
    nav.querySelectorAll("[data-catalog-filter]").forEach(button => {
      const active = button.dataset.catalogFilter === state.catalogFilter;
      button.classList.toggle("active", active);
      button.setAttribute("aria-current", active ? "true" : "false");
    });
    const activeGroup = CATALOG_FILTER_GROUPS.find(group =>
      !group.standalone && (group.id === state.catalogFilter || group.items.some(item => item.id === state.catalogFilter))
    );
    nav.querySelectorAll("[data-catalog-children]").forEach(list => {
      const open = list.dataset.catalogChildren === activeGroup?.id;
      list.hidden = !open;
      const parent = nav.querySelector(`[data-catalog-parent="${list.dataset.catalogChildren}"]`);
      if (parent) parent.setAttribute("aria-expanded", open ? "true" : "false");
    });
  }
  function applyCatalogFilter(filterId) {
    state.catalogFilter = filterId;
    $("category").value = "";
    $("search").value = "";
    state.page = 0;
    updateCatalogNavigation();
    filter();
  }
  function catalogFilterButton(filterId, label, className) {
    const button = node("button", undefined, className);
    button.append(node("span", label, "catalog-filter-label"));
    button.type = "button";
    button.dataset.catalogFilter = filterId;
    button.addEventListener("click", () => applyCatalogFilter(filterId));
    return button;
  }
  function renderCatalogNavigation() {
    const nav = $("catalog-nav");
    const title = node("div", "БЫСТРЫЙ ФИЛЬТР", "catalog-nav-title");
    const all = catalogFilterButton("all", "Все товары", "catalog-filter-all");
    const legend = node("div", undefined, "catalog-schedule-legend");
    legend.id = "catalog-schedule-legend";
    legend.setAttribute("aria-label", "План публикаций");
    nav.append(title, legend, all);
    CATALOG_FILTER_GROUPS.forEach(group => {
      if (group.standalone) {
        nav.append(catalogFilterButton(group.id, group.label, "catalog-filter-standalone"));
        return;
      }
      const wrapper = node("div", undefined, "catalog-filter-group");
      const parent = catalogFilterButton(group.id, `▸ ${group.label}`, "catalog-filter-parent");
      parent.dataset.catalogParent = group.id;
      parent.setAttribute("aria-expanded", "false");
      const children = node("div", undefined, "catalog-filter-children");
      children.dataset.catalogChildren = group.id;
      children.hidden = true;
      group.items.forEach(item => children.append(catalogFilterButton(item.id, `• ${item.label}`, "catalog-filter-child")));
      wrapper.append(parent, children);
      nav.append(wrapper);
    });
    nav.append(catalogFilterButton(CATALOG_OTHER_FILTER.id, CATALOG_OTHER_FILTER.label, "catalog-filter-other"));
    updateCatalogNavigation();
  }
  function updatePublicationMarkers(schedule) {
    const nav = $("catalog-nav"), legend = $("catalog-schedule-legend");
    nav.querySelectorAll(".catalog-schedule-markers").forEach(markers => markers.remove());
    nav.querySelectorAll("[data-catalog-filter]").forEach(button => button.removeAttribute("title"));
    legend.replaceChildren();
    if (!schedule || !Array.isArray(schedule.days)) {
      legend.append(node("span", "Расписание временно недоступно", "schedule-unavailable"));
      return;
    }
    const days = schedule.days.filter(day => day.offset === 1 || day.offset === 2);
    const description = day => `${day.offset === 1 ? "Завтра" : "Послезавтра"}, ${day.date.split("-").reverse().join(".")}`;
    const dot = day => {
      const mark = node("span", undefined, "schedule-dot " + (day.offset === 1 ? "schedule-tomorrow" : "schedule-later"));
      mark.setAttribute("role", "img");
      mark.setAttribute("aria-label", "По плану: " + description(day));
      mark.title = "По плану: " + description(day);
      return mark;
    };
    days.forEach(day => {
      const item = node("span", undefined, "schedule-legend-item");
      item.append(dot(day), node("span", description(day)));
      legend.append(item);
    });
    nav.querySelectorAll("[data-catalog-filter]").forEach(button => {
      const id = button.dataset.catalogFilter;
      const group = CATALOG_FILTER_GROUPS.find(group => group.id === id);
      const keys = group ? group.items.map(item => item.id) : [id];
      const planned = days.filter(day => day.section_keys.some(key => keys.includes(key)));
      if (!planned.length) return;
      const markers = node("span", undefined, "catalog-schedule-markers");
      planned.forEach(day => markers.append(dot(day)));
      button.append(markers);
      button.title = planned.map(day => "По плану: " + description(day)).join("; ");
    });
  }
  let scheduleLoading = false;
  async function refreshPublicationMarkers() {
    if (document.hidden || !state.suppliers.length || scheduleLoading) return;
    scheduleLoading = true;
    try {
      const response = await fetch("/monitoring/api/prices/admin/entry/suppliers", {
        credentials: "same-origin", cache: "no-store", headers: {Accept: "application/json"},
      });
      if (!response.ok) throw new Error("schedule_unavailable");
      updatePublicationMarkers((await response.json()).publication_schedule);
    } catch { updatePublicationMarkers(null); }
    finally { scheduleLoading = false; }
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
  function categoryView() { return Boolean($("category").value) || state.catalogFilter !== "all"; }
  function shownRows() {
    if (categoryView()) return state.filtered;
    return state.filtered.slice(state.page * PAGE_SIZE, (state.page + 1) * PAGE_SIZE);
  }
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
    $("copy-ids").disabled = !state.selected.size;
    $("bulk").disabled = !state.selected.size || state.busy || state.uncertain;
    $("supplier").disabled = state.busy;
    $("refresh").disabled = state.busy;
  }
  function filter() {
    const words = $("search").value.toLowerCase().trim().split(/\s+/).filter(Boolean);
    const category = $("category").value, availability = $("availability").value;
    state.filtered = state.rows.filter(row => {
      if (!catalogFilterMatches(row)) return false;
      if (category && row.category_name !== category) return false;
      const searchable = `${row.model_name} ${row.color} ${row.memory} ${row.product_id}`.toLowerCase();
      if (!words.every(w => searchable.includes(w))) return false;
      const priced = Number(row.price_1) > 0 || Number(row.price_12) > 0;
      if (availability === "priced" && !priced) return false;
      if (availability === "empty" && priced) return false;
      if (availability === "changed" && !["price_1", "price_12"].some(f => state.edits.has(key(row, f)))) return false;
      return true;
    });
    state.page = categoryView() ? 0 : Math.min(state.page, Math.max(0, Math.ceil(state.filtered.length / PAGE_SIZE) - 1));
    renderRows();
  }
  function renderRows() {
    const slice = shownRows();
    const fullCategory = categoryView();
    const categoryName = $("category").value
      ? $("category").selectedOptions[0]?.textContent
      : CATALOG_FILTER_GROUPS.find(group => group.id === state.catalogFilter)?.label
        || CATALOG_FILTER_RULES.find(rule => rule.id === state.catalogFilter)?.label
        || CATALOG_OTHER_FILTER.label;
    const fragment = document.createDocumentFragment();
    slice.forEach((row, rowIndex) => {
      const tr = node("tr");
      const previous = slice[rowIndex - 1];
      if (!previous || previous.model_name !== row.model_name || previous.category_name !== row.category_name) tr.classList.add("model-start");
      if (state.selected.has(row.key)) tr.classList.add("row-selected");
      if (PRICE_FIELDS.some(f => state.edits.has(key(row, f)))) tr.classList.add("row-dirty");
      const checkCell = node("td"), check = node("input");
      check.type = "checkbox"; check.checked = state.selected.has(row.key);
      check.setAttribute("aria-label", "Выбрать " + row.model_name + " " + row.memory + " " + row.color);
      check.addEventListener("change", () => { check.checked ? state.selected.add(row.key) : state.selected.delete(row.key); renderRows(); });
      checkCell.append(check); tr.append(checkCell);
      const product = node("td", undefined, "product-cell");
      const productMeta = node("div", undefined, "product-meta");
      productMeta.append(node("span", "ID " + row.product_id, "product-id"), node("span", row.category_name, "product-category"));
      product.append(node("div", row.model_name, "product-name"), productMeta); tr.append(product);
      const memory = node("td", undefined, "memory-cell");
      memory.append(node("span", row.memory || "Не указана", "variant-memory" + (row.memory ? "" : " variant-empty")));
      tr.append(memory);
      const color = node("td", undefined, "color-cell");
      const colorContent = node("div", undefined, "color-content");
      colorContent.append(node("span", row.color || "Не указан", "variant-color" + (row.color ? "" : " variant-empty")));
      color.append(colorContent);
      tr.append(color);
      PRICE_FIELDS.forEach(field => {
        const td = node("td", undefined, "editable-price-cell"), input = node("input");
        td.dataset.label = "Гарантия " + warranty(field) + " · $";
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
          tr.classList.toggle("row-dirty", PRICE_FIELDS.some(f => state.edits.has(key(row, f))));
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
        const history = node("button", "◷", "cell-history-button");
        history.type = "button";
        history.setAttribute("aria-label", `История цены: ${row.model_name} ${row.memory} ${row.color}, ${fields[field]}`);
        history.title = "История сохранённых изменений этой цены";
        history.addEventListener("click", () => showCellHistory(row, field));
        const control = node("div", undefined, "price-control");
        control.append(input, history);
        td.append(control); tr.append(td);
      });
      const minimum = node("td", undefined, "minimum");
      const minimumValue = node("div", undefined, "minimum-value");
      if (Number(row.min_price) > 0) {
        minimumValue.append(node("strong", number(row.min_price)),
          node("small", `${row.min_supplier_name || "Поставщик не указан"}${row.min_price_field ? ` · ${warranty(row.min_price_field)}` : ""}`));
      } else minimumValue.append(node("span", "Нет предложений", "variant-empty"));
      const minimumHistory = node("button", "◷", "cell-history-button");
      minimumHistory.type = "button";
      minimumHistory.setAttribute("aria-label", `История минимальной цены: ${row.model_name} ${row.memory} ${row.color}`);
      minimumHistory.title = "История изменения минимальной цены и поставщика";
      minimumHistory.addEventListener("click", () => showCellHistory(row, "min_price"));
      const minimumContent = node("div", undefined, "minimum-content");
      minimumContent.append(minimumValue, minimumHistory);
      minimum.append(minimumContent); tr.append(minimum);
      fragment.append(tr);
    });
    $("rows").replaceChildren(fragment);
    $("found-count").textContent = `${number(state.filtered.length)} позиций`;
    $("empty").hidden = state.filtered.length !== 0;
    $("page-label").textContent = fullCategory
      ? `${categoryName} · вся категория на одной странице · ${number(state.filtered.length)} позиций`
      : slice.length ? `${number(state.page * PAGE_SIZE + 1)}–${number(state.page * PAGE_SIZE + slice.length)} из ${number(state.filtered.length)}` : "0 позиций";
    $("page-actions").hidden = fullCategory;
    $("previous").disabled = state.page === 0;
    $("next").disabled = fullCategory || (state.page + 1) * PAGE_SIZE >= state.filtered.length;
    $("select-page").checked = !!slice.length && slice.every(r => state.selected.has(r.key));
    $("select-page").indeterminate = slice.some(r => state.selected.has(r.key)) && !$("select-page").checked;
    $("select-page").setAttribute("aria-label", fullCategory ? "Выбрать всю категорию" : "Выбрать текущую страницу");
    updateSavebar();
  }
  async function load(sheetId) {
    state.busy = true; updateSavebar(); $("workspace").classList.add("loading"); notice("");
    try {
      const data = await api("catalog/" + sheetId);
      state.sheet = data.sheet_id; state.rows = data.rows; state.edits.clear(); state.selected.clear(); state.uncertain = false; state.page = 0;
      $("supplier").value = String(sheetId);
      $("editing-supplier").textContent = `Цены: ${$("supplier").selectedOptions[0]?.textContent || "поставщик"} · доллары США ($)`;
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
    const isMinimum = field === "min_price";
    const description = node("p", `${isMinimum ? "Все поставщики" : supplier} · ${row.model_name} · ${row.memory || "—"} · ${row.color || "—"} · ${fields[field]}`, "cell-history-description");
    const scope = node("p", isMinimum
      ? "Минимум пересчитывается по ценам 1 и 12 месяцев всех поставщиков. История построена по подтверждённым сохранениям на сайте."
      : "Только сохранённые изменения с сайта. Правки напрямую в Google Price до перехода на сайт в этот журнал не входили.", "cell-history-note");
    const saved = node("p", isMinimum
      ? `Текущий минимум: ${minimumText(row.min_price, row.min_supplier_name, row.min_price_field)}.`
      : `Цена в загруженном каталоге: ${shownPrice(row[field])}${row[field] === null ? "" : " $"}.`, "cell-history-note");
    if (!isMinimum && state.edits.has(key(row, field))) saved.append(node("strong", " Несохранённый черновик в историю не входит."));
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
          if (isMinimum) {
            values.append(node("del", minimumText(entry.before, entry.before_supplier_name, entry.before_field)),
              node("span", " → "),
              node("strong", minimumText(entry.after, entry.after_supplier_name, entry.after_field)));
          } else values.append(node("del", shownPrice(entry.before)), node("span", " → "), node("strong", shownPrice(entry.after)));
          item.append(label, values); list.append(item);
        }
        total += data.entries.length; cursor = data.next_before;
        status.textContent = total ? `Показано записей: ${total}.${cursor === null ? ` Это вся сохранённая история ${isMinimum ? "минимальной цены" : "ячейки"}.` : ""}` : isMinimum ? "Минимальная цена после сохранений на сайте ещё не менялась." : "Эту цену на сайте ещё не меняли.";
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
        const refreshQueued = ["queued"].includes(result.refresh?.status);
        const refreshText = refreshQueued
          ? "Обновление прайса запущено сразу и обычно завершается в течение 2 минут."
          : "Прайс подхватит изменения при очередном плановом импорте.";
        notice(`Сохранено цен: ${result.changed}. ${state.source === "sqlite" ? "Цены записаны на сервере, Google Price не используется." : "Google Sheets обновлена."} ${refreshText}`);
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
  $("copy-ids").addEventListener("click", async () => {
    const ids = [...new Set(state.rows.filter(row => state.selected.has(row.key)).map(row => String(row.product_id).trim()).filter(Boolean))];
    if (!ids.length) return;
    const text = ids.join(", ");
    try {
      let copied = false;
      if (navigator.clipboard?.writeText) {
        try { await navigator.clipboard.writeText(text); copied = true; } catch { copied = false; }
      }
      if (!copied) {
        const helper = node("textarea");
        helper.value = text; helper.readOnly = true; helper.className = "clipboard-helper";
        document.body.append(helper); helper.select();
        copied = document.execCommand("copy"); helper.remove();
      }
      if (!copied) throw new Error("clipboard_unavailable");
      notice(`ID скопированы: ${ids.length}. Формат: ${ids.slice(0, 3).join(", ")}${ids.length > 3 ? ", …" : ""}`);
    } catch {
      notice("Не удалось скопировать ID. Разрешите браузеру доступ к буферу обмена и повторите попытку.", true);
    }
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
  ["search", "availability"].forEach(id => $(id).addEventListener(id === "search" ? "input" : "change", () => { state.page = 0; filter(); }));
  $("category").addEventListener("change", () => {
    state.catalogFilter = $("category").value ? "toolbar-category" : "all";
    state.page = 0;
    updateCatalogNavigation();
    filter();
  });
  $("previous").addEventListener("click", () => { state.page--; renderRows(); });
  $("next").addEventListener("click", () => { state.page++; renderRows(); });
  $("select-page").addEventListener("change", () => { shownRows().forEach(r => $("select-page").checked ? state.selected.add(r.key) : state.selected.delete(r.key)); renderRows(); });
  window.addEventListener("beforeunload", e => { if (state.edits.size || state.busy) { e.preventDefault(); e.returnValue = ""; } });
  document.addEventListener("keydown", e => { if (e.key === "/" && !["INPUT", "SELECT", "TEXTAREA"].includes(document.activeElement.tagName)) { e.preventDefault(); $("search").focus(); } });
  async function init() {
    notice("Загружаем цены…");
    try {
      const data = await api("suppliers"); state.suppliers = data.suppliers;
      updatePublicationMarkers(data.publication_schedule);
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
  renderCatalogNavigation();
  window.setInterval(refreshPublicationMarkers, 60000);
  document.addEventListener("visibilitychange", () => { if (!document.hidden) refreshPublicationMarkers(); });
  init();
})();

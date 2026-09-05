"use strict";

const bootstrap = JSON.parse(document.getElementById("monitoring-bootstrap").textContent);
const $ = id => document.getElementById(id);
const esc = value => String(value ?? "—").replace(/[&<>'"]/g, character => ({"&":"&amp;","<":"&lt;",">":"&gt;","'":"&#39;",'"':"&quot;"}[character]));
const number = value => new Intl.NumberFormat("ru-RU").format(Number(value || 0));
const titles = {
  overview:["ОПЕРАЦИОННЫЙ ЦЕНТР","Главная"], calls:["КОММУНИКАЦИИ","Звонки и продажи"],
  site:["САЙТ TEXNIKACH","Статистика GO"], reviews:["ОБРАТНАЯ СВЯЗЬ","Отзывы клиентов"],
  "delivery/live":["ЛОГИСТИКА","Доставка сейчас"], "delivery/stats":["ЛОГИСТИКА","Статистика доставки"],
  prices:["КАТАЛОГ","Прайс и синхронизация"]
};
let controller = null;
const query = new URLSearchParams(location.search);
let period = query.get("period") || "today";

function csrfToken(){
  const prefix="__Host-texnikach_monitoring_csrf=";
  const item=document.cookie.split("; ").find(value=>value.startsWith(prefix));
  return item ? decodeURIComponent(item.slice(prefix.length)) : "";
}
async function api(path){
  const url = new URL(path, location.origin);
  if(!url.search){["period","date_from","date_to","day","month","week","courier_id","delivery_courier_id"].forEach(key=>{if(query.get(key))url.searchParams.set(key,query.get(key));});if(!url.searchParams.has("period"))url.searchParams.set("period",period);}
  const response=await fetch(url,{credentials:"same-origin",cache:"no-store",signal:controller.signal,headers:{Accept:"application/json"}});
  if(response.status===401){location.href=`/monitoring/login?next=${encodeURIComponent(location.pathname+location.search)}`;throw new Error("Сессия завершена");}
  const payload=await response.json().catch(()=>({detail:`HTTP ${response.status}`}));
  if(!response.ok)throw new Error(payload?.meta?.error_code||payload?.detail||`HTTP ${response.status}`);
  return payload;
}
function metric(value,label,kind=""){return `<div class="metric ${kind}"><strong>${esc(value)}</strong><span>${esc(label)}</span></div>`}
function panel(title,subtitle,body,state="ok",link=""){return `<article class="panel"><div class="panel-head"><div><h2>${esc(title)}</h2><p>${esc(subtitle)}</p></div><span class="source-state ${state}">${state==="ok"?"Актуально":"Недоступно"}</span></div>${body}${link?`<a class="panel-link" href="${esc(link)}">Открыть раздел →</a>`:""}</article>`}
function unavailable(source){return panel(source,"Источник данных",`<div class="panel-error">Источник временно недоступен</div>`,"unavailable")}
function source(payload,name){return payload.sources[name]||{data:null,meta:{status:"unavailable"}}}

function renderOverview(payload){
  const calls=source(payload,"calls"),reviews=source(payload,"reviews"),delivery=source(payload,"delivery"),prices=source(payload,"prices"),go=source(payload,"go_site");
  const cs=calls.data?.stats||{},rs=reviews.data?.summary||{},ds=delivery.data?.summary||{},ps=prices.data||{};
  const attention=[
    [cs.missed_not_processed||0,"Пропущенных без обработки"],
    [ds.unassigned||0,"Доставок без курьера"],
    [ds.attention||0,"Доставок требуют внимания"],
    [rs.attention||0,"Отзывов требуют ответа"],
    [Object.values(payload.sources).filter(item=>item.meta.status!=="ok").length,"Недоступных источников"]
  ];
  const blocks=[];
  blocks.push(calls.data?panel("Звонки","Сегодня и выбранный период",`<div class="metric-grid">${metric(number(cs.calls),"Клиентских звонков")}${metric(number(cs.answered),"Отвечено","good")}${metric(number(cs.missed),"Пропущено","danger")}${metric(`${cs.answer_rate||0}%`,"Процент ответа")}</div>`,"ok","/monitoring/calls"):unavailable("Звонки"));
  blocks.push(delivery.data?panel("Доставка","Текущее состояние",`<div class="metric-grid">${metric(number(ds.active),"Активные")}${metric(number(ds.on_way),"В пути")}${metric(number(ds.completed_today),"Завершено")}${metric(number(ds.attention),"Внимание","danger")}</div>`,"ok","/monitoring/delivery/live"):unavailable("Доставка"));
  blocks.push(reviews.data?panel("Отзывы","Обратная связь клиентов",`<div class="metric-grid">${metric(number(rs.total),"Всего")}${metric(number(rs.attention),"Внимание","danger")}${metric(number(rs.with_comment),"С комментарием")}${metric(number(rs.notified),"Уведомлено")}</div>`,"ok","/monitoring/reviews"):unavailable("Отзывы"));
  blocks.push(prices.data?panel("Прайс","Состояние каталога",`<div class="mini-list"><div class="mini-row"><span>Статус</span><b>${esc(ps.status)}</b></div><div class="mini-row"><span>Разделов</span><b>${number(ps.sections?.length)}</b></div><div class="mini-row"><span>Планировщик</span><b>${ps.scheduler_running?"Работает":"Остановлен"}</b></div></div>`,"ok","/monitoring/prices"):unavailable("Прайс"));
  blocks.push(go.data?panel("Сайт GO","Показатели сайта",renderObjectMetrics(go.data.metrics||go.data),"ok","/monitoring/site"):unavailable("Сайт GO"));
  $("content").innerHTML=`<section class="attention-grid">${attention.map(([value,label])=>`<div class="attention-card ${Number(value)===0?"good":""}"><strong>${number(value)}</strong><span>${esc(label)}</span></div>`).join("")}</section><section class="section-grid">${blocks.join("")}</section>`;
}
function renderObjectMetrics(value){
  const entries=Object.entries(value||{}).filter(([,item])=>["string","number","boolean"].includes(typeof item)).slice(0,8);
  if(!entries.length)return `<div class="empty-state">Показателей пока нет</div>`;
  return `<div class="metric-grid">${entries.map(([key,item])=>metric(typeof item==="number"?number(item):item,key.replaceAll("_"," "))).join("")}</div>`;
}
function table(headers,rows){return `<div class="table-wrap"><table><thead><tr>${headers.map(value=>`<th>${esc(value)}</th>`).join("")}</tr></thead><tbody>${rows.length?rows.join(""):`<tr><td colspan="${headers.length}" class="muted">Нет данных</td></tr>`}</tbody></table></div>`}
async function renderCalls(){
  const [summary,managers,recent]=await Promise.all([api("/monitoring/api/calls"),api("/monitoring/api/calls/managers"),api("/monitoring/api/calls/recent")]);
  const s=summary.data.stats||{},managerRows=managers.data.results||[],recentRows=recent.data.results||recent.data.calls||[];
  $("content").innerHTML=`<section class="metric-grid">${metric(number(s.calls),"Звонки")}${metric(number(s.answered),"Отвечено","good")}${metric(number(s.missed),"Пропущено","danger")}${metric(`${s.answer_rate||0}%`,"Процент ответа")}${metric(number(s.bought),"Купил")}${metric(number(s.not_bought),"Потерян")}${metric(number(s.pending),"В работе")}${metric(`${s.processed_sale_conversion||0}%`,"Конверсия")}</section><section class="section-grid">${panel("По менеджерам","Результаты выбранного периода",table(["Менеджер","Звонки","Отвечено","Пропущено","Продажи"],managerRows.map(row=>`<tr><td><b>${esc(row.manager||row.name)}</b></td><td>${number(row.calls)}</td><td>${number(row.answered??Math.max(Number(row.calls||0)-Number(row.missed||0),0))}</td><td>${number(row.missed)}</td><td>${number(row.bought)}</td></tr>`)))}${panel("Последние звонки","До 50 записей",table(["Время","Клиент","Менеджер","Статус"],recentRows.map(row=>`<tr><td>${esc(row.local_time||row.start_time_formatted||row.start_time)}</td><td>${esc(row.client_name||row.client_number)}</td><td>${esc(row.manager||row.user_login)}</td><td>${row.answered?"Отвечен":"Пропущен"}</td></tr>`)))}</section>`;
}
async function renderReviews(){
  const payload=await api("/monitoring/api/reviews"),data=payload.data,s=data.summary||{};
  const categories=data.categories||[],reviews=data.reviews||[];
  $("content").innerHTML=`<section class="metric-grid">${metric(number(s.total),"Всего отзывов")}${metric(number(s.attention),"Требуют внимания","danger")}${metric(number(s.with_comment),"С комментарием")}${metric(number(s.with_phone),"С телефоном")}</section><section class="section-grid">${panel("Оценки по категориям","Средняя оценка",table(["Категория","Оценок","Средняя","5★"],categories.map(row=>`<tr><td>${esc(row.label)}</td><td>${number(row.count)}</td><td class="money">${esc(row.average)}</td><td>${number(row.r5)}</td></tr>`)))}${panel("Последние отзывы","До 100 записей",table(["Дата","Менеджер","Телефон","Комментарий"],reviews.map(row=>`<tr><td>${esc(row.created_at)}</td><td>${esc((row.managers||[]).map(item=>item.name).filter(Boolean).join(", "))}</td><td>${esc(row.customer_phone)}</td><td>${esc(row.final_comment)}</td></tr>`)))}</section>`;
}
async function renderPrices(){
  const payload=await api("/monitoring/api/prices"),data=payload.data||{},snapshot=data.snapshot||{};
  const canManage=Boolean(bootstrap.user.can_manage_prices);
  const accessPanel=canManage
    ? panel("Управление прайсом","Разрешено вашему Telegram ID",`<p class="muted">В полном прайсе снова доступны обновление текущего поста, отправка, расписание и остальные прежние действия.</p>`)
    : panel("Безопасный режим","Только просмотр",`<p class="muted">Кнопки публикации доступны только сотрудникам, отдельно указанным в списке редакторов прайса.</p>`);
  const sandbox="allow-scripts allow-popups allow-popups-to-escape-sandbox";
  $("content").innerHTML=`<section class="metric-grid">${metric(data.status||"—","Статус")}${metric(number(data.sections?.length),"Разделов")}${metric(number(snapshot.product_count),"Товаров")}${metric(data.scheduler_running?"Работает":"Остановлен","Планировщик")}</section><section class="section-grid">${panel("Разделы прайса",canManage?"Просмотр и управление публикациями":"Просмотр актуального каталога",table(["Раздел","Товаров","Изменения"],(data.sections||[]).map(row=>`<tr><td><b>${esc(row.title)}</b><br><span class="muted">${esc(row.section_key)}</span></td><td>${number(row.product_count)}</td><td>${row.changed_recent?"Есть":"Нет"}</td></tr>`)))}${accessPanel}</section><section class="catalog-panel"><div class="panel-head"><div><h2>Полный прайс</h2><p>${canManage?"Актуальный каталог и прежние кнопки управления":"Актуальный каталог без повторного пароля"}</p></div><a class="panel-link catalog-open" href="/monitoring/prices/catalog" target="_blank" rel="noopener">Открыть отдельно ↗</a></div><iframe class="price-catalog" title="Полный прайс TEXNIKACH" src="/monitoring/prices/catalog" sandbox="${sandbox}"></iframe></section>`;
}
async function renderSite(){const payload=await api("/monitoring/api/site");$("content").innerHTML=panel("Сайт GO","Данные внутреннего JSON API",renderObjectMetrics(payload.data.metrics||payload.data))}
async function renderDelivery(live){const endpoint=live?"/monitoring/api/delivery/live":"/monitoring/api/delivery/report",payload=await api(endpoint),data=payload.data||{},s=data.summary||{},orders=data.active_orders||data.orders||[],mapQuery=new URLSearchParams();["day","period","courier_id","delivery_courier_id"].forEach(key=>{if(query.get(key))mapQuery.set(key,query.get(key));});if(!mapQuery.has("period"))mapQuery.set("period",period);const mapPanel=live?"":panel("Маршрут на карте",data.day_label||"Выбранный день",`<img class="delivery-map" src="/monitoring/api/delivery/map.png?${mapQuery}" alt="Карта маршрутов доставки">`);$("content").innerHTML=`<section class="metric-grid">${metric(number(s.active??s.orders),live?"Активные":"Заказы")}${metric(number(s.unassigned),"Без курьера","danger")}${metric(number(s.on_way),"В пути")}${metric(number(s.completed_today??s.completed),"Завершено","good")}${metric(number(s.attention),"Внимание","danger")}${metric(s.amount_text||s.created_value_text||"—","Сумма")}</section><section class="section-grid">${mapPanel}${panel(live?"Активные заказы":"Заказы за день",live?"Обновление каждые 20 секунд":"Выбранный отчёт",table(["№","Товар","Менеджер","Курьер","Статус","Адрес"],orders.map(row=>`<tr><td><b>${esc(row.order_number)}</b></td><td>${esc(row.product)}</td><td>${esc(row.manager_name)}</td><td>${esc(row.courier_name)}</td><td>${esc(row.status)}</td><td>${esc(row.address)}</td></tr>`)))}${panel("Курьеры","Текущее распределение",table(["Курьер","Активные","В пути"],(data.couriers||[]).map(row=>`<tr><td><b>${esc(row.courier_name||row.name)}</b></td><td>${number(row.active??row.remaining)}</td><td>${number(row.on_way)}</td></tr>`)))}</section>`}

async function load(){
  if(controller)controller.abort();controller=new AbortController();
  $("pageStatus").classList.remove("show");$("content").innerHTML='<div class="loading-card">Загружаем данные…</div>';$("refreshButton").disabled=true;
  try{
    if(bootstrap.section==="overview")renderOverview(await api("/monitoring/api/overview"));
    else if(bootstrap.section==="calls")await renderCalls();
    else if(bootstrap.section==="reviews")await renderReviews();
    else if(bootstrap.section==="prices")await renderPrices();
    else if(bootstrap.section==="site")await renderSite();
    else if(bootstrap.section==="delivery/live")await renderDelivery(true);
    else if(bootstrap.section==="delivery/stats")await renderDelivery(false);
    $("updated").textContent=`Обновлено ${new Date().toLocaleTimeString("ru-RU",{hour:"2-digit",minute:"2-digit",timeZone:"Asia/Tashkent"})}`;
  }catch(error){if(error.name!=="AbortError"){$("pageStatus").textContent=`Не удалось обновить раздел: ${error.message}`;$("pageStatus").classList.add("show");$("content").innerHTML='<div class="empty-state">Данные временно недоступны. Попробуйте обновить страницу.</div>';}}
  finally{$("refreshButton").disabled=false;}
}
function setPeriod(value){period=value;query.set("period",value);if(value!=="custom"){query.delete("date_from");query.delete("date_to");history.replaceState(null,"",`${location.pathname}?${query}`);load();}$("customDates").hidden=value!=="custom";document.querySelectorAll("[data-period]").forEach(button=>button.classList.toggle("active",button.dataset.period===value));}
document.querySelectorAll("[data-period]").forEach(button=>button.onclick=()=>setPeriod(button.dataset.period));
$("applyDates").onclick=()=>{if(!$("dateFrom").value||!$("dateTo").value)return;query.set("date_from",$("dateFrom").value);query.set("date_to",$("dateTo").value);history.replaceState(null,"",`${location.pathname}?${query}`);load();};
$("refreshButton").onclick=load;
$("logoutButton").onclick=async()=>{const response=await fetch("/monitoring/auth/logout",{method:"POST",credentials:"same-origin",redirect:"follow",headers:{"X-CSRF-Token":csrfToken(),"Content-Type":"application/json"},body:"{}"});location.href=response.url||"/monitoring/login";};
$("menuButton").onclick=()=>{$("sidebar").classList.toggle("open");$("scrim").classList.toggle("show");$("menuButton").setAttribute("aria-expanded",String($("sidebar").classList.contains("open")));};
$("scrim").onclick=()=>{$("sidebar").classList.remove("open");$("scrim").classList.remove("show");};
document.querySelector(`[data-nav="${bootstrap.section}"]`)?.classList.add("active");
$("pageEyebrow").textContent=titles[bootstrap.section][0];$("pageTitle").textContent=titles[bootstrap.section][1];
$("userName").textContent=bootstrap.user.name;$("userRole").textContent=bootstrap.user.role==="admin"?"Администратор":"Менеджер";$("userAvatar").textContent=(bootstrap.user.name||"M").trim().charAt(0).toUpperCase();
$("dateFrom").value=query.get("date_from")||"";$("dateTo").value=query.get("date_to")||"";setPeriod(period);
if(period==="custom")load();
if(bootstrap.section==="delivery/live")setInterval(()=>{if(!document.hidden)load();},20000);

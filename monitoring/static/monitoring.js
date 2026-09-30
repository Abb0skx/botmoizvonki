"use strict";

const bootstrap = JSON.parse(document.getElementById("monitoring-bootstrap").textContent);
const $ = id => document.getElementById(id);
const esc = value => String(value ?? "—").replace(/[&<>'"]/g, character => ({"&":"&amp;","<":"&lt;",">":"&gt;","'":"&#39;",'"':"&quot;"}[character]));
const number = value => new Intl.NumberFormat("ru-RU").format(Number(value || 0));
const titles = {
  overview:["ОПЕРАЦИОННЫЙ ЦЕНТР","Главная"], calls:["КОММУНИКАЦИИ","Звонки и продажи"],
  site:["САЙТ TEXNIKACH","Статистика GO"], reviews:["ОБРАТНАЯ СВЯЗЬ","Отзывы клиентов"],
  prices:["КАТАЛОГ","Прайс и синхронизация"],
  finance:["FINANCE","Рынок и доставка"]
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
  const calls=source(payload,"calls"),reviews=source(payload,"reviews"),prices=source(payload,"prices"),go=source(payload,"go_site");
  const cs=calls.data?.stats||{},rs=reviews.data?.summary||{},ps=prices.data||{};
  const attention=[
    [cs.missed_not_processed||0,"Пропущенных без обработки"],
    [rs.attention||0,"Отзывов требуют ответа"],
    [Object.values(payload.sources).filter(item=>item.meta.status!=="ok").length,"Недоступных источников"]
  ];
  const blocks=[];
  blocks.push(calls.data?panel("Звонки","Полная старая статистика",`<div class="metric-grid">${metric(number(cs.calls),"Клиентских звонков")}${metric(number(cs.answered),"Отвечено","good")}${metric(number(cs.missed),"Пропущено","danger")}${metric(`${cs.answer_rate||0}%`,"Процент ответа")}</div>`,"ok","/dashboard"):unavailable("Звонки"));
  blocks.push(reviews.data?panel("Отзывы","Полная статистика и карточки отзывов",`<div class="metric-grid">${metric(number(rs.total),"Всего")}${metric(number(rs.attention),"Внимание","danger")}${metric(number(rs.with_comment),"С комментарием")}${metric(number(rs.notified),"Уведомлено")}</div>`,"ok","/admin/reviews"):unavailable("Отзывы"));
  blocks.push(prices.data?panel("Прайс","Все прежние кнопки управления",`<div class="mini-list"><div class="mini-row"><span>Статус</span><b>${esc(ps.status)}</b></div><div class="mini-row"><span>Разделов</span><b>${number(ps.sections?.length)}</b></div><div class="mini-row"><span>Планировщик</span><b>${ps.scheduler_running?"Работает":"Остановлен"}</b></div></div>`,"ok","/price"):unavailable("Прайс"));
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
    ? panel("Управление прайсом","Доступно после входа по общему паролю",`<p class="muted">В полном прайсе снова доступны обновление текущего поста, отправка, расписание и остальные прежние действия.</p>`)
    : panel("Безопасный режим","Только просмотр",`<p class="muted">Кнопки публикации сейчас отключены конфигурацией сервера.</p>`);
  const sandbox="allow-scripts allow-popups allow-popups-to-escape-sandbox";
  $("content").innerHTML=`<section class="metric-grid">${metric(data.status||"—","Статус")}${metric(number(data.sections?.length),"Разделов")}${metric(number(snapshot.product_count),"Товаров")}${metric(data.scheduler_running?"Работает":"Остановлен","Планировщик")}</section><section class="section-grid">${panel("Разделы прайса",canManage?"Просмотр и управление публикациями":"Просмотр актуального каталога",table(["Раздел","Товаров","Изменения"],(data.sections||[]).map(row=>`<tr><td><b>${esc(row.title)}</b><br><span class="muted">${esc(row.section_key)}</span></td><td>${number(row.product_count)}</td><td>${row.changed_recent?"Есть":"Нет"}</td></tr>`)))}${accessPanel}</section><section class="catalog-panel"><div class="panel-head"><div><h2>Полный прайс</h2><p>${canManage?"Актуальный каталог и прежние кнопки управления":"Актуальный каталог без повторного пароля"}</p></div><a class="panel-link catalog-open" href="/monitoring/prices/catalog" target="_blank" rel="noopener">Открыть отдельно ↗</a></div><iframe class="price-catalog" title="Полный прайс TEXNIKACH" src="/monitoring/prices/catalog" sandbox="${sandbox}"></iframe></section>`;
}
async function renderSite(){const payload=await api("/monitoring/api/site");$("content").innerHTML=panel("Сайт GO","Данные внутреннего JSON API",renderObjectMetrics(payload.data.metrics||payload.data))}
function financeNavigation(){return `<nav class="finance-nav" aria-label="Разделы Finance"><a class="active" href="/finance">⌁ Рынок Malika</a><a href="/finance/delivery/live">⌖ Доставка сейчас</a><a href="/finance/delivery/stats">▥ Статистика доставки</a></nav>`}
async function renderFinance(){
  $("content").innerHTML=financeNavigation()+'<div class="loading-card">Загружаем статистику рынка…</div>';
  const payload=await api("/monitoring/api/finance"),data=payload.data||{},s=data.summary||{},collector=data.collector||{};
  const modelRows=(data.top_models||[]).map((row,index)=>`<tr><td>${index+1}</td><td><b>${esc(row.model_name)}</b></td><td>${number(row.searches)}</td><td>${number(row.unique_senders)}</td><td>${number(row.competitor_searches)}</td><td>${esc(row.share_percent)}%</td><td>${formatMarketTime(row.last_search_at)}</td></tr>`);
  const competitorRows=(data.competitors||[]).map(row=>`<tr><td><b>${esc(row.label)}</b><br><span class="muted">${esc(row.telegram_user_id)}</span></td><td>${number(row.searches)}</td><td>${number(row.unique_models)}</td><td>${formatMarketTime(row.last_search_at)}</td></tr>`);
  const detailRows=(data.competitor_models||[]).map(row=>`<tr><td><b>${esc(row.label)}</b></td><td>${esc(row.model_name)}</td><td>${number(row.searches)}</td><td>${formatMarketTime(row.last_search_at)}</td></tr>`);
  const dailyRows=(data.daily||[]).map(row=>`<tr><td>${formatMarketDay(row.day)}</td><td>${number(row.searches)}</td><td>${number(row.unique_senders)}</td><td>${number(row.competitor_searches)}</td></tr>`);
  const collectionState=collector.backfill_complete?"История загружена":`Загрузка истории: ${number(collector.backfill_scanned)} сообщений`;
  $("content").innerHTML=`${financeNavigation()}<section class="metric-grid market-metrics">${metric(number(s.searches),"Запросов моделей")}${metric(number(s.unique_searchers),"Искали товар")}${metric(number(s.unique_models),"Разных моделей")}${metric(number(s.competitor_searches),"Запросов конкурентов")}${metric(number(s.supply_messages),"Предложений товара")}${metric(number(s.messages),"Сообщений просмотрено")}</section><section class="market-source"><div><span class="status-dot"></span><b>${esc(data.source?.title||"Malika bozor N1")}</b><small>${esc(data.period?.label||"")} · ${esc(collectionState)}</small></div><span>Последний сбор: ${formatMarketTime(collector.last_collected_at)}</span></section><section class="section-grid finance-grid">${panel("Популярные модели","Спрос за выбранный период",table(["№","Модель","Запросы","Люди","Конкуренты","Доля","Последний"],modelRows))}${panel("Конкуренты","Активность по Telegram ID",table(["Конкурент","Запросы","Модели","Последний"],competitorRows))}${panel("Что искали конкуренты","Разбивка по модели",table(["Конкурент","Модель","Запросы","Последний"],detailRows))}${panel("Динамика по дням","По времени Ташкента",table(["Дата","Запросы","Люди","Конкуренты"],dailyRows))}</section>`;
}
function formatMarketTime(value){if(!value)return "—";const parsed=new Date(value);return Number.isNaN(parsed.getTime())?esc(value):parsed.toLocaleString("ru-RU",{timeZone:"Asia/Tashkent",day:"2-digit",month:"2-digit",hour:"2-digit",minute:"2-digit"})}
function formatMarketDay(value){if(!value)return "—";const [year,month,day]=String(value).split("-");return year&&month&&day?`${day}.${month}.${year}`:esc(value)}
async function load(){
  if(controller)controller.abort();controller=new AbortController();
  $("pageStatus").classList.remove("show");$("content").innerHTML='<div class="loading-card">Загружаем данные…</div>';$("refreshButton").disabled=true;
  try{
    if(bootstrap.section==="overview")renderOverview(await api("/monitoring/api/overview"));
    else if(bootstrap.section==="calls")await renderCalls();
    else if(bootstrap.section==="reviews")await renderReviews();
    else if(bootstrap.section==="prices")await renderPrices();
    else if(bootstrap.section==="site")await renderSite();
    else if(bootstrap.section==="finance")await renderFinance();
    $("updated").textContent=`Обновлено ${new Date().toLocaleTimeString("ru-RU",{hour:"2-digit",minute:"2-digit",timeZone:"Asia/Tashkent"})}`;
  }catch(error){if(error.name!=="AbortError"){$("pageStatus").textContent=`Не удалось обновить раздел: ${error.message}`;$("pageStatus").classList.add("show");$("content").innerHTML=(bootstrap.section==="finance"?financeNavigation():"")+'<div class="empty-state">Данные временно недоступны. Попробуйте обновить страницу.</div>';}}
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

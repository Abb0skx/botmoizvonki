"use strict";

const $ = id => document.getElementById(id);
const format = value => new Intl.NumberFormat("ru-RU").format(Number(value || 0));
const periods = new Map();
let selectedPeriod = "today";
let loadGeneration = 0;

async function getPeriod(period){
  const endpoint=period === "all"?"/monitoring/api/site/all":"/monitoring/api/site";
  const url = new URL(endpoint, location.origin);
  if(period !== "all"){
    url.searchParams.set("period", period);
  }
  const response = await fetch(url,{credentials:"same-origin",cache:"no-store",headers:{Accept:"application/json"}});
  if(response.status === 401){
    location.href = `/monitoring/login?next=${encodeURIComponent(location.pathname+location.search)}`;
    throw new Error("Сессия завершена");
  }
  const payload = await response.json().catch(()=>({}));
  if(!response.ok || !payload.data) throw new Error(payload?.meta?.error_code || payload.detail || `HTTP ${response.status}`);
  return payload.data;
}

function statRow(name,value,ctr){
  const row=document.createElement("div");row.className="stat-row";
  const label=document.createElement("div");label.className="stat-name";label.textContent=name;
  const count=document.createElement("div");count.className="stat-value";count.textContent=format(value);
  const rate=document.createElement("div");rate.className="stat-ctr";rate.textContent=ctr===undefined?"":`${Number(ctr||0).toLocaleString("ru-RU")}%`;
  row.append(label,count,rate);return row;
}

function renderRows(target,items){target.replaceChildren(...items.map(item=>statRow(...item)))}

function render(data){
  const m=data.metrics||{};
  $("views").textContent=format(m.views);$("mainClicks").textContent=format(m.main_clicks);$("mapClicks").textContent=format(m.map_clicks);
  renderRows($("mainActions"),[["Интернет-магазин",m.click_shop,m.click_shop_ctr],["Telegram-каталог",m.click_telegram,m.click_telegram_ctr],["Написать менеджеру",m.click_manager,m.click_manager_ctr],["Позвонить",m.click_phone,m.click_phone_ctr],["Открыли «Наш магазин»",m.click_location_open,m.click_location_open_ctr]]);
  renderRows($("mapActions"),[["Яндекс Навигатор",m.click_yandex_navi],["Яндекс Карты",m.click_yandex_maps],["Google Maps",m.click_google_maps],["Apple Maps",m.click_apple_maps],["Скопировали координаты",m.click_copy_coordinates]]);
  renderRows($("orderActions"),[["Оформить заказ — RU",m.click_order_ru,m.click_order_ru_ctr],["Buyurtma berish — UZ",m.click_order_uz,m.click_order_uz_ctr]]);
  const history=(periods.get("30d")?.series||[]).slice().reverse();
  if(history.length){
    $("historyRows").replaceChildren(...history.map(day=>{const row=document.createElement("tr");[day.date||day.stat_date,day.views,day.main_clicks,day.map_clicks,day.order_clicks].forEach(value=>{const cell=document.createElement("td");cell.textContent=value??"—";row.appendChild(cell)});return row}));
  }else{
    const row=document.createElement("tr"),cell=document.createElement("td");cell.colSpan=5;cell.textContent="История 30 дней временно недоступна";row.appendChild(cell);$("historyRows").replaceChildren(row);
  }
  $("state").hidden=true;$("content").hidden=false;
}

async function load(force=false){
  const generation=++loadGeneration;
  $("refresh").disabled=true;$("state").hidden=false;$("state").classList.remove("error");$("state").textContent="Загружаем статистику…";
  try{
    if(force) periods.clear();
    const requestedPeriod=selectedPeriod;
    if(!periods.has(requestedPeriod)) periods.set(requestedPeriod,await getPeriod(requestedPeriod));
    if(generation!==loadGeneration||requestedPeriod!==selectedPeriod)return;
    render(periods.get(requestedPeriod));
    if(requestedPeriod!=="30d"&&!periods.has("30d")){
      try{
        periods.set("30d",await getPeriod("30d"));
        if(generation===loadGeneration&&requestedPeriod===selectedPeriod)render(periods.get(requestedPeriod));
      }catch(historyError){
        if(generation===loadGeneration&&requestedPeriod===selectedPeriod)render(periods.get(requestedPeriod));
      }
    }
  }catch(error){if(generation===loadGeneration){$("content").hidden=true;$("state").hidden=false;$("state").classList.add("error");$("state").textContent=`Не удалось загрузить статистику: ${error.message}`}}
  finally{if(generation===loadGeneration)$("refresh").disabled=false}
}

document.querySelectorAll("[data-period]").forEach(button=>button.addEventListener("click",()=>{selectedPeriod=button.dataset.period;document.querySelectorAll("[data-period]").forEach(item=>item.classList.toggle("active",item===button));load()}));
$("refresh").addEventListener("click",()=>{periods.clear();load(true)});
load();

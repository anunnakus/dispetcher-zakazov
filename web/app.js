const TITLES = {
  today: "Сегодня",
  orders: "Заказы",
  templates: "Шаблоны",
  settings: "Настройки",
  journal: "Журнал",
  help: "Как управлять",
};

const STATUS_FILTERS = [
  ["all", "Все"],
  ["awaiting_payment", "Ждёт оплату"],
  ["paid", "Оплачен"],
  ["assembling", "В сборке"],
  ["shipped", "В доставке"],
  ["ready_pickup", "Самовывоз"],
  ["problem", "Проблема"],
  ["closed", "Закрыт"],
];

const state = { page: "today", status: "all", query: "", orderId: null };

const view = document.querySelector("#view");
const title = document.querySelector("#title");
const headerActions = document.querySelector("#header-actions");
const toast = document.querySelector("#toast");

document.querySelector("nav").addEventListener("click", (event) => {
  const button = event.target.closest("button");
  if (!button) return;
  document.querySelectorAll("nav button").forEach((item) => item.classList.remove("active"));
  button.classList.add("active");
  openPage(button.dataset.page);
});

function openPage(page) {
  state.page = page;
  title.textContent = TITLES[page];
  headerActions.innerHTML = "";
  if (page === "today") loadToday();
  if (page === "orders") loadOrders();
  if (page === "templates") loadTemplates();
  if (page === "settings") loadSettings();
  if (page === "journal") loadJournal();
  if (page === "help") renderHelp();
}

function showError(error) {
  toast.textContent = error.message || String(error);
  toast.classList.remove("hidden");
  clearTimeout(showError.timer);
  showError.timer = setTimeout(() => toast.classList.add("hidden"), 4200);
}

async function api(url, options) {
  const response = await fetch(url, options);
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(data.error || "Не получилось");
  return data;
}

function money(order) {
  return order.amount_display || order.amount || "0";
}

function tag(status, label) {
  return `<span class="tag ${status}">${label || status}</span>`;
}

const messageTexts = new Map();

function remember(message) {
  messageTexts.set(String(message.id), message.body);
}

async function loadToday() {
  headerActions.innerHTML = `
    <button class="btn-ghost" id="demo">Учебные заказы</button>
    <button class="btn" id="add">Новый заказ</button>`;
  document.querySelector("#demo").onclick = async () => {
    await api("/api/demo", { method: "POST" });
    loadToday();
  };
  document.querySelector("#add").onclick = () => loadOrders(true);
  let data;
  try {
    data = await api("/api/today");
  } catch (error) {
    showError(error);
    return;
  }
  data.drafts.forEach(remember);
  const counts = data.counts;
  view.innerHTML = `
    <div class="cards">
      <div class="card"><span>Открытых заказов</span><b>${counts.open}</b></div>
      <div class="card"><span>Задач вам и складу</span><b>${counts.tasks}</b></div>
      <div class="card"><span>Сообщений ждут отправки</span><b>${counts.drafts}</b></div>
      <div class="card"><span>Проблем</span><b>${counts.problem}</b></div>
    </div>
    <div class="layout">
      <div class="panel">
        <h2>Сделать руками</h2>
        <div class="list">${data.tasks.length ? data.tasks.map(taskItem).join("") : "<p class='muted'>Пусто. Обычные заказы программа уже разложила по статусам.</p>"}</div>
      </div>
      <div class="panel">
        <h2>Сообщения</h2>
        <div class="list">${data.drafts.length ? data.drafts.map(draftItem).join("") : "<p class='muted'>Нет черновиков. Включите отправку в настройках или копируйте текст из карточки заказа.</p>"}</div>
      </div>
    </div>
    <div class="panel" style="margin-top:14px">
      <h2>Держать в поле зрения</h2>
      <div class="list">${data.attention.map(orderButton).join("") || "<p class='muted'>Нет зависших заказов.</p>"}</div>
    </div>`;
  bindOrderButtons();
  view.querySelectorAll("[data-done]").forEach((button) => {
    button.onclick = async (event) => {
      event.stopPropagation();
      await api(`/api/tasks/${button.dataset.done}/done`, { method: "POST" });
      loadToday();
    };
  });
  view.querySelectorAll("[data-copy-id]").forEach((button) => {
    button.onclick = () => copyMessage(messageTexts.get(button.dataset.copyId), button.dataset.copyId);
  });
  view.querySelectorAll("[data-send]").forEach((button) => {
    button.onclick = async () => {
      try {
        await api(`/api/messages/${button.dataset.send}/send`, { method: "POST" });
        loadToday();
      } catch (error) {
        showError(error);
      }
    };
  });
}

function taskItem(task) {
  return `<div class="item"><div><b>${escapeHtml(task.title)}</b><br><small>${escapeHtml(task.number)} · ${escapeHtml(task.customer || "")}</small></div><button class="btn-ghost" data-done="${task.id}">Готово</button></div>`;
}

function draftItem(message) {
  const failed = message.status === "failed" ? `<small>${escapeHtml(message.error || "")}</small>` : "";
  return `<div class="item"><div><b>${escapeHtml(message.number)}</b> ${tag(message.status, message.status === "failed" ? "ошибка" : "черновик")}<br><small>${escapeHtml(message.body.slice(0, 140))}</small>${failed}</div><div class="row"><button class="btn-ghost" data-copy-id="${message.id}">Копировать</button><button class="btn" data-send="${message.id}">Отправить</button></div></div>`;
}

function orderButton(order) {
  return `<button class="order" data-order="${order.id}"><b>${escapeHtml(order.number)}</b> ${tag(order.status, order.status_label)}<br><small>${escapeHtml(order.customer || "без имени")} · ${money(order)} ₽ · ${escapeHtml(order.items || "")}</small></button>`;
}

function bindOrderButtons() {
  view.querySelectorAll("[data-order]").forEach((button) => {
    button.onclick = () => openOrder(button.dataset.order);
  });
}

async function loadOrders(focusForm) {
  headerActions.innerHTML = `<button class="btn" id="reload">Обновить</button>`;
  document.querySelector("#reload").onclick = () => loadOrders();
  const data = await api(`/api/orders?status=${encodeURIComponent(state.status)}&q=${encodeURIComponent(state.query)}`);
  view.innerHTML = `
    <div class="split">
      <form id="create" class="panel">
        <h2>Новый заказ</h2>
        ${field("number", "Номер заказа")}
        ${field("customer", "Клиент")}
        ${field("phone", "Телефон")}
        ${field("email", "Почта, если есть")}
        ${field("items", "Состав", "textarea")}
        <div class="grid-2">
          ${field("amount", "Сумма")}
          <label><span>Доставка</span><select name="delivery_type"><option value="transport">Транспортная</option><option value="courier">Курьер</option><option value="pickup">Самовывоз</option></select></label>
        </div>
        ${field("address", "Адрес")}
        <label class="check"><input type="checkbox" name="paid"> Уже оплачен</label>
        <button class="btn" type="submit">Создать и подготовить сообщения</button>
      </form>
      <div>
        <div class="filters">${STATUS_FILTERS.map(([id, label]) => `<button class="chip ${state.status === id ? "on" : ""}" data-status="${id}">${label}</button>`).join("")}</div>
        <input class="search" id="q" placeholder="Номер, телефон, имя, товар" value="${escapeAttr(state.query)}">
        <div class="list" style="margin-top:10px">${data.orders.map(orderButton).join("") || "<p class='muted'>Заказов нет.</p>"}</div>
      </div>
    </div>`;
  bindOrderButtons();
  view.querySelectorAll("[data-status]").forEach((button) => {
    button.onclick = () => { state.status = button.dataset.status; loadOrders(); };
  });
  const search = document.querySelector("#q");
  search.oninput = () => { state.query = search.value; clearTimeout(search.timer); search.timer = setTimeout(() => loadOrders(), 250); };
  document.querySelector("#create").onsubmit = async (event) => {
    event.preventDefault();
    const form = new FormData(event.target);
    const payload = Object.fromEntries(form.entries());
    payload.status = form.get("paid") ? "paid" : "awaiting_payment";
    delete payload.paid;
    try {
      const order = await api("/api/orders", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) });
      openOrder(order.id);
    } catch (error) {
      showError(error);
    }
  };
  if (focusForm) document.querySelector("[name=number]").focus();
}

async function openOrder(id) {
  state.orderId = id;
  state.page = "orders";
  title.textContent = "Заказ";
  const order = await api(`/api/orders/${id}`);
  order.messages.forEach(remember);
  headerActions.innerHTML = `<button class="btn-ghost" id="back">К списку</button>`;
  document.querySelector("#back").onclick = () => loadOrders();
  const next = nextActions(order);
  view.innerHTML = `
    <div class="layout">
      <div class="panel">
        <h2>${escapeHtml(order.number)} ${tag(order.status, order.status_label)}</h2>
        <p>${escapeHtml(order.customer || "Без имени")} · ${escapeHtml(order.phone_display || "нет телефона")} · ${escapeHtml(order.email || "нет почты")}</p>
        <p>${escapeHtml(order.items || "Состав не указан")}<br><b>${money(order)} ₽</b> · ${escapeHtml(order.delivery_label)}</p>
        <p class="muted">${escapeHtml(order.address || "")}</p>
        <div class="row">${next}</div>
        <form id="edit" style="margin-top:12px">
          <div class="grid-2">
            ${field("track", "Трек", "input", order.track || "")}
            ${field("phone", "Телефон", "input", order.phone || "")}
          </div>
          ${field("comment", "Комментарий", "textarea", order.comment || "")}
          <div class="row">
            <button class="btn-ghost" type="submit">Сохранить поля</button>
            <button class="btn-bad" type="button" id="remove">Удалить заказ</button>
          </div>
        </form>
        <h2 style="margin-top:18px">Задачи</h2>
        ${order.tasks.filter((task) => !task.done).map(taskItem).join("") || "<p class='muted'>Открытых задач нет.</p>"}
      </div>
      <div class="panel">
        <h2>Что программа сказала клиенту</h2>
        ${order.messages.map(messageBlock).join("") || "<p class='muted'>Сообщений ещё нет.</p>"}
      </div>
    </div>`;
  view.querySelectorAll("[data-status-set]").forEach((button) => {
    button.onclick = () => setStatus(order, button.dataset.statusSet);
  });
  view.querySelectorAll("[data-done]").forEach((button) => {
    button.onclick = async () => {
      await api(`/api/tasks/${button.dataset.done}/done`, { method: "POST" });
      openOrder(id);
    };
  });
  view.querySelector("#edit").onsubmit = async (event) => {
    event.preventDefault();
    const form = Object.fromEntries(new FormData(event.target).entries());
    try {
      await api(`/api/orders/${id}/update`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(form) });
      openOrder(id);
    } catch (error) { showError(error); }
  };
  document.querySelector("#remove").onclick = async () => {
    if (!confirm("Удалить заказ и его сообщения?")) return;
    await api(`/api/orders/${id}/delete`, { method: "POST" });
    loadOrders();
  };
  view.querySelectorAll("[data-copy-id]").forEach((button) => {
    button.onclick = () => copyMessage(messageTexts.get(button.dataset.copyId), button.dataset.copyId);
  });
  view.querySelectorAll("[data-send]").forEach((button) => {
    button.onclick = async () => {
      try {
        await api(`/api/messages/${button.dataset.send}/send`, { method: "POST" });
        openOrder(id);
      } catch (error) { showError(error); }
    };
  });
  view.querySelectorAll("[data-mark]").forEach((button) => {
    button.onclick = async () => {
      await api(`/api/messages/${button.dataset.mark}/mark`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ status: button.dataset.markStatus }),
      });
      openOrder(id);
    };
  });
}

function nextActions(order) {
  const map = {
    new: [["awaiting_payment", "Ждёт оплату"], ["paid", "Уже оплачен"]],
    awaiting_payment: [["paid", "Оплата пришла"]],
    paid: [["assembling", "Передан в сборку"]],
    assembling: order.delivery_type === "pickup" ? [["ready_pickup", "Готов к выдаче"]] : [["shipped", "Передан в доставку"]],
    shipped: [["closed", "Закрыть"]],
    ready_pickup: [["closed", "Выдан"]],
    problem: [["awaiting_payment", "Вернуть к оплате"], ["assembling", "Вернуть в сборку"]],
    closed: [],
  };
  const main = (map[order.status] || []).map(([status, label]) => `<button class="btn" data-status-set="${status}">${label}</button>`).join("");
  const problem = order.status === "problem" ? "" : `<button class="btn-ghost" data-status-set="problem">Проблема</button>`;
  const close = order.status === "closed" ? "" : `<button class="btn-ghost" data-status-set="closed">Закрыть</button>`;
  return main + problem + close;
}

async function setStatus(order, status) {
  const payload = { status };
  if (status === "shipped") {
    const track = prompt("Трек-номер", order.track || "");
    if (!track) return;
    payload.track = track.trim();
  }
  if (status === "problem") {
    const comment = prompt("В чём проблема?", order.comment || "");
    if (!comment) return;
    payload.comment = comment.trim();
  }
  try {
    await api(`/api/orders/${order.id}/status`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    openOrder(order.id);
  } catch (error) {
    showError(error);
  }
}

function messageBlock(message) {
  const labels = { draft: "черновик", sent: "отправлено", failed: "ошибка", copied: "скопировано", skipped: "пропущено" };
  return `<article class="message">${tag(message.status, labels[message.status] || message.status)} <b>${escapeHtml(message.channel)}</b><pre>${escapeHtml(message.body)}</pre>${message.error ? `<small>${escapeHtml(message.error)}</small>` : ""}<div class="row"><button class="btn-ghost" data-copy-id="${message.id}">Копировать</button><button class="btn" data-send="${message.id}">Отправить</button><button class="btn-ghost" data-mark="${message.id}" data-mark-status="copied">Я уже отправил</button><button class="btn-ghost" data-mark="${message.id}" data-mark-status="skipped">Не слать</button></div></article>`;
}

async function copyMessage(text, id) {
  try {
    await navigator.clipboard.writeText(text);
    if (id) {
      await api(`/api/messages/${id}/mark`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ status: "copied" }),
      });
    }
    toast.textContent = "Текст скопирован";
    toast.classList.remove("hidden");
    if (state.orderId && state.page === "orders" && title.textContent === "Заказ") openOrder(state.orderId);
    else if (state.page === "today") loadToday();
  } catch (error) {
    showError(error);
  }
}

async function loadTemplates() {
  const data = await api("/api/templates");
  view.innerHTML = data.templates.map((template) => `
    <form class="panel template" data-key="${template.key}" style="margin-bottom:12px">
      <h2>${escapeHtml(template.title)}</h2>
      <p class="muted">Подстановки: {customer} {number} {items} {amount} {track} {address} {delivery} {comment} {phone} {store} {pickup_address} {pickup_hours} {payment_details}</p>
      <textarea name="body">${escapeHtml(template.body)}</textarea>
      <button class="btn" type="submit">Сохранить шаблон</button>
    </form>`).join("");
  view.querySelectorAll(".template").forEach((form) => {
    form.onsubmit = async (event) => {
      event.preventDefault();
      await api(`/api/templates/${form.dataset.key}`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ body: new FormData(form).get("body") }),
      });
      toast.textContent = "Шаблон сохранён. Новые заказы возьмут этот текст.";
      toast.classList.remove("hidden");
    };
  });
}

async function loadSettings() {
  const settings = await api("/api/settings");
  headerActions.innerHTML = "";
  view.innerHTML = `
    <form id="settings" class="panel">
      <h2>Магазин</h2>
      <div class="grid-2">
        ${setting("store_name", "Название в сообщениях", settings)}
        ${setting("manager_name", "Ваше имя", settings)}
        ${setting("store_phone", "Телефон магазина", settings)}
        ${setting("pickup_hours", "Часы самовывоза", settings)}
      </div>
      ${setting("pickup_address", "Адрес самовывоза", settings)}
      ${setting("payment_details", "Как оплатить", settings, "textarea")}
      <h2>Когда напоминать</h2>
      <div class="grid-2">
        ${setting("payment_reminder_hours", "Часов до напоминания об оплате", settings)}
        ${setting("payment_task_hours", "Часов до задачи вам, если не оплатили", settings)}
        ${setting("pickup_reminder_hours", "Часов до напоминания о самовывозе", settings)}
        ${setting("pickup_task_hours", "Часов до задачи, если не забрали", settings)}
      </div>
      <label class="check"><input type="checkbox" name="auto_send" ${settings.auto_send === "1" ? "checked" : ""}> Отправлять самой, как только канал включён</label>
      <h2>SMS через SMS.ru</h2>
      <label class="check"><input type="checkbox" name="sms_enabled" ${settings.sms_enabled === "1" ? "checked" : ""}> Включить SMS</label>
      ${setting("smsru_api_id", settings.smsru_api_id_set === "1" ? "Ключ SMS.ru уже сохранён, вставьте новый только чтобы заменить" : "Ключ api_id", settings, "input", "", true)}
      <h2>Почта Gmail</h2>
      <label class="check"><input type="checkbox" name="email_enabled" ${settings.email_enabled === "1" ? "checked" : ""}> Включить почту, если SMS выключены или телефона нет</label>
      <div class="grid-2">
        ${setting("smtp_host", "Сервер", settings)}
        ${setting("smtp_port", "Порт", settings)}
        ${setting("smtp_user", "Логин Gmail", settings)}
        ${setting("smtp_from", "Адрес отправителя", settings)}
      </div>
      ${setting("smtp_password", settings.smtp_password_set === "1" ? "Пароль приложения уже сохранён" : "Пароль приложения Gmail", settings, "input", "", true)}
      <h2>Telegram складу</h2>
      <label class="check"><input type="checkbox" name="telegram_enabled" ${settings.telegram_enabled === "1" ? "checked" : ""}> Писать складу при оплате</label>
      ${setting("telegram_token", settings.telegram_token_set === "1" ? "Токен бота уже сохранён" : "Токен бота", settings, "input", "", true)}
      ${setting("telegram_warehouse_chat", "ID чата склада", settings)}
      <h2>Импорт из 1С</h2>
      <p class="muted">Сохраните выгрузку как CSV и положите файл в папку «входящие» рядом с программой или вставьте текст ниже. Повторный импорт того же номера обновляет заказ и не шлёт сообщение второй раз, если статус не изменился.</p>
      <textarea id="csv" placeholder="номер;клиент;телефон;состав;сумма;доставка;статус"></textarea>
      <div class="row" style="margin-top:10px">
        <button class="btn" type="submit">Сохранить настройки</button>
        <button class="btn-ghost" type="button" id="do-import">Импортировать CSV</button>
        <button class="btn-ghost" type="button" id="clear-demo">Удалить учебные заказы</button>
      </div>
    </form>`;
  document.querySelector("#settings").onsubmit = async (event) => {
    event.preventDefault();
    const form = new FormData(event.target);
    const payload = {};
    for (const [key, value] of form.entries()) {
      if (event.target.elements[key] && event.target.elements[key].type === "checkbox") continue;
      payload[key] = value;
    }
    ["auto_send", "sms_enabled", "email_enabled", "telegram_enabled"].forEach((key) => {
      payload[key] = event.target.elements[key].checked ? "1" : "0";
    });
    try {
      await api("/api/settings", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) });
      toast.textContent = "Настройки сохранены";
      toast.classList.remove("hidden");
      loadSettings();
    } catch (error) { showError(error); }
  };
  document.querySelector("#do-import").onclick = async () => {
    try {
      const result = await api("/api/import", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ csv: document.querySelector("#csv").value }),
      });
      toast.textContent = `Загружено строк: ${result.imported}`;
      toast.classList.remove("hidden");
    } catch (error) { showError(error); }
  };
  document.querySelector("#clear-demo").onclick = async () => {
    await api("/api/demo/clear", { method: "POST" });
    toast.textContent = "Учебные заказы удалены";
    toast.classList.remove("hidden");
  };
}

async function loadJournal() {
  const data = await api("/api/journal");
  view.innerHTML = `<div class="panel list">${data.journal.map((row) => `<div class="item"><div>${escapeHtml(row.text)}<br><small>${escapeHtml(row.created_at.replace("T", " "))}</small></div></div>`).join("") || "<p class='muted'>Пока пусто.</p>"}</div>`;
}

function renderHelp() {
  view.innerHTML = `
    <article class="help panel">
      <p>Программа сама готовит клиентские сообщения и задачи, когда вы меняете статус заказа. Вам остаются исключения: нет товара, не стыкуется техника, претензия, странная оплата.</p>
      <h2>Каждое утро</h2>
      <ol>
        <li>На рабочем Windows дважды щёлкните «Запуск.bat», если чёрное окно ещё не открыто. Права администратора не нужны.</li>
        <li>Выгрузите из 1С новые и изменившиеся заказы в CSV и положите файл в папку «входящие». Либо вставьте таблицу в настройках.</li>
        <li>На экране «Сегодня» закройте задачи и либо отправьте сообщения кнопкой, либо скопируйте текст в Манго или почту.</li>
      </ol>
      <h2>В течение дня</h2>
      <ul>
        <li>Оплата пришла — откройте заказ и нажмите «Оплата пришла». Программа напишет клиенту и поставит складу задачу собрать.</li>
        <li>Склад собрал самовывоз — «Готов к выдаче».</li>
        <li>Отдали курьеру или в ТК — «Передан в доставку» и трек. Без трека сообщение не создаётся.</li>
        <li>Клиент забрал или получил — «Закрыть» или «Выдан».</li>
        <li>Что-то не так — «Проблема» и одна строка, что случилось. Программа поставит задачу вам и сама клиенту ничего не выдумает.</li>
      </ul>
      <h2>Сама, без кнопки</h2>
      <ul>
        <li>Через 24 часа без оплаты готовит напоминание. Через 48 часов ставит задачу вам.</li>
        <li>Через 24 часа невостребованного самовывоза пишет клиенту. Через 72 часа ставит задачу вам.</li>
        <li>Пока программа запущена, проверка идёт каждую минуту. Если компьютер спал, проверка догонит пропущенное при следующем открытии.</li>
        <li>Повторно тот же текст по тому же заказу не создаётся.</li>
      </ul>
      <h2>Чтобы уходило само</h2>
      <p>В настройках включите «Отправлять самой» и один канал. SMS имеет приоритет: нужен ключ SMS.ru. Если SMS выключены, а почта включена и в заказе есть адрес, уйдёт письмо через Gmail. Складу нужен свой бот Telegram и ID общего чата. Пароль Gmail — это пароль приложения, не обычный пароль от ящика.</p>
      <p>Пока каналы пустые, программа всё равно пишет готовый текст. Его копируют в SMS Манго или в Telegram. После копирования сообщение уходит из очереди «Сегодня».</p>
    </article>`;
}

function field(name, label, type = "input", value = "") {
  const control = type === "textarea"
    ? `<textarea name="${name}">${escapeHtml(value)}</textarea>`
    : `<input name="${name}" value="${escapeAttr(value)}">`;
  return `<label><span>${label}</span>${control}</label>`;
}

function setting(name, label, settings, type = "input", value, password = false) {
  const current = value || settings[name] || "";
  if (type === "textarea") return field(name, label, "textarea", current);
  return `<label><span>${label}</span><input name="${name}" ${password ? "type='password'" : ""} value="${escapeAttr(current)}" autocomplete="off"></label>`;
}

function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>"']/g, (char) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[char]));
}

function escapeAttr(value) {
  return escapeHtml(value);
}

openPage("today");

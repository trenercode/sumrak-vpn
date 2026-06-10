const tg = window.Telegram?.WebApp;
const state = { me: null, devices: [], referral: null, clients: [], supportUrl: "" };
const platforms = {
  ios: "iPhone / iPad", android: "Android", windows: "Windows",
  macos: "macOS", android_tv: "Android TV", other: "Другое"
};

if (tg) {
  tg.ready();
  tg.expand();
  tg.setHeaderColor("#070711");
  tg.setBackgroundColor("#070711");
}

async function api(path, options = {}) {
  const response = await fetch(`/api/webapp${path}`, {
    ...options,
    headers: {
      "Content-Type": "application/json",
      "X-Telegram-Init-Data": tg?.initData || "",
      ...options.headers,
    },
  });
  if (!response.ok) {
    const error = await response.json().catch(() => ({ detail: "Ошибка соединения" }));
    throw new Error(error.detail || "Ошибка соединения");
  }
  return response.status === 204 ? null : response.json();
}

function toast(text) {
  const node = document.querySelector("#toast");
  node.textContent = text;
  node.classList.add("show");
  setTimeout(() => node.classList.remove("show"), 2200);
}

function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>"']/g, (char) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#039;",
  })[char]);
}

function popup(text) {
  tg?.HapticFeedback?.notificationOccurred("error");
  if (tg?.showAlert) tg.showAlert(text); else alert(text);
}

async function copy(text, message = "Ссылка скопирована") {
  await navigator.clipboard.writeText(text);
  tg?.HapticFeedback?.notificationOccurred("success");
  toast(message);
}

function navigate(screen) {
  document.querySelectorAll(".screen").forEach((node) => node.classList.toggle("active", node.dataset.screen === screen));
  document.querySelectorAll(".bottom-nav button").forEach((node) => node.classList.toggle("active", node.dataset.nav === screen));
  window.scrollTo({ top: 0, behavior: "smooth" });
  if (screen === "devices") loadDevices();
  if (screen === "instructions") loadClients();
  if (screen === "referral") loadReferral();
}

function date(value) {
  return value ? new Intl.DateTimeFormat("ru-RU", { day: "numeric", month: "long", year: "numeric" }).format(new Date(value)) : "не указана";
}

function statusLabel(status) {
  return { active: "Активна", trial: "Пробный период", expired: "Доступ завершён" }[status] || status;
}

async function loadMe() {
  state.me = await api("/me");
  state.supportUrl = state.me.support_url;
  const active = state.me.status !== "expired";
  document.querySelector("#hero-title").textContent = active ? "VPN готов к работе" : "Доступ завершён";
  document.querySelector("#hero-subtitle").textContent = active ? `${statusLabel(state.me.status)} · до ${date(state.me.access_until)}` : "Продлите подписку для подключения";
  document.querySelector("#status-icon").textContent = active ? "✓" : "×";
  document.querySelector("#days-left").textContent = state.me.days_left;
  document.querySelector("#device-count").textContent = `${state.me.devices_used}/${state.me.device_limit}`;
  document.querySelector("#subscription-badge").textContent = statusLabel(state.me.status);
  document.querySelector("#subscription-title").textContent = active ? `${state.me.days_left} дней доступа` : "Подписка не активна";
  document.querySelector("#subscription-date").textContent = `Действует до: ${date(state.me.access_until)}`;
  document.querySelector("#subscription-devices").textContent = `Используется устройств: ${state.me.devices_used} из ${state.me.device_limit}`;
  document.querySelector("#subscription-progress").style.width = `${Math.min(100, state.me.devices_used / state.me.device_limit * 100)}%`;
}

async function loadDevices() {
  state.devices = await api("/devices");
  const list = document.querySelector("#device-list");
  if (!state.devices.length) {
    list.innerHTML = `<div class="empty">Устройств пока нет.<br>Добавьте первое и получите ссылку подписки.</div>`;
    return;
  }
  list.innerHTML = state.devices.map((device) => `
    <article class="list-card">
      <div class="list-card-head"><div><h3>${escapeHtml(device.name)}</h3><p class="muted">${escapeHtml(device.platform_label)}</p></div><span class="chip">${device.servers.filter(s => s.active).length} стран</span></div>
      <div class="server-list">${device.servers.map(s => `<span>${s.active ? "●" : "○"} ${escapeHtml(s.server)}</span>`).join("")}</div>
      <div class="card-actions"><button class="mini-button" data-copy-device="${device.id}">Скопировать подписку</button><button class="mini-button danger" data-delete-device="${device.id}">Удалить</button></div>
    </article>`).join("");
}

async function addDevice(platform) {
  try {
    const device = await api("/devices", { method: "POST", body: JSON.stringify({ platform }) });
    closeSheet();
    await copy(device.subscription_url, "Устройство добавлено, ссылка скопирована");
    await Promise.all([loadDevices(), loadMe()]);
    navigate("devices");
  } catch (error) { popup(error.message); }
}

async function removeDevice(id) {
  const confirmed = tg?.showConfirm ? await new Promise(resolve => tg.showConfirm("Удалить устройство и отключить его профили?", resolve)) : confirm("Удалить устройство?");
  if (!confirmed) return;
  try {
    await api(`/devices/${id}`, { method: "DELETE" });
    toast("Устройство удалено");
    await Promise.all([loadDevices(), loadMe()]);
  } catch (error) { popup(error.message); }
}

function openAddDevice() {
  document.querySelector("#sheet-title").textContent = "На какое устройство?";
  document.querySelector("#sheet-content").innerHTML = `<div class="platform-grid">${Object.entries(platforms).map(([id, label]) => `<button class="platform-button" data-create-platform="${id}">${label}</button>`).join("")}</div>`;
  document.querySelector("#sheet").classList.add("open");
}

function closeSheet() { document.querySelector("#sheet").classList.remove("open"); }

async function loadReferral() {
  state.referral = await api("/referral");
  document.querySelector("#ref-invited").textContent = state.referral.invited;
  document.querySelector("#ref-days").textContent = state.referral.days;
  const list = document.querySelector("#referral-list");
  list.innerHTML = state.referral.recent.length ? state.referral.recent.map(item => `
    <article class="list-card"><div class="list-card-head"><div><h3>${escapeHtml(item.name)}</h3><p class="muted">${date(item.created_at)}</p></div><span class="chip">${escapeHtml(item.status)}</span></div></article>`).join("") : `<div class="empty">Здесь появятся приглашённые друзья.</div>`;
}

async function loadClients(platform = null) {
  const chosen = platform || "ios";
  document.querySelector("#platform-list").innerHTML = Object.entries(platforms).map(([id, label]) => `<button class="platform-button ${id === chosen ? "active" : ""}" data-client-platform="${id}">${label}</button>`).join("");
  state.clients = await api(`/clients?platform=${chosen}`);
  const list = document.querySelector("#client-list");
  list.innerHTML = state.clients.length ? state.clients.map(item => `
    <article class="list-card"><div class="list-card-head"><div><h3>${escapeHtml(item.name)}</h3><p class="muted">${escapeHtml(item.description || "VPN-клиент для Sumrak VPN")}</p></div><span class="chip">${escapeHtml(item.platform_label)}</span></div>
    <p>${escapeHtml(item.instruction).replaceAll("\n", "<br>")}</p><button class="mini-button wide" data-open-url="${escapeHtml(item.download_url)}">Установить приложение</button></article>`).join("") : `<div class="empty">Инструкции для этой платформы скоро появятся.</div>`;
}

function openUrl(url) {
  if (!url) return popup("Ссылка пока не настроена");
  if (url.startsWith("https://t.me/") && tg?.openTelegramLink) tg.openTelegramLink(url);
  else if (tg?.openLink) tg.openLink(url);
  else window.open(url, "_blank");
}

document.addEventListener("click", async (event) => {
  const button = event.target.closest("button");
  if (!button) return;
  if (button.dataset.nav) navigate(button.dataset.nav);
  if (button.dataset.action === "add-device") openAddDevice();
  if (button.dataset.action === "close-sheet") closeSheet();
  if (button.dataset.action === "support") openUrl(state.supportUrl);
  if (button.dataset.action === "connect") state.devices.length ? navigate("devices") : openAddDevice();
  if (button.dataset.action === "renew") popup("Онлайн-оплата появится скоро. Сейчас продлить доступ можно через поддержку.");
  if (button.dataset.action === "copy-referral") copy(state.referral.link, "Реферальная ссылка скопирована");
  if (button.dataset.createPlatform) addDevice(button.dataset.createPlatform);
  if (button.dataset.clientPlatform) loadClients(button.dataset.clientPlatform);
  if (button.dataset.copyDevice) {
    const device = state.devices.find(item => item.id === button.dataset.copyDevice);
    if (device) copy(device.subscription_url);
  }
  if (button.dataset.deleteDevice) removeDevice(button.dataset.deleteDevice);
  if (button.dataset.openUrl) openUrl(button.dataset.openUrl);
});
document.querySelector("#sheet").addEventListener("click", (event) => { if (event.target.id === "sheet") closeSheet(); });

Promise.all([loadMe(), loadDevices()]).catch((error) => {
  document.querySelector("#hero-title").textContent = "Откройте из Telegram";
  document.querySelector("#hero-subtitle").textContent = "Web App доступен через кнопку в боте";
  popup(error.message);
});

/* Мини-приложение бота торговых сигналов.

   Один запрос за данными, четыре вкладки, никаких библиотек. Всё, что
   можно нажать, — крупное; всё, что можно не показывать новичку, —
   не показано.

   Правило по текстам то же, что и в боте: ни одного слова, которое
   пришлось бы гуглить. */

const tg = window.Telegram ? window.Telegram.WebApp : null;

const state = {
  data: null,        // ответ /api/overview
  tab: 'signals',
  instruments: null, // список инструментов, подгружается при первом открытии
  busy: false,
};

/* ------------------------------------------------------------------ */
/* Общение с сервером                                                  */
/* ------------------------------------------------------------------ */

async function api(path, options = {}) {
  const headers = Object.assign(
    { 'Content-Type': 'application/json' },
    options.headers || {},
  );
  if (tg && tg.initData) headers['X-Telegram-Init-Data'] = tg.initData;

  const response = await fetch(path, Object.assign({}, options, { headers }));
  if (!response.ok) {
    let message = 'Не получилось. Попробуйте ещё раз.';
    try {
      const body = await response.json();
      if (body && body.detail) message = body.detail;
    } catch (e) { /* ответ без тела — оставим общий текст */ }
    throw new Error(message);
  }
  return response.status === 204 ? null : response.json();
}

async function load() {
  try {
    state.data = await api('/api/overview');
    render();
  } catch (error) {
    document.getElementById('screen').innerHTML = `
      <div class="empty">
        <span class="empty-emoji">🔌</span>
        <b>Нет связи с ботом</b>
        ${escape(error.message)}
      </div>`;
    document.getElementById('pulse').textContent = 'связи нет';
  }
}

/* ------------------------------------------------------------------ */
/* Форматирование                                                      */
/* ------------------------------------------------------------------ */

function money(value) {
  if (value === null || value === undefined) return '—';
  const abs = Math.abs(value);
  const digits = abs >= 100 ? 2 : (abs >= 1 ? 4 : 6);
  return value.toFixed(digits).replace(/\B(?=(\d{3})+(?!\d))/g, ' ');
}

function sum(value) {
  if (value === null || value === undefined) return '—';
  const rounded = Math.round(value * 100) / 100;
  const text = (Number.isInteger(rounded) ? rounded.toFixed(0) : rounded.toFixed(2));
  return text.replace(/\B(?=(\d{3})+(?!\d))/g, ' ');
}

function signedSum(value) {
  if (value === null || value === undefined) return '—';
  return (value >= 0 ? '+' : '−') + sum(Math.abs(value)) + ' $';
}

function pct(value) {
  if (value === null || value === undefined) return '—';
  return (value >= 0 ? '+' : '−') + Math.abs(value).toFixed(2) + '%';
}

function ago(ts) {
  if (!ts) return 'ещё ни разу';
  const seconds = Math.floor(Date.now() / 1000) - ts;
  if (seconds < 90) return 'только что';
  if (seconds < 3600) return `${Math.floor(seconds / 60)} мин назад`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)} ч назад`;
  return `${Math.floor(seconds / 86400) } дн назад`;
}

function clock(ts) {
  if (!ts) return '';
  const date = new Date(ts * 1000);
  return date.toLocaleTimeString('ru-RU', { hour: '2-digit', minute: '2-digit' });
}

/* «1 из 1 сигнала», но «6 из 9 сигналов» — иначе текст выглядит машинным */
function signalsWord(count) {
  return count % 10 === 1 && count % 100 !== 11 ? 'сигнала' : 'сигналов';
}

function escape(text) {
  return String(text === null || text === undefined ? '' : text)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}

/* ------------------------------------------------------------------ */
/* Отрисовка                                                           */
/* ------------------------------------------------------------------ */

function render() {
  const data = state.data;
  if (!data) return;

  const name = data.user && data.user.name ? `, ${escape(data.user.name)}` : '';
  document.getElementById('greeting').textContent = `Привет${name}`;
  renderPulse(data.pulse);

  const screen = document.getElementById('screen');
  const painters = {
    signals: paintSignals,
    stats: paintStats,
    alerts: paintAlerts,
    settings: paintSettings,
  };
  screen.innerHTML = painters[state.tab](data);
  screen.scrollTop = 0;

  document.querySelectorAll('.tab').forEach((tab) => {
    tab.classList.toggle('is-active', tab.dataset.tab === state.tab);
  });
}

function renderPulse(pulse) {
  const node = document.getElementById('pulse');
  node.classList.remove('is-bad', 'is-muted');
  if (!pulse || !pulse.running) {
    node.textContent = 'Бот сейчас не следит за рынком';
    node.classList.add('is-bad');
    return;
  }
  const watching = (pulse.watching || []).join(', ') || 'ничего не выбрано';
  if (pulse.muted_by_news) {
    node.textContent = `Молчу из-за новости: ${pulse.muted_by_news}`;
    node.classList.add('is-muted');
    return;
  }
  node.textContent = `Слежу за: ${watching} · проверил ${ago(pulse.last_scan_at)}`;
}

/* --- вкладка «Сигналы» --- */

function paintSignals(data) {
  let html = '';

  if (!data.active.length) {
    html += `
      <div class="empty">
        <span class="empty-emoji">🎯</span>
        <b>Сейчас сигналов нет</b>
        Это нормально: я пишу только тогда, когда вижу подходящий момент,
        а такие моменты бывают не каждый час.
      </div>`;
  } else {
    html += '<div class="section-title">В работе</div>';
    html += data.active.map(signalCard).join('');
  }

  if (data.recent.length) {
    html += '<div class="section-title">Чем закончились прошлые</div><div class="card">';
    html += data.recent.map((item) => `
      <div class="line">
        <div class="line-main">
          <div class="line-title">${item.icon} ${escape(item.title)}</div>
          <div class="line-sub">${escape(item.status_text)} · ${clock(item.closed_at)}</div>
        </div>
        <div class="line-value ${item.won ? 'up' : (item.lost ? 'down' : '')}">
          ${item.money !== null ? signedSum(item.money) : pct(item.pnl_pct)}
        </div>
      </div>`).join('');
    html += '</div>';
  }
  return html;
}

function signalCard(signal) {
  const isLong = signal.side === 'LONG';
  const now = signal.price
    ? `Сейчас <b>${money(signal.price)}</b> <span class="${signal.pnl_pct >= 0 ? 'up' : 'down'}">${pct(signal.pnl_pct)}</span>`
    : 'Цена сейчас неизвестна';

  let body;
  if (signal.is_binary) {
    body = `
      <div class="rows">
        <div class="row"><span class="row-label">Ставить на</span>
          <span class="row-value">${isLong ? 'рост' : 'падение'}</span></div>
        <div class="row"><span class="row-label">Цена входа</span>
          <span class="row-value">${money(signal.entry)}</span></div>
        <div class="row"><span class="row-label">Итог в</span>
          <span class="row-value">${clock(signal.expiry_at)}</span></div>
        ${signal.payout ? `<div class="row"><span class="row-label">Выплата брокера</span>
          <span class="row-value">${signal.payout.toFixed(0)}%</span></div>` : ''}
      </div>`;
  } else {
    const mark = signal.progress === null ? null : Math.max(2, Math.min(98, signal.progress));
    body = `
      ${mark === null ? '' : `
        <div class="track"><div class="track-mark" style="left:${mark}%"></div></div>
        <div class="track-ends">
          <span>стоп ${money(signal.stop_loss)}</span>
          <span>цель ${money(signal.take_profit)}</span>
        </div>`}
      <div class="rows">
        <div class="row"><span class="row-label">Входить по</span>
          <span class="row-value">${money(signal.entry)}</span></div>
        <div class="row"><span class="row-label">Выйти, если дойдёт до</span>
          <span class="row-value">${money(signal.stop_loss)}</span></div>
        <div class="row"><span class="row-label">Забрать прибыль на</span>
          <span class="row-value">${money(signal.take_profit)}</span></div>
      </div>`;
  }

  const why = signal.reasons.length ? `
    <details class="why">
      <summary>Почему я так решил</summary>
      <ul>${signal.reasons.map((r) => `<li>${escape(r)}</li>`).join('')}</ul>
    </details>` : '';

  return `
    <div class="card">
      <div class="signal-head">
        <span class="side ${isLong ? 'long' : 'short'}">
          ${isLong ? '🟢' : '🔴'} ${escape(signal.side_text)}
        </span>
        <span class="signal-name">${escape(signal.title)}</span>
      </div>
      <div class="signal-now">${now}</div>
      ${body}
      ${signal.size ? `<div class="hint">💰 ${stripTags(signal.size)}</div>` : ''}
      <div class="hint">Уверенность ${signal.confidence}% · сигнал #${signal.id} от ${clock(signal.created_at)}</div>
      ${why}
    </div>`;
}

/* Подсказку про объём бот присылает с разметкой — здесь она не нужна */
function stripTags(text) {
  return escape(String(text).replace(/<[^>]+>/g, ''));
}

/* --- вкладка «Итоги» --- */

function paintStats(data) {
  const s = data.stats;
  if (!s.decided && !s.active) {
    return `
      <div class="empty">
        <span class="empty-emoji">📊</span>
        <b>Пока нечего показывать</b>
        Итоги появятся, когда первые сигналы дойдут до цели или до стопа.
        Обычно на это уходит от нескольких часов до пары дней.
      </div>`;
  }

  const color = s.winrate >= 55 ? 'up' : (s.winrate >= 45 ? '' : 'down');
  let html = `
    <div class="card score">
      <div class="score-value ${color}">${s.decided ? Math.round(s.winrate) + '%' : '—'}</div>
      <div class="score-label">${s.decided
        ? `угадано — ${s.wins} из ${s.decided} ${signalsWord(s.decided)}`
        : 'завершённых сигналов пока нет'}</div>
      ${s.money !== null && s.decided ? `
        <div class="score-money ${s.money >= 0 ? 'up' : 'down'}">${signedSum(s.money)}</div>
        <div class="score-label">столько было бы, если торговать по всем сигналам</div>` : ''}
    </div>

    <div class="counts">
      <div class="count"><div class="count-value up">${s.wins}</div><div class="count-name">до цели</div></div>
      <div class="count"><div class="count-value down">${s.losses}</div><div class="count-name">до стопа</div></div>
      <div class="count"><div class="count-value">${s.active}</div><div class="count-name">в работе</div></div>
    </div>`;

  if (s.by_symbol.length) {
    html += '<div class="section-title">По инструментам</div><div class="card">';
    html += s.by_symbol.map((row) => `
      <div class="bar-row">
        <div class="bar-top">
          <span>${escape(row.title)}</span>
          <span>${Math.round(row.winrate)}% · ${row.wins} из ${row.total}</span>
        </div>
        <div class="bar"><div class="bar-fill" style="width:${Math.max(3, row.winrate)}%"></div></div>
      </div>`).join('');
    html += '</div>';
  }

  if (s.decided < 20) {
    html += `<div class="note">⚠️ Сигналов пока мало, чтобы судить о боте.
      Ориентир появляется после 20–30 завершённых.</div>`;
  }
  return html;
}

/* --- вкладка «Цена» (уведомления) --- */

function paintAlerts(data) {
  let html = `<button class="button" onclick="openAlertSheet()">➕ Сообщить, когда цена дойдёт</button>`;

  if (!data.alerts.length) {
    html += `
      <div class="empty">
        <span class="empty-emoji">🔔</span>
        <b>Уведомлений пока нет</b>
        Это будильник по цене: вы называете число — я пишу, когда рынок
        до него дошёл. Например, если биткоин упадёт на 1%.
      </div>`;
    return html;
  }

  html += '<div class="section-title">Жду вот этого</div><div class="card">';
  html += data.alerts.map((alert) => {
    const what = alert.percent
      ? `${alert.up ? 'вырастет' : 'упадёт'} на ${alert.percent}% — это ${money(alert.price)}`
      : `${alert.up ? 'поднимется до' : 'опустится до'} ${money(alert.price)}`;
    const where = alert.current
      ? `сейчас ${money(alert.current)} · осталось ${alert.distance}%`
      : 'цена сейчас неизвестна';
    return `
      <div class="line">
        <div class="line-main">
          <div class="line-title">${escape(alert.title)} ${what}</div>
          <div class="line-sub">${where}${alert.note ? ' · 📝 ' + escape(alert.note) : ''}</div>
        </div>
        <button class="icon-button" onclick="dropAlert(${alert.id})" aria-label="убрать">✕</button>
      </div>`;
  }).join('');
  html += '</div>';
  html += `<div class="note">Сработавшее уведомление гаснет само.
    Это напоминание о цене, а не совет на сделку.</div>`;
  return html;
}

/* --- вкладка «Настройки» --- */

function paintSettings(data) {
  const s = data.settings;
  const watching = s.symbols.map((item) => escape(item.title)).join(', ') || 'ничего не выбрано';

  let html = `
    <div class="section-title">За чем следить</div>
    <button class="switch" onclick="openInstruments()">
      <span>${watching}</span><span class="switch-state">изменить</span>
    </button>

    <div class="section-title">Как часто сигналы</div>`;

  html += s.presets.map((preset) => `
    <button class="pick ${s.preset === preset.key ? 'is-on' : ''}"
            onclick="choosePreset('${preset.key}')">
      <span class="pick-mark">${s.preset === preset.key ? '✅' : preset.emoji}</span>
      <span class="pick-main">
        <span>${escape(preset.name)}</span>
        <span class="pick-note">${escape(preset.summary)}. Ожидайте ${escape(preset.expect)}</span>
      </span>
    </button>`).join('');

  html += `
    <div class="section-title">Деньги</div>
    <div class="card">
      <div class="row"><span class="row-label">На счёте</span>
        <span class="row-value">${sum(s.deposit)} $</span></div>
      <div class="row"><span class="row-label">Рискую на сделке</span>
        <span class="row-value">${s.risk_per_trade}% — это ${sum(s.risk_money)} $</span></div>
      <div class="hint">От этих чисел я считаю, сколько брать в сделку.
        Сами деньги мне не видны и никуда не уходят.</div>
    </div>
    <div class="chips" style="margin-bottom:14px">
      ${[100, 500, 1000, 5000, 10000].map((value) => `
        <button class="chip ${s.deposit === value ? 'is-on' : ''}"
                onclick="setSetting('deposit', ${value})">${sum(value)}</button>`).join('')}
      <button class="chip" onclick="openDepositSheet()">✏️ своя</button>
    </div>
    <div class="chips" style="margin-bottom:14px">
      ${[0.5, 1, 2].map((value) => `
        <button class="chip wide ${s.risk_per_trade === value ? 'is-on' : ''}"
                onclick="setSetting('risk_per_trade', ${value})">риск ${value}%</button>`).join('')}
    </div>

    <div class="section-title">Ночью</div>
    <button class="switch" onclick="setSetting('quiet_night', ${!s.quiet_night})">
      <span>Не беспокоить с 23:00 до 7:00</span>
      <span class="switch-state">${s.quiet_night ? 'включено' : 'выключено'}</span>
    </button>
    <div class="note">Пропущенное не теряется — утром оно будет во вкладке «Сигналы».</div>
    <div class="note" style="margin-top:18px">Версия ${escape(data.version)}</div>`;
  return html;
}

/* ------------------------------------------------------------------ */
/* Действия                                                            */
/* ------------------------------------------------------------------ */

function haptic(kind) {
  if (tg && tg.HapticFeedback) {
    try { tg.HapticFeedback.impactOccurred(kind || 'light'); } catch (e) { /* не везде есть */ }
  }
}

function toast(text) {
  const node = document.getElementById('toast');
  node.textContent = text;
  node.hidden = false;
  clearTimeout(node._timer);
  node._timer = setTimeout(() => { node.hidden = true; }, 2600);
}

async function act(work, okText) {
  if (state.busy) return;
  state.busy = true;
  try {
    await work();
    haptic('medium');
    if (okText) toast(okText);
    await load();
  } catch (error) {
    toast(error.message);
  } finally {
    state.busy = false;
  }
}

function setSetting(key, value) {
  const body = {}; body[key] = value;
  act(() => api('/api/settings', { method: 'POST', body: JSON.stringify(body) }));
}

function choosePreset(key) {
  act(
    () => api('/api/settings', { method: 'POST', body: JSON.stringify({ preset: key }) }),
    'Режим изменён',
  );
}

function dropAlert(id) {
  const remove = () => act(
    () => api(`/api/alerts/${id}`, { method: 'DELETE' }),
    'Уведомление убрано',
  );
  if (tg && tg.showConfirm) {
    tg.showConfirm('Убрать это уведомление?', (ok) => { if (ok) remove(); });
  } else {
    remove();
  }
}

async function toggleSymbol(symbol) {
  const chosen = state.data.settings.symbols.map((item) => item.symbol);
  const next = chosen.includes(symbol)
    ? chosen.filter((item) => item !== symbol)
    : chosen.concat([symbol]);
  await act(
    () => api('/api/settings', { method: 'POST', body: JSON.stringify({ symbols: next }) }),
  );
  state.instruments = null;
  openInstruments();
}

/* ------------------------------------------------------------------ */
/* Шторки                                                              */
/* ------------------------------------------------------------------ */

function openSheet(html) {
  document.getElementById('sheet-body').innerHTML = html;
  document.getElementById('sheet').hidden = false;
}

function closeSheet() {
  document.getElementById('sheet').hidden = true;
}

async function openInstruments() {
  openSheet('<div class="loader"><div class="spinner"></div><p>Смотрю, что доступно…</p></div>');
  try {
    if (!state.instruments) state.instruments = await api('/api/instruments');
  } catch (error) {
    openSheet(`<div class="sheet-title">Не получилось</div>
      <div class="sheet-sub">${escape(error.message)}</div>
      <button class="button secondary" onclick="closeSheet()">Закрыть</button>`);
    return;
  }

  const items = state.instruments.items.map((item) => `
    <button class="pick ${item.chosen ? 'is-on' : ''}"
            onclick="toggleSymbol('${item.symbol}')">
      <span class="pick-mark">${item.chosen ? '✅' : '▫️'}</span>
      <span class="pick-main">
        <span>${escape(item.title)}</span>
        <span class="pick-note">${escape(item.note)}</span>
      </span>
    </button>`).join('');

  openSheet(`
    <div class="sheet-title">За чем следить</div>
    <div class="sheet-sub">Нажмите, чтобы включить или выключить.
      Начните с одного-двух: так проще понять, как всё работает.</div>
    ${items}
    <button class="button secondary" onclick="closeSheet()">Готово</button>`);
}

function openAlertSheet() {
  const quick = (state.instruments && state.instruments.quick)
    || state.data.settings.symbols.map((item) => ({ symbol: item.symbol, title: item.title }));

  openSheet(`
    <div class="sheet-title">Сообщить, когда цена дойдёт</div>
    <div class="sheet-sub">Выберите инструмент и скажите, чего ждёте.</div>
    <div class="chips" id="alert-symbols" style="margin-bottom:18px">
      ${quick.slice(0, 10).map((item, index) => `
        <button class="chip ${index === 0 ? 'is-on' : ''}"
                data-symbol="${item.symbol}" onclick="pickAlertSymbol(this)">
          ${escape(item.title)}
        </button>`).join('')}
    </div>
    <div class="chips" style="margin-bottom:14px">
      ${[-5, -3, -1, 1, 3, 5].map((value) => `
        <button class="chip" onclick="createAlert('${value > 0 ? '+' : '−'}${Math.abs(value)}%')">
          ${value > 0 ? '+' : '−'}${Math.abs(value)}%
        </button>`).join('')}
    </div>
    <input class="field" id="alert-value" inputmode="decimal"
           placeholder="или напишите цену, например 95000">
    <button class="button" onclick="createAlert()">Поставить</button>
    <button class="button secondary" onclick="closeSheet()">Отмена</button>`);
}

function pickAlertSymbol(button) {
  document.querySelectorAll('#alert-symbols .chip')
    .forEach((chip) => chip.classList.remove('is-on'));
  button.classList.add('is-on');
  haptic();
}

function createAlert(preset) {
  const chosen = document.querySelector('#alert-symbols .chip.is-on');
  if (!chosen) { toast('Выберите инструмент'); return; }

  const field = document.getElementById('alert-value');
  // Минус в кнопках — типографский, а сервер ждёт обычный
  const value = (preset || (field ? field.value : '')).replace('−', '-').trim();
  if (!value) { toast('Напишите цену или выберите процент'); return; }

  act(async () => {
    await api('/api/alerts', {
      method: 'POST',
      body: JSON.stringify({ symbol: chosen.dataset.symbol, value }),
    });
    closeSheet();
    state.tab = 'alerts';
  }, 'Буду следить за ценой');
}

function openDepositSheet() {
  openSheet(`
    <div class="sheet-title">Сколько у вас на счёте</div>
    <div class="sheet-sub">Нужно только для подсказки «сколько брать в сделку».</div>
    <input class="field" id="deposit-value" inputmode="decimal" placeholder="например, 2500">
    <button class="button" onclick="saveDeposit()">Сохранить</button>
    <button class="button secondary" onclick="closeSheet()">Отмена</button>`);
}

function saveDeposit() {
  const raw = (document.getElementById('deposit-value').value || '')
    .replace(/\s/g, '').replace(',', '.');
  const value = parseFloat(raw);
  if (!Number.isFinite(value) || value <= 0) { toast('Напишите сумму числом'); return; }
  act(async () => {
    await api('/api/settings', {
      method: 'POST', body: JSON.stringify({ deposit: value }),
    });
    closeSheet();
  }, 'Сохранил');
}

/* ------------------------------------------------------------------ */
/* Запуск                                                              */
/* ------------------------------------------------------------------ */

document.getElementById('tabs').addEventListener('click', (event) => {
  const tab = event.target.closest('.tab');
  if (!tab) return;
  state.tab = tab.dataset.tab;
  haptic();
  render();
});

document.getElementById('sheet').addEventListener('click', (event) => {
  if (event.target.hasAttribute('data-close')) closeSheet();
});

if (tg) {
  tg.ready();
  tg.expand();
  if (tg.setHeaderColor) {
    try { tg.setHeaderColor('bg_color'); } catch (e) { /* старые клиенты */ }
  }
}

load();
// Пока приложение открыто, цены и «проверил N назад» не должны застывать
setInterval(() => { if (!state.busy && !document.hidden) load(); }, 45000);

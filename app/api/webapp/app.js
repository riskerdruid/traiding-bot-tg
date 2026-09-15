/* =========================================================================
   Mini App торговых сигналов.

   Всё управление ботом живёт здесь: заказчику не нужны ни сервер, ни .env.
   Экран настроек строится автоматически из описания параметров, которое
   отдаёт /api/config — добавленный на бэкенде параметр появляется тут сам.
   ========================================================================= */

(function () {
  'use strict';

  const tg = window.Telegram && window.Telegram.WebApp ? window.Telegram.WebApp : null;
  const LWC = window.LightweightCharts || null;

  /* ------------------------------------------------------------ Состояние */

  const state = {
    screen: 'overview',
    overview: null,
    signals: { items: [], filter: 'all' },
    stats: { data: null, period: 'all' },
    settings: { fields: [], groups: [], readonly: {}, showAdvanced: false },
    news: null,
    chart: null,
    chartSeries: null,
    chartSymbol: null,
    autoTimer: null,
    pending: {},
    symbolBroker: 'ex',
    brokers: [],
    help: null,
    helpOpen: null,
    prevScreen: 'overview',
  };

  const SCREEN_TITLES = {
    overview: 'Обзор',
    signals: 'Сигналы',
    stats: 'Статистика',
    settings: 'Настройки',
    help: 'Помощь',
  };

  const STATUS_VIEW_BINARY = {
    ACTIVE:  { label: 'Ждём экспирации', cls: 'pending' },
    TP_HIT:  { label: 'В плюс',  cls: 'win' },
    SL_HIT:  { label: 'В минус', cls: 'loss' },
    EXPIRED: { label: 'Возврат', cls: '' },
  };

  const STATUS_VIEW = {
    ACTIVE:    { label: 'В работе', cls: 'pending' },
    TP_HIT:    { label: 'Цель',     cls: 'win' },
    SL_HIT:    { label: 'Стоп',     cls: 'loss' },
    EXPIRED:   { label: 'Истёк',    cls: '' },
    CANCELLED: { label: 'Отменён',  cls: '' },
  };

  /* ------------------------------------------------------ Инициализация */

  function initTelegram() {
    if (!tg) return;
    tg.ready();
    tg.expand();
    if (tg.setHeaderColor) {
      try { tg.setHeaderColor('secondary_bg_color'); } catch (e) { /* старый клиент */ }
    }
  }

  function haptic(type) {
    if (!tg || !tg.HapticFeedback) return;
    try {
      if (type === 'select') tg.HapticFeedback.selectionChanged();
      else if (type === 'success') tg.HapticFeedback.notificationOccurred('success');
      else if (type === 'error') tg.HapticFeedback.notificationOccurred('error');
      else tg.HapticFeedback.impactOccurred(type || 'light');
    } catch (e) { /* не критично */ }
  }

  /* ------------------------------------------------------------- Запросы */

  async function api(path, options) {
    const opts = options || {};
    const headers = Object.assign({ 'Content-Type': 'application/json' }, opts.headers || {});
    if (tg && tg.initData) headers['X-Telegram-Init-Data'] = tg.initData;

    const response = await fetch(path, Object.assign({}, opts, { headers }));
    if (response.status === 401 || response.status === 403) {
      throw new Error('Нет доступа. Откройте приложение через своего бота в Telegram.');
    }
    if (!response.ok) {
      let detail = 'Ошибка ' + response.status;
      try {
        const body = await response.json();
        if (body && body.detail) detail = body.detail;
      } catch (e) { /* тело не json */ }
      throw new Error(detail);
    }
    return response.json();
  }

  /* ------------------------------------------------------- Форматирование */

  function money(value) {
    if (value === null || value === undefined) return '—';
    const abs = Math.abs(value);
    const decimals = abs >= 100 ? 2 : (abs >= 1 ? 4 : 6);
    return value.toLocaleString('ru-RU', {
      minimumFractionDigits: decimals, maximumFractionDigits: decimals,
    });
  }

  function pct(value, withSign) {
    if (value === null || value === undefined) return '—';
    const sign = withSign === false ? '' : (value > 0 ? '+' : '');
    return sign + value.toFixed(2) + '%';
  }

  function signClass(value) {
    if (value === null || value === undefined || value === 0) return 'dim';
    return value > 0 ? 'pos' : 'neg';
  }

  function timeOf(ts) {
    if (!ts) return '—';
    return new Date(ts * 1000).toLocaleTimeString('ru-RU', { hour: '2-digit', minute: '2-digit' });
  }

  function dateOf(ts) {
    if (!ts) return '—';
    return new Date(ts * 1000).toLocaleString('ru-RU', {
      day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit',
    });
  }

  function ago(ts) {
    if (!ts) return 'никогда';
    const seconds = Math.floor(Date.now() / 1000) - ts;
    if (seconds < 60) return 'только что';
    if (seconds < 3600) return Math.floor(seconds / 60) + ' мин назад';
    if (seconds < 86400) return Math.floor(seconds / 3600) + ' ч назад';
    return Math.floor(seconds / 86400) + ' дн назад';
  }

  function shortSymbol(symbol) {
    // Сначала снимаем префикс площадки, потом хвост расчётной валюты:
    //   po:EURUSD_otc -> EURUSD OTC,  XAU/USDT:USDT -> XAU/USDT
    const raw = String(symbol || '');
    if (raw.startsWith('po:')) {
      return raw.slice(3).replace('_otc', ' OTC').replace(/_/g, ' ');
    }
    if (raw.startsWith('ex:')) return raw.slice(3).split(':')[0];
    return raw.split(':')[0];
  }

  function escapeHtml(text) {
    return String(text == null ? '' : text).replace(/[&<>"']/g, function (ch) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[ch];
    });
  }

  function confidenceColor(value) {
    if (value >= 80) return 'var(--green)';
    if (value >= 65) return 'var(--accent)';
    return 'var(--amber)';
  }

  function cssVar(name, fallback) {
    const v = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
    return v || fallback;
  }

  function toast(message, kind) {
    const el = document.getElementById('toast');
    el.textContent = message;
    el.className = 'toast' + (kind ? ' ' + kind : '');
    el.hidden = false;
    clearTimeout(el._timer);
    el._timer = setTimeout(function () { el.hidden = true; }, 2600);
  }

  /* ---------------------------------------------------------- Графики SVG */

  function donut(percent, size) {
    const s = size || 108;
    const stroke = 11;
    const radius = (s - stroke) / 2;
    const circumference = 2 * Math.PI * radius;
    const filled = Math.max(0, Math.min(100, percent)) / 100 * circumference;
    const color = percent >= 55 ? 'var(--green)' : (percent >= 45 ? 'var(--amber)' : 'var(--red)');
    return '<svg width="' + s + '" height="' + s + '" viewBox="0 0 ' + s + ' ' + s + '" style="flex-shrink:0">'
      + '<circle cx="' + s / 2 + '" cy="' + s / 2 + '" r="' + radius + '" fill="none" '
      + 'stroke="color-mix(in srgb, var(--muted) 18%, transparent)" stroke-width="' + stroke + '"/>'
      + '<circle cx="' + s / 2 + '" cy="' + s / 2 + '" r="' + radius + '" fill="none" '
      + 'stroke="' + color + '" stroke-width="' + stroke + '" stroke-linecap="round" '
      + 'stroke-dasharray="' + filled.toFixed(2) + ' ' + circumference.toFixed(2) + '" '
      + 'transform="rotate(-90 ' + s / 2 + ' ' + s / 2 + ')"/>'
      + '<text x="50%" y="50%" text-anchor="middle" dy=".36em" '
      + 'style="font-size:23px;font-weight:700;fill:var(--text)">' + Math.round(percent) + '%</text></svg>';
  }

  function equityChart(points) {
    if (!points || points.length < 2) return '';
    const w = 320, h = 120, padding = 6;
    const values = points.map(function (p) { return p.cum_r; });
    const min = Math.min(0, Math.min.apply(null, values));
    const max = Math.max(0, Math.max.apply(null, values));
    const span = (max - min) || 1;
    const step = (w - padding * 2) / (points.length - 1);
    function yOf(v) { return h - padding - ((v - min) / span) * (h - padding * 2); }
    const coords = points.map(function (p, i) {
      return (padding + i * step).toFixed(1) + ',' + yOf(p.cum_r).toFixed(1);
    });
    const last = values[values.length - 1];
    const color = last >= 0 ? 'var(--green)' : 'var(--red)';
    const zeroY = yOf(0).toFixed(1);
    const area = 'M' + padding + ',' + zeroY + ' L' + coords.join(' L')
      + ' L' + (padding + (points.length - 1) * step).toFixed(1) + ',' + zeroY + ' Z';
    return '<div class="chart-wrap"><svg viewBox="0 0 ' + w + ' ' + h + '" preserveAspectRatio="none" style="height:120px">'
      + '<defs><linearGradient id="eqGrad" x1="0" y1="0" x2="0" y2="1">'
      + '<stop offset="0%" stop-color="' + color + '" stop-opacity=".26"/>'
      + '<stop offset="100%" stop-color="' + color + '" stop-opacity="0"/></linearGradient></defs>'
      + '<line x1="' + padding + '" y1="' + zeroY + '" x2="' + (w - padding) + '" y2="' + zeroY + '" '
      + 'stroke="color-mix(in srgb, var(--muted) 30%, transparent)" stroke-width="1" stroke-dasharray="3 3"/>'
      + '<path d="' + area + '" fill="url(#eqGrad)"/>'
      + '<polyline fill="none" stroke="' + color + '" stroke-width="2" '
      + 'stroke-linejoin="round" stroke-linecap="round" points="' + coords.join(' ') + '"/>'
      + '</svg></div>';
  }

  function hoursChart(byHour) {
    const active = byHour.filter(function (h) { return h.total > 0; });
    if (!active.length) return '';
    const maxTotal = Math.max.apply(null, byHour.map(function (h) { return h.total; })) || 1;
    const columns = byHour.map(function (h) {
      const height = h.total ? Math.max(8, (h.total / maxTotal) * 100) : 2;
      let color = 'color-mix(in srgb, var(--muted) 22%, transparent)';
      if (h.total > 0) {
        color = h.winrate >= 55 ? 'var(--green)' : (h.winrate >= 45 ? 'var(--amber)' : 'var(--red)');
      }
      const title = h.total
        ? h.hour + ':00 — угадал ' + h.wins + ' из ' + h.total + ' (' + h.winrate + '%)'
        : h.hour + ':00 — сигналов не было';
      return '<div class="hour-col" title="' + escapeHtml(title) + '">'
        + '<div class="hour-bar" style="height:' + height + '%;background:' + color + '"></div></div>';
    }).join('');
    return '<div class="hours">' + columns + '</div>'
      + '<div class="hours-axis"><span>00</span><span>06</span><span>12</span><span>18</span><span>23</span></div>'
      + '<div class="note">Высота столбика — сколько сигналов было в этот час. '
      + 'Зелёный — чаще угадывал, красный — чаще ошибался. Время всемирное (UTC).'
      + '</div>';
  }

  /* ------------------------------------------------- Свечной график (LWC) */

  function destroyChart() {
    if (state.chart) {
      try { state.chart.remove(); } catch (e) { /* уже удалён */ }
      state.chart = null;
      state.chartSeries = null;
    }
  }

  async function drawCandles(symbol, activeSignals) {
    const holder = document.getElementById('candles');
    if (!holder) return;
    if (!LWC || !LWC.createChart) {
      holder.innerHTML = '<div class="note">График недоступен: библиотека не загрузилась.</div>';
      return;
    }

    let payload;
    try {
      payload = await api('/api/candles?limit=150'
        + (symbol ? '&symbol=' + encodeURIComponent(symbol) : ''));
    } catch (err) {
      holder.innerHTML = '<div class="note">Не удалось загрузить график: '
        + escapeHtml(err.message) + '</div>';
      return;
    }
    if (!payload.candles || !payload.candles.length) {
      holder.innerHTML = '<div class="note">Нет данных для графика.</div>';
      return;
    }

    destroyChart();
    holder.innerHTML = '';
    state.chartSymbol = payload.symbol;

    const text = cssVar('--text', '#fff');
    const muted = cssVar('--muted', '#8b93a1');
    const chart = LWC.createChart(holder, {
      height: 240,
      layout: {
        background: { color: 'transparent' },
        textColor: muted,
        fontSize: 11,
      },
      grid: {
        vertLines: { color: 'rgba(128,128,128,.10)' },
        horzLines: { color: 'rgba(128,128,128,.10)' },
      },
      rightPriceScale: { borderVisible: false },
      timeScale: { borderVisible: false, timeVisible: true, secondsVisible: false },
      crosshair: { mode: 0 },
      handleScale: { axisPressedMouseMove: false },
    });

    const series = chart.addCandlestickSeries({
      upColor: cssVar('--green', '#22c55e'),
      downColor: cssVar('--red', '#ef4444'),
      borderVisible: false,
      wickUpColor: cssVar('--green', '#22c55e'),
      wickDownColor: cssVar('--red', '#ef4444'),
    });

    series.setData(payload.candles.map(function (c) {
      return { time: Math.floor(c.ts / 1000), open: c.o, high: c.h, low: c.l, close: c.c };
    }));

    // Уровни активного сигнала прямо на графике — вход, стоп и цель
    const mine = (activeSignals || []).filter(function (s) { return s.symbol === payload.symbol; });
    mine.slice(0, 1).forEach(function (s) {
      [
        { price: s.entry, color: cssVar('--accent', '#3b82f6'), title: 'вход' },
        { price: s.stop_loss, color: cssVar('--red', '#ef4444'), title: 'стоп' },
        { price: s.take_profit, color: cssVar('--green', '#22c55e'), title: 'цель' },
      ].forEach(function (line) {
        series.createPriceLine({
          price: line.price,
          color: line.color,
          lineWidth: 1,
          lineStyle: 2,
          axisLabelVisible: true,
          title: line.title,
        });
      });
    });

    chart.timeScale().fitContent();
    state.chart = chart;
    state.chartSeries = series;

    const label = document.getElementById('chart-label');
    if (label) {
      label.textContent = shortSymbol(payload.symbol) + ' · ' + payload.timeframe;
    }
  }

  /* --------------------------------------------------------- Компоненты */

  function emptyState(icon, title, text) {
    return '<div class="empty"><div class="empty-icon">' + icon + '</div>'
      + '<div class="empty-title">' + escapeHtml(title) + '</div>'
      + '<div class="empty-text">' + escapeHtml(text) + '</div></div>';
  }

  function binaryCard(signal, detailed) {
    const isLong = signal.side === 'LONG';
    const view = (STATUS_VIEW_BINARY[signal.status] || STATUS_VIEW[signal.status]
                  || { label: signal.status, cls: '' });
    const arrow = isLong ? '▲' : '▼';
    const side = isLong ? 'ВВЕРХ' : 'ВНИЗ';

    let chip = view.label;
    if (signal.status !== 'ACTIVE' && signal.pnl_pct !== null && signal.pnl_pct !== undefined) {
      chip = view.label + ' ' + pct(signal.pnl_pct);
    }

    let html = '<article class="signal binary ' + (isLong ? 'long' : 'short') + '">'
      + '<div class="signal-head"><div>'
      + '<div class="signal-side">' + arrow + ' ' + side + '</div>'
      + '<div class="signal-symbol">' + escapeHtml(shortSymbol(signal.symbol))
      + ' · ' + escapeHtml(signal.timeframe) + ' · #' + signal.id + '</div>'
      + '</div><span class="chip ' + view.cls + '">' + escapeHtml(chip) + '</span></div>';

    html += '<div class="levels binary-levels">'
      + '<div class="level"><div class="level-label">Вход</div>'
      + '<div class="level-value">' + money(signal.entry) + '</div></div>'
      + '<div class="level"><div class="level-label">Срок</div>'
      + '<div class="level-value">' + Math.round(signal.expiry_minutes || 0) + ' мин</div></div>'
      + '<div class="level tp"><div class="level-label">Выплата</div>'
      + '<div class="level-value">' + (signal.payout != null ? Math.round(signal.payout) + '%' : '—')
      + '</div></div></div>';

    // Обратный отсчёт до экспирации
    if (signal.status === 'ACTIVE' && signal.expiry_at) {
      const left = signal.expiry_at - Math.floor(Date.now() / 1000);
      const total = Math.max(1, signal.expiry_at - signal.created_at);
      const doneRatio = Math.max(0, Math.min(100, (1 - left / total) * 100));
      html += '<div class="progress-track"><div class="progress-fill up" '
        + 'style="left:0;width:' + doneRatio.toFixed(1) + '%"></div></div>'
        + '<div class="progress-legend"><span>вход</span>'
        + '<span class="' + (left > 0 ? 'dim' : 'neg') + '">'
        + (left > 0 ? 'осталось ' + fmtLeft(left) : 'ждём результат') + '</span>'
        + '<span>экспирация</span></div>';
    }

    html += '<div class="confidence">'
      + '<span class="dim" style="font-size:11px">Уверенность</span>'
      + '<div class="confidence-track"><div class="confidence-fill" style="width:'
      + signal.confidence + '%;background:' + confidenceColor(signal.confidence) + '"></div></div>'
      + '<span class="confidence-value">' + signal.confidence + '%</span></div>';

    if (signal.payout != null) {
      const breakeven = 100 / (1 + signal.payout / 100);
      html += '<div class="note" style="margin-top:10px">Чтобы выйти в ноль при выплате '
        + Math.round(signal.payout) + '%, нужно угадывать <b>'
        + breakeven.toFixed(0) + '%</b> сделок.</div>';
    }

    if (detailed && signal.reasons && signal.reasons.length) {
      html += '<ul class="reasons">';
      signal.reasons.forEach(function (r) { html += '<li>' + escapeHtml(r) + '</li>'; });
      html += '</ul>';
    }

    const rText = (signal.r_multiple !== null && signal.r_multiple !== undefined)
      ? '<span class="' + signClass(signal.r_multiple) + '">'
        + (signal.r_multiple > 0 ? '+' : '') + signal.r_multiple.toFixed(2) + 'R</span>' : '';
    html += '<div class="signal-foot"><span>' + dateOf(signal.created_at) + '</span>' + rText + '</div>';
    return html + '</article>';
  }

  function fmtLeft(seconds) {
    if (seconds >= 60) return Math.floor(seconds / 60) + ' мин ' + (seconds % 60) + ' с';
    return seconds + ' с';
  }

  function signalCard(signal, detailed) {
    if (signal.kind === 'binary') return binaryCard(signal, detailed);
    const isLong = signal.side === 'LONG';
    const view = STATUS_VIEW[signal.status] || { label: signal.status, cls: '' };
    const arrow = isLong ? '▲' : '▼';

    let chipText = view.label;
    if (signal.status !== 'ACTIVE' && signal.pnl_pct !== null && signal.pnl_pct !== undefined) {
      chipText = view.label + ' ' + pct(signal.pnl_pct);
    } else if (signal.status === 'ACTIVE' && signal.unrealized_pct !== null
               && signal.unrealized_pct !== undefined) {
      chipText = pct(signal.unrealized_pct);
    }

    let html = '<article class="signal ' + (isLong ? 'long' : 'short') + '">'
      + '<div class="signal-head"><div>'
      + '<div class="signal-side">' + arrow + ' ' + (isLong ? 'ПОКУПКА' : 'ПРОДАЖА') + '</div>'
      + '<div class="signal-symbol">' + escapeHtml(shortSymbol(signal.symbol))
      + ' · ' + escapeHtml(signal.timeframe) + ' · #' + signal.id + '</div>'
      + '</div><span class="chip ' + view.cls + '">' + escapeHtml(chipText) + '</span></div>';

    html += '<div class="levels">'
      + '<div class="level sl"><div class="level-label">Стоп</div>'
      + '<div class="level-value">' + money(signal.stop_loss) + '</div></div>'
      + '<div class="level"><div class="level-label">Вход</div>'
      + '<div class="level-value">' + money(signal.entry) + '</div></div>'
      + '<div class="level tp"><div class="level-label">Цель</div>'
      + '<div class="level-value">' + money(signal.take_profit) + '</div></div></div>';

    if (signal.status === 'ACTIVE' && signal.current_price) {
      const progress = Math.max(-100, Math.min(100, signal.progress || 0));
      const up = progress >= 0;
      html += '<div class="progress-track">'
        + '<div class="progress-fill ' + (up ? 'up' : 'down') + '" style="width:'
        + (Math.abs(progress) / 2) + '%"></div><div class="progress-mid"></div></div>'
        + '<div class="progress-legend"><span>стоп</span>'
        + '<span class="' + signClass(signal.unrealized_pct) + '">'
        + money(signal.current_price) + ' · ' + pct(signal.unrealized_pct) + '</span>'
        + '<span>цель</span></div>';
    }

    html += '<div class="confidence">'
      + '<span class="dim" style="font-size:11px">Уверенность</span>'
      + '<div class="confidence-track"><div class="confidence-fill" style="width:'
      + signal.confidence + '%;background:' + confidenceColor(signal.confidence) + '"></div></div>'
      + '<span class="confidence-value">' + signal.confidence + '%</span></div>';

    if (detailed && signal.reasons && signal.reasons.length) {
      html += '<ul class="reasons">';
      signal.reasons.forEach(function (r) { html += '<li>' + escapeHtml(r) + '</li>'; });
      html += '</ul>';
    }

    if (detailed && signal.sizing) {
      html += '<div class="sizing"><span class="dim">Объём по вашему риску</span>'
        + '<b>' + escapeHtml(signal.sizing.units) + '</b></div>';
    }

    if (detailed && signal.indicators) {
      const ind = signal.indicators;
      const chips = [];
      if (ind.rsi != null) chips.push('RSI ' + ind.rsi);
      if (ind.adx != null) chips.push('ADX ' + ind.adx);
      if (ind.atr != null) chips.push('ATR ' + money(ind.atr));
      if (ind.stoch_k != null) chips.push('Stoch ' + Math.round(ind.stoch_k));
      if (ind.htf_bias) {
        const t = { BULL: 'тренд вверх', BEAR: 'тренд вниз', NEUTRAL: 'без тренда' };
        chips.push(t[ind.htf_bias] || ind.htf_bias);
      }
      if (chips.length) {
        html += '<div class="indicator-chips">';
        chips.forEach(function (c) { html += '<span class="chip">' + escapeHtml(c) + '</span>'; });
        html += '</div>';
      }
    }

    const rText = (signal.r_multiple !== null && signal.r_multiple !== undefined)
      ? '<span class="' + signClass(signal.r_multiple) + '">'
        + (signal.r_multiple > 0 ? '+' : '') + signal.r_multiple.toFixed(2) + 'R</span>' : '';
    html += '<div class="signal-foot"><span>' + dateOf(signal.created_at) + '</span>' + rText + '</div>';
    return html + '</article>';
  }

  /* ------------------------------------------------------------- Экраны */

  function renderOverview(data) {
    let html = '<div class="screen">';

    const muted = data.scanner && data.scanner.muted_by_news;
    if (muted) {
      html += '<div class="banner"><div class="banner-icon">🔇</div><div>'
        + '<div class="banner-title">Пауза из-за новости</div>'
        + '<div class="banner-text">' + escapeHtml(muted.title) + ' (' + escapeHtml(muted.currency)
        + ') — через ' + Math.round(muted.minutes_until) + ' мин. '
        + 'Сигналы возобновятся после выхода данных.</div></div></div>';
    }
    if (!data.scanner || !data.scanner.running) {
      html += '<div class="banner"><div class="banner-icon">⚠️</div><div>'
        + '<div class="banner-title">Сканер не запущен</div>'
        + '<div class="banner-text">Бот не анализирует рынок.</div></div></div>';
    }

    const symbols = Object.keys(data.prices || {});
    if (symbols.length) {
      html += '<div class="section-title">Рынок</div>';
      symbols.forEach(function (symbol) {
        html += '<div class="card price-card" data-sym="' + escapeHtml(symbol) + '"><div>'
          + '<div class="price-symbol">' + escapeHtml(shortSymbol(symbol)) + '</div>'
          + '<div class="price-value">' + money(data.prices[symbol]) + '</div></div></div>';
      });
      html += '<div class="card" style="margin-top:12px">'
        + '<div class="chart-head"><b id="chart-label">График</b>'
        + '<span class="dim" style="font-size:11px">свечи биржи</span></div>'
        + '<div id="candles" class="candles"></div></div>';
    }

    html += '<div class="section-title">Активные сигналы'
      + (data.active.length ? ' · ' + data.active.length : '') + '</div>';
    if (!data.active.length) {
      html += '<div class="card">' + emptyState('🎯', 'Пока тихо',
        'Бот следит за рынком и пришлёт сигнал, как только появится подходящая точка входа.')
        + '</div>';
    } else {
      data.active.forEach(function (s) { html += signalCard(s, true); });
    }

    const today = data.stats_today, all = data.stats_all;
    html += '<div class="section-title">Сегодня</div><div class="grid-3">'
      + '<div class="metric"><div class="metric-value">' + (today.decided + today.active) + '</div>'
      + '<div class="metric-label">сигналов</div></div>'
      + '<div class="metric"><div class="metric-value ' + (today.decided ? signClass(today.winrate - 50) : 'dim') + '">'
      + (today.decided ? today.winrate + '%' : '—') + '</div><div class="metric-label">точность</div></div>'
      + '<div class="metric"><div class="metric-value ' + signClass(today.total_r) + '">'
      + (today.total_r > 0 ? '+' : '') + today.total_r.toFixed(1) + 'R</div>'
      + '<div class="metric-label">результат</div></div></div>';

    if (all.decided > 0) {
      html += '<div class="note">За всё время: <b>' + all.decided + '</b> завершённых, '
        + 'точность <b>' + all.winrate + '%</b>, итог <b>'
        + (all.total_r > 0 ? '+' : '') + all.total_r.toFixed(2) + 'R</b>.'
        + (all.decided < 20 ? ' Выборка пока мала для выводов.' : '') + '</div>';
    }

    const scanner = data.scanner || {};
    const hours = Math.floor(data.uptime_sec / 3600);
    const minutes = Math.floor((data.uptime_sec % 3600) / 60);
    html += '<div class="section-title">Бот</div><div class="card">'
      + '<div class="switch-row"><span class="switch-label">Последняя проверка</span>'
      + '<span class="dim nums">' + ago(scanner.last_scan_at) + '</span></div>'
      + '<div class="switch-row"><span class="switch-label">Проверок выполнено</span>'
      + '<span class="dim nums">' + (scanner.scans_done || 0) + '</span></div>'
      + '<div class="switch-row"><span class="switch-label">Работает без сбоев</span>'
      + '<span class="dim nums">' + (hours ? hours + ' ч ' : '') + minutes + ' мин</span></div>'
      + '<div class="switch-row"><span class="switch-label">Таймфрейм</span>'
      + '<span class="dim nums">' + escapeHtml(data.config.timeframe) + ' / '
      + escapeHtml(data.config.htf_timeframe) + '</span></div></div>';

    return html + '</div>';
  }

  function renderSignals(items, filter) {
    let html = '<div class="screen"><div class="filters">';
    [
      { key: 'all', label: 'Все' }, { key: 'ACTIVE', label: 'В работе' },
      { key: 'TP_HIT', label: 'Цель' }, { key: 'SL_HIT', label: 'Стоп' },
      { key: 'EXPIRED', label: 'Истёкшие' },
    ].forEach(function (f) {
      html += '<button class="filter' + (filter === f.key ? ' active' : '')
        + '" data-filter="' + f.key + '">' + f.label + '</button>';
    });
    html += '</div>';
    if (!items.length) {
      html += emptyState('📭', 'Сигналов нет', filter === 'all'
        ? 'Как только бот найдёт точку входа, она появится здесь.'
        : 'По этому фильтру пока ничего нет.');
    } else {
      items.forEach(function (s) { html += signalCard(s, true); });
    }
    return html + '</div>';
  }

  function renderStats(payload, period) {
    const s = payload.summary;
    let html = '<div class="screen"><div class="filters">';
    [
      { key: 'day', label: 'Сегодня' }, { key: 'week', label: 'Неделя' },
      { key: 'month', label: 'Месяц' }, { key: 'all', label: 'Всё время' },
    ].forEach(function (p) {
      html += '<button class="filter' + (period === p.key ? ' active' : '')
        + '" data-period="' + p.key + '">' + p.label + '</button>';
    });
    html += '</div>';

    if (!s.decided && !s.active) {
      return html + emptyState('📊', 'Статистики пока нет',
        'Она появится, когда первые сигналы дойдут до цели или стопа.') + '</div>';
    }

    html += '<div class="card"><div class="donut-wrap">' + donut(s.winrate)
      + '<div class="donut-legend">'
      + '<div class="legend-row"><span class="legend-dot" style="background:var(--green)"></span>'
      + '<span class="legend-label">Угадал</span><span class="legend-value">' + s.wins + '</span></div>'
      + '<div class="legend-row"><span class="legend-dot" style="background:var(--red)"></span>'
      + '<span class="legend-label">Не угадал</span><span class="legend-value">' + s.losses + '</span></div>'
      + '<div class="legend-row"><span class="legend-dot" style="background:var(--muted)"></span>'
      + '<span class="legend-label">Истекли</span><span class="legend-value">' + s.expired + '</span></div>'
      + '<div class="legend-row"><span class="legend-dot" style="background:var(--amber)"></span>'
      + '<span class="legend-label">В работе</span><span class="legend-value">' + s.active + '</span></div>'
      + '</div></div>';
    if (s.decided < 20) {
      html += '<div class="note">⚠️ Всего ' + s.decided + ' завершённых сигналов. '
        + 'Это мало: при таком количестве результат почти целиком зависит '
        + 'от везения. Судить можно с 30–50.</div>';
    }
    html += '</div>';

    html += '<div class="grid-3" style="margin-top:12px">'
      + '<div class="metric"><div class="metric-value ' + signClass(s.total_r) + '">'
      + (s.total_r > 0 ? '+' : '') + s.total_r.toFixed(1) + 'R</div>'
      + '<div class="metric-label">итог в размерах риска</div></div>'
      + '<div class="metric"><div class="metric-value ' + signClass(s.avg_r) + '">'
      + (s.avg_r > 0 ? '+' : '') + s.avg_r.toFixed(2) + 'R</div>'
      + '<div class="metric-label">в среднем за сигнал</div></div>'
      + '<div class="metric"><div class="metric-value">' + s.profit_factor + '</div>'
      + '<div class="metric-label">прибыль / убыток</div></div></div>';

    if (payload.equity && payload.equity.length > 1) {
      html += '<div class="section-title">Как рос счёт</div><div class="card">'
        + equityChart(payload.equity)
        + '<div class="note">Каждая точка — завершённый сигнал. Линия вверх — '
        + 'счёт растёт. Единица по вертикали равна тому, чем вы рискуете '
        + 'в одной сделке: +2 значит заработали вдвое больше, чем рисковали.'
        + '</div></div>';
    }
    const hrs = hoursChart(payload.by_hour || []);
    if (hrs) html += '<div class="section-title">В какие часы точнее</div><div class="card">' + hrs + '</div>';

    if (payload.by_symbol && payload.by_symbol.length) {
      html += '<div class="section-title">По инструментам</div><div class="card">'
        + '<table class="table"><thead><tr><th>Инструмент</th><th>Сигналов</th><th>Точность</th></tr></thead><tbody>';
      payload.by_symbol.forEach(function (row) {
        const cls = row.winrate >= 55 ? 'pos' : (row.winrate >= 45 ? '' : 'neg');
        html += '<tr><td>' + escapeHtml(shortSymbol(row.symbol)) + '</td><td>'
          + (row.wins + row.losses) + '</td><td class="' + cls + '">' + row.winrate + '%</td></tr>';
      });
      html += '</tbody></table></div>';
    }

    html += '<div class="section-title">Подробности</div><div class="card">'
      + '<div class="switch-row"><span class="switch-label">Средняя прибыль</span>'
      + '<span class="pos nums">' + pct(s.avg_win_pct) + '</span></div>'
      + '<div class="switch-row"><span class="switch-label">Средний убыток</span>'
      + '<span class="neg nums">' + pct(s.avg_loss_pct) + '</span></div>'
      + '<div class="switch-row"><span class="switch-label">Лучший сигнал</span>'
      + '<span class="pos nums">' + pct(s.best_pct) + '</span></div>'
      + '<div class="switch-row"><span class="switch-label">Худший сигнал</span>'
      + '<span class="neg nums">' + pct(s.worst_pct) + '</span></div>'
      + '<div class="switch-row"><span class="switch-label">Удач подряд, максимум</span>'
      + '<span class="nums">' + s.max_win_streak + ' подряд</span></div>'
      + '<div class="switch-row"><span class="switch-label">Неудач подряд, максимум</span>'
      + '<span class="nums">' + s.max_loss_streak + ' подряд</span></div></div>';

    return html + '</div>';
  }

  /* --------------------------------------------- Экран настроек */

  function controlFor(f) {
    const id = 'f-' + f.key;
    const value = f.value;

    if (f.kind === 'bool') {
      return '<button class="switch' + (value ? ' on' : '') + '" data-key="' + f.key + '"></button>';
    }
    if (f.kind === 'secret') {
      // Значение наружу не отдаётся — показываем только признак «задан»
      // и короткий хвост, чтобы можно было опознать нужный токен.
      return '<div class="secret-box">'
        + '<div class="secret-state' + (f.is_set ? ' filled' : '') + '">'
        + (f.is_set ? '✓ ' : '') + escapeHtml(f.preview || 'не задан') + '</div>'
        + '<input class="text-input secret-input" type="password" data-key="' + f.key
        + '" id="' + id + '" placeholder="' + (f.is_set ? 'заменить…' : 'вставьте токен')
        + '" autocomplete="off" spellcheck="false">'
        + (f.is_set ? '<button class="secret-clear" data-clear="' + f.key + '">Удалить</button>' : '')
        + '</div>';
    }
    if (f.kind === 'choice') {
      let opts = '';
      (f.choices || []).forEach(function (c) {
        opts += '<option value="' + escapeHtml(c) + '"'
          + (String(value) === c ? ' selected' : '') + '>' + escapeHtml(c) + '</option>';
      });
      return '<select class="select" data-key="' + f.key + '" id="' + id + '">' + opts + '</select>';
    }
    if (f.kind === 'int' || f.kind === 'float') {
      const step = f.step || (f.kind === 'int' ? 1 : 0.1);
      return '<div class="stepper">'
        + '<button class="step-btn" data-key="' + f.key + '" data-dir="-1">−</button>'
        + '<input class="step-input" type="text" inputmode="decimal" data-key="' + f.key
        + '" id="' + id + '" value="' + escapeHtml(String(value)) + '" data-step="' + step + '">'
        + '<button class="step-btn" data-key="' + f.key + '" data-dir="1">+</button></div>';
    }
    if (f.kind === 'list') {
      let chips = '';
      (value || []).forEach(function (v) {
        chips += '<span class="sym-chip">' + escapeHtml(shortSymbol(v))
          + '<button data-remove="' + escapeHtml(v) + '">×</button></span>';
      });
      return '<div class="sym-wrap">' + chips
        + '<button class="sym-add" id="open-symbols">+ добавить</button></div>';
    }
    return '<input class="text-input" type="text" data-key="' + f.key
      + '" id="' + id + '" value="' + escapeHtml(String(value == null ? '' : value)) + '">';
  }

  function renderSettings(data, news) {
    const advanced = state.settings.showAdvanced;
    let html = '<div class="screen">';

    html += '<div class="note" style="margin-bottom:14px">Все изменения '
      + 'применяются сразу — перезапускать бота не нужно.</div>';

    data.groups.forEach(function (group) {
      const fields = data.fields.filter(function (f) {
        return f.group === group.key && (advanced || !f.advanced);
      });
      if (!fields.length) return;

      html += '<div class="section-title">' + escapeHtml(group.label) + '</div><div class="card">';
      fields.forEach(function (f) {
        // Списку инструментов и текстовым полям с длинной подсказкой
        // тесно в узкой колонке — разворачиваем строку на всю ширину
        const wide = f.kind === 'list' || (f.kind === 'str' && (f.hint || '').length > 40);
        html += '<div class="setting-row' + (wide ? ' wide' : '') + '"><div class="setting-info">'
          + '<div class="setting-label">' + escapeHtml(f.label)
          + (f.unit && f.kind !== 'bool' ? ' <span class="dim">' + escapeHtml(f.unit) + '</span>' : '')
          + '</div>'
          + (f.hint ? '<div class="setting-hint">' + escapeHtml(f.hint) + '</div>' : '')
          + '</div><div class="setting-control">' + controlFor(f) + '</div></div>';
      });
      html += '</div>';
    });

    html += '<div class="card" style="margin-top:12px">'
      + '<div class="switch-row"><div><div class="switch-label">Показать все параметры</div>'
      + '<div class="switch-hint">Тонкая настройка индикаторов — меняйте, '
      + 'только если понимаете, что делаете</div></div>'
      + '<button class="switch' + (advanced ? ' on' : '') + '" id="toggle-advanced"></button></div></div>';

    // Календарь новостей — рядом с настройкой фильтра, так понятнее
    if (news) {
      html += '<div class="section-title">Ближайшие новости</div><div class="card">';
      if (news.muted_by) {
        html += '<div class="banner" style="margin-bottom:12px"><div class="banner-icon">🔇</div><div>'
          + '<div class="banner-title">Сейчас пауза</div><div class="banner-text">'
          + escapeHtml(news.muted_by.title) + ' — через '
          + Math.round(news.muted_by.minutes_until) + ' мин</div></div></div>';
      }
      if (!news.upcoming.length) {
        html += '<div class="dim" style="font-size:13px">Важных событий не запланировано.</div>';
      } else {
        news.upcoming.slice(0, 6).forEach(function (e) {
          const when = e.minutes_until < 120 ? Math.round(e.minutes_until) + ' мин' : timeOf(e.event_at);
          html += '<div class="news-item"><div class="news-time">' + escapeHtml(when) + '</div>'
            + '<div class="news-body"><div class="news-title">' + escapeHtml(e.title) + '</div>'
            + '<div class="news-meta"><span class="impact ' + String(e.impact).toLowerCase() + '"></span>'
            + escapeHtml(e.currency) + ' · ' + dateOf(e.event_at) + '</div></div></div>';
        });
      }
      html += '</div>';
    }

    if (state.brokers && state.brokers.length) {
      html += '<div class="section-title">Площадки</div><div class="card">';
      state.brokers.forEach(function (b) {
        const ok = b.connected;
        const detail = ok
          ? (b.kind === 'binary'
              ? ((b.demo === true ? 'демо-счёт' : b.demo === false ? 'реальный счёт' : 'подключено')
                 + (b.balance != null ? ' · ' + money(b.balance) : '')
                 + (b.assets ? ' · ' + b.assets + ' активов' : ''))
              : 'подключено · ' + (b.latency_ms || 0) + ' мс')
          : (b.error || 'нет связи');
        html += '<div class="switch-row"><div>'
          + '<div class="switch-label">' + escapeHtml(b.title || b.broker) + '</div>'
          + '<div class="switch-hint">' + escapeHtml(detail) + '</div></div>'
          + '<span class="live-dot ' + (ok ? 'online' : 'offline') + '"></span></div>';
      });
      html += '</div>';
    }

    const ro = data.readonly || {};
    html += '<div class="section-title">Система</div><div class="card">'
      + '<div class="switch-row"><span class="switch-label">Биржа</span>'
      + '<span class="dim">' + escapeHtml(ro.exchange || '—') + '</span></div>'
      + '<div class="switch-row"><span class="switch-label">Часовой пояс</span>'
      + '<span class="dim">' + escapeHtml(ro.timezone || '—') + '</span></div></div>'
      + '<div class="note">Эти два значения задаёт разработчик при установке.</div>';

    return html + '</div>';
  }

  /* ------------------------------------------------------ Экран помощи */

  function renderHelpBlock(block) {
    if (block.type === 'text') return '<p class="help-text">' + block.text + '</p>';
    if (block.type === 'note') return '<div class="help-note">💡 ' + block.text + '</div>';
    if (block.type === 'warn') return '<div class="help-warn">⚠️ ' + block.text + '</div>';
    if (block.type === 'code') {
      return '<pre class="help-code">' + escapeHtml(block.text) + '</pre>';
    }
    if (block.type === 'steps') {
      return '<ol class="help-steps">'
        + block.items.map(function (i) { return '<li>' + i + '</li>'; }).join('')
        + '</ol>';
    }
    if (block.type === 'list') {
      return '<ul class="help-list">'
        + block.items.map(function (i) { return '<li>' + i + '</li>'; }).join('')
        + '</ul>';
    }
    return '';
  }

  function renderHelp(topics) {
    let html = '<div class="screen">';
    html += '<div class="note" style="margin-bottom:14px">'
      + 'Та же справка есть в боте: команда /help. Нажмите на тему, чтобы раскрыть.'
      + '</div>';

    topics.forEach(function (topic) {
      const open = state.helpOpen === topic.id;
      html += '<div class="help-card' + (open ? ' open' : '') + '" data-topic="'
        + escapeHtml(topic.id) + '">'
        + '<button class="help-head">'
        + '<span class="help-icon">' + topic.icon + '</span>'
        + '<span class="help-title"><b>' + escapeHtml(topic.title) + '</b>'
        + '<span class="help-summary">' + escapeHtml(topic.summary) + '</span></span>'
        + '<span class="help-chev">' + (open ? '−' : '+') + '</span>'
        + '</button>';
      if (open) {
        html += '<div class="help-body">'
          + topic.blocks.map(renderHelpBlock).join('') + '</div>';
      }
      html += '</div>';
    });

    return html + '</div>';
  }

  function bindHelp() {
    document.querySelectorAll('[data-topic] .help-head').forEach(function (btn) {
      btn.addEventListener('click', function () {
        const card = btn.closest('[data-topic]');
        const id = card.dataset.topic;
        state.helpOpen = state.helpOpen === id ? null : id;
        haptic('select');
        content.innerHTML = renderHelp(state.help || []);
        bindHelp();
        if (state.helpOpen === id) {
          const target = document.querySelector('[data-topic="' + id + '"]');
          if (target) target.scrollIntoView({ behavior: 'smooth', block: 'start' });
        }
      });
    });
  }

  /* --------------------------------------------- Сохранение настроек */

  async function saveField(key, value) {
    const body = {};
    body[key] = value;
    try {
      const result = await api('/api/config', { method: 'POST', body: JSON.stringify(body) });
      state.settings.fields = result.fields;
      haptic('success');
      toast('Сохранено');
      return true;
    } catch (err) {
      haptic('error');
      toast(err.message, 'error');
      return false;
    }
  }

  function fieldByKey(key) {
    return state.settings.fields.filter(function (f) { return f.key === key; })[0];
  }

  /* --------------------------------------------- Выбор инструментов */

  let symbolSearchTimer = null;

  async function loadSymbols(query) {
    const list = document.getElementById('symbol-list');
    list.innerHTML = '<div class="loader"><div class="spinner"></div></div>';
    try {
      const data = await api('/api/symbols?limit=60&broker=' + state.symbolBroker
        + '&q=' + encodeURIComponent(query || ''));
      if (!data.items.length) {
        list.innerHTML = '<div class="empty"><div class="empty-text">Ничего не найдено</div></div>';
        return;
      }
      // У биржи несколько инструментов могут свернуться в одно имя
      // (своп и срочные фьючерсы на золото — все «XAU/USD»). Там, где
      // метки совпадают, показываем полный символ, иначе выбрать нужный
      // невозможно.
      const labels = {};
      data.items.forEach(function (item) {
        const l = shortSymbol(item.symbol);
        labels[l] = (labels[l] || 0) + 1;
      });

      list.innerHTML = data.items.map(function (item) {
        const label = shortSymbol(item.symbol);
        const ambiguous = labels[label] > 1;
        const tags = [];

        if (item.is_otc) tags.push('<span class="tag otc">OTC</span>');
        if (item.payout != null) {
          const good = item.payout >= 80;
          tags.push('<span class="tag' + (good ? ' pay-good' : '') + '">'
            + Math.round(item.payout) + '%</span>');
        }
        const typeLabel = { swap: 'бессрочный', future: 'срочный', spot: 'спот',
                            option: 'опцион', margin: 'маржа' }[item.asset_type];
        if (typeLabel) tags.push('<span class="tag">' + typeLabel + '</span>');

        return '<button class="sym-row' + (item.selected ? ' selected' : '') + '" '
          + 'data-symbol="' + escapeHtml(item.symbol) + '">'
          + '<span class="sym-name"><b>' + escapeHtml(label) + '</b>'
          + (tags.length ? ' ' + tags.join(' ') : '')
          + (ambiguous ? '<span class="sym-full">' + escapeHtml(item.symbol) + '</span>' : '')
          + '</span>'
          + '<span class="dim" style="font-size:11px">'
          + (item.selected ? 'выбран ✓' : 'добавить') + '</span></button>';
      }).join('');
      list.querySelectorAll('[data-symbol]').forEach(function (btn) {
        btn.addEventListener('click', function () { toggleSymbol(btn.dataset.symbol); });
      });
    } catch (err) {
      list.innerHTML = '<div class="error-box">' + escapeHtml(err.message) + '</div>'
        + (state.symbolBroker === 'po'
          ? '<div class="note">Для опционов нужен SSID Pocket Option — '
            + 'задайте его в настройках выше.</div>' : '');
    }
  }

  function bindBrokerTabs() {
    document.querySelectorAll('[data-sbroker]').forEach(function (btn) {
      btn.addEventListener('click', function () {
        state.symbolBroker = btn.dataset.sbroker;
        document.querySelectorAll('[data-sbroker]').forEach(function (b) {
          b.classList.toggle('active', b === btn);
        });
        haptic('select');
        loadSymbols(document.getElementById('symbol-search').value);
      });
    });
  }

  async function toggleSymbol(symbol) {
    const field = fieldByKey('symbols');
    const current = (field && field.value ? field.value : []).slice();
    const index = current.indexOf(symbol);
    if (index >= 0) current.splice(index, 1);
    else current.push(symbol);

    if (!current.length) {
      toast('Нужен хотя бы один инструмент', 'error');
      haptic('error');
      return;
    }
    haptic('select');
    const ok = await saveField('symbols', current);
    if (ok) {
      await loadSymbols(document.getElementById('symbol-search').value);
      loadScreen('settings', true);
    }
  }

  function openSymbolSheet() {
    document.getElementById('sheet').hidden = false;
    const tabs = document.getElementById('sheet-brokers');
    if (tabs) {
      tabs.innerHTML = [
        { id: 'ex', label: 'Биржа' },
        { id: 'po', label: 'Опционы' },
      ].map(function (b) {
        return '<button class="filter' + (state.symbolBroker === b.id ? ' active' : '')
          + '" data-sbroker="' + b.id + '">' + b.label + '</button>';
      }).join('');
      bindBrokerTabs();
    }
    const search = document.getElementById('symbol-search');
    search.value = '';
    loadSymbols('');
    setTimeout(function () { search.focus(); }, 120);
  }

  function closeSymbolSheet() {
    document.getElementById('sheet').hidden = true;
  }

  /* --------------------------------------------------------- Загрузка */

  const content = document.getElementById('content');

  function showLoader() {
    content.innerHTML = '<div class="loader"><div class="spinner"></div><p>Загружаю данные…</p></div>';
  }

  function showError(message) {
    content.innerHTML = '<div class="screen"><div class="error-box">'
      + '<b>Не удалось загрузить данные</b><br>' + escapeHtml(message) + '</div></div>';
  }

  function setLive(online) {
    document.getElementById('live-dot').className = 'live-dot ' + (online ? 'online' : 'offline');
  }

  async function loadScreen(screen, silent) {
    if (!silent) showLoader();
    if (screen !== 'overview') destroyChart();

    try {
      if (screen === 'overview') {
        const data = await api('/api/overview');
        state.overview = data;
        content.innerHTML = renderOverview(data);
        updateBadge(data.active.length);
        setLive(data.scanner && data.scanner.running);
        drawCandles(null, data.active);
      } else if (screen === 'signals') {
        const filter = state.signals.filter;
        const query = filter === 'all' ? '' : '?status=' + encodeURIComponent(filter);
        const data = await api('/api/signals' + query);
        state.signals.items = data.items;
        content.innerHTML = renderSignals(data.items, filter);
        bindFilters('data-filter', function (v) { state.signals.filter = v; loadScreen('signals'); });
      } else if (screen === 'stats') {
        const data = await api('/api/stats?period=' + state.stats.period);
        state.stats.data = data;
        content.innerHTML = renderStats(data, state.stats.period);
        bindFilters('data-period', function (v) { state.stats.period = v; loadScreen('stats'); });
      } else if (screen === 'help') {
        if (!state.help) {
          const data = await api('/api/help');
          state.help = data.topics || [];
        }
        content.innerHTML = renderHelp(state.help);
        bindHelp();
      } else if (screen === 'settings') {
        const [cfg, news, brokers] = await Promise.all([
          api('/api/config'), api('/api/news'),
          api('/api/brokers').catch(function () { return { items: [] }; }),
        ]);
        state.brokers = brokers.items || [];
        state.settings.fields = cfg.fields;
        state.settings.groups = cfg.groups;
        state.settings.readonly = cfg.readonly;
        state.news = news;
        content.innerHTML = renderSettings(
          { fields: cfg.fields, groups: cfg.groups, readonly: cfg.readonly }, news);
        bindSettings();
      }
    } catch (err) {
      setLive(false);
      showError(err.message);
    }
  }

  function updateBadge(count) {
    const badge = document.getElementById('tab-badge');
    if (count > 0) { badge.textContent = count; badge.hidden = false; }
    else badge.hidden = true;
  }

  /* ---------------------------------------------------------- События */

  function bindFilters(attr, handler) {
    document.querySelectorAll('[' + attr + ']').forEach(function (btn) {
      btn.addEventListener('click', function () {
        haptic('select');
        handler(btn.getAttribute(attr));
      });
    });
  }

  function bindSettings() {
    // Переключатели
    document.querySelectorAll('.switch[data-key]').forEach(function (btn) {
      btn.addEventListener('click', async function () {
        const key = btn.dataset.key;
        const next = !btn.classList.contains('on');
        btn.classList.toggle('on', next);
        haptic('light');
        const ok = await saveField(key, next);
        if (!ok) btn.classList.toggle('on', !next);
      });
    });

    // Выпадающие списки
    document.querySelectorAll('.select[data-key]').forEach(function (sel) {
      const before = sel.value;
      sel.addEventListener('change', async function () {
        const ok = await saveField(sel.dataset.key, sel.value);
        if (!ok) sel.value = before;
        else loadScreen('settings', true);
      });
    });

    // Числовые поля со степперами
    document.querySelectorAll('.step-btn').forEach(function (btn) {
      const key = btn.dataset.key;
      const dir = parseInt(btn.dataset.dir, 10);
      let timer = null;
      let ticks = 0;

      function bump() {
        const input = document.getElementById('f-' + key);
        if (!input) return;
        const step = parseFloat(input.dataset.step) || 1;
        // Разгон при удержании: сначала по шагу, потом крупнее.
        // Иначе докрутить от 0 до 240 по одной минуте невозможно.
        const factor = ticks > 25 ? 25 : (ticks > 8 ? 5 : 1);
        const current = parseFloat(String(input.value).replace(',', '.')) || 0;
        const next = Math.round((current + step * factor * dir) * 1000) / 1000;
        input.value = String(next);
        ticks += 1;
      }

      function start(event) {
        event.preventDefault();
        ticks = 0;
        bump();
        haptic('light');
        // Первое повторение с задержкой, чтобы одиночное нажатие
        // не превращалось в серию
        timer = setTimeout(function () {
          timer = setInterval(bump, 90);
        }, 450);
      }

      function stop() {
        if (timer === null) return;
        clearTimeout(timer);
        clearInterval(timer);
        timer = null;
        const input = document.getElementById('f-' + key);
        if (input) commitNumber(key, input);
      }

      btn.addEventListener('pointerdown', start);
      btn.addEventListener('pointerup', stop);
      btn.addEventListener('pointerleave', stop);
      btn.addEventListener('pointercancel', stop);
      // Чтобы палец не «уезжал» и не превращал удержание в прокрутку
      btn.addEventListener('contextmenu', function (e) { e.preventDefault(); });
    });
    document.querySelectorAll('.step-input, .text-input:not(.secret-input)').forEach(function (input) {
      input.addEventListener('change', function () { commitNumber(input.dataset.key, input); });
      input.addEventListener('blur', function () { commitNumber(input.dataset.key, input); });
    });

    // Секреты сохраняются только по явному вводу: пустое поле означает
    // «оставить как было», иначе любой заход в настройки стирал бы токен
    document.querySelectorAll('.secret-input').forEach(function (input) {
      const save = async function () {
        const value = input.value.trim();
        if (!value) return;
        const ok = await saveField(input.dataset.key, value);
        if (ok) { input.value = ''; loadScreen('settings', true); }
      };
      input.addEventListener('change', save);
      input.addEventListener('blur', save);
    });
    document.querySelectorAll('[data-clear]').forEach(function (btn) {
      btn.addEventListener('click', async function () {
        haptic('light');
        const ok = await saveField(btn.dataset.clear, '');
        if (ok) loadScreen('settings', true);
      });
    });

    // Инструменты
    const addBtn = document.getElementById('open-symbols');
    if (addBtn) addBtn.addEventListener('click', openSymbolSheet);
    document.querySelectorAll('[data-remove]').forEach(function (btn) {
      btn.addEventListener('click', function () { toggleSymbol(btn.dataset.remove); });
    });

    const adv = document.getElementById('toggle-advanced');
    if (adv) adv.addEventListener('click', function () {
      state.settings.showAdvanced = !state.settings.showAdvanced;
      haptic('select');
      content.innerHTML = renderSettings({
        fields: state.settings.fields,
        groups: state.settings.groups,
        readonly: state.settings.readonly,
      }, state.news);
      bindSettings();
    });
  }

  let commitTimer = null;
  function commitNumber(key, input) {
    clearTimeout(commitTimer);
    const raw = String(input.value).replace(',', '.').trim();
    commitTimer = setTimeout(async function () {
      const field = fieldByKey(key);
      if (field && String(field.value) === raw) return;
      const ok = await saveField(key, raw);
      if (!ok && field) input.value = String(field.value);
    }, 350);
  }

  function switchScreen(screen) {
    if (state.screen === screen) return;
    state.screen = screen;
    document.getElementById('screen-title').textContent = SCREEN_TITLES[screen];
    document.querySelectorAll('.tab').forEach(function (tab) {
      tab.classList.toggle('active', tab.dataset.screen === screen);
    });
    window.scrollTo({ top: 0 });
    haptic('select');
    loadScreen(screen);
    scheduleAutoRefresh();
  }

  function scheduleAutoRefresh() {
    if (state.autoTimer) clearInterval(state.autoTimer);
    if (state.screen === 'overview') {
      state.autoTimer = setInterval(function () {
        if (!document.hidden) loadScreen('overview', true);
      }, 30000);
    }
  }

  document.querySelectorAll('.tab').forEach(function (tab) {
    tab.addEventListener('click', function () { switchScreen(tab.dataset.screen); });
  });

  document.getElementById('help-btn').addEventListener('click', function () {
    haptic('select');
    if (state.screen === 'help') {
      // Повторное нажатие возвращает туда, откуда пришли
      const back = state.prevScreen || 'overview';
      state.screen = null;
      switchScreen(back);
      return;
    }
    state.prevScreen = state.screen;
    state.screen = 'help';
    document.getElementById('screen-title').textContent = SCREEN_TITLES.help;
    document.querySelectorAll('.tab').forEach(function (t) { t.classList.remove('active'); });
    window.scrollTo({ top: 0 });
    if (state.autoTimer) clearInterval(state.autoTimer);
    loadScreen('help');
  });

  document.getElementById('refresh-btn').addEventListener('click', function () {
    const btn = this;
    btn.classList.add('spinning');
    haptic('light');
    Promise.resolve(loadScreen(state.screen, true)).finally(function () {
      setTimeout(function () { btn.classList.remove('spinning'); }, 400);
    });
  });

  document.getElementById('sheet-close').addEventListener('click', closeSymbolSheet);
  document.getElementById('sheet').addEventListener('click', function (e) {
    if (e.target === this) closeSymbolSheet();
  });
  document.getElementById('symbol-search').addEventListener('input', function () {
    const value = this.value;
    clearTimeout(symbolSearchTimer);
    symbolSearchTimer = setTimeout(function () { loadSymbols(value); }, 300);
  });

  document.addEventListener('visibilitychange', function () {
    if (!document.hidden && state.screen === 'overview') loadScreen('overview', true);
  });

  /* ------------------------------------------------------------- Старт */

  initTelegram();
  loadScreen('overview');
  scheduleAutoRefresh();
})();

'use strict';

/**
 * SuperNav · 交通规划 — 前端逻辑
 * 纯原生 JS（无框架、无构建、无外部依赖）。
 * 所有接口字段与 docs/API-CONTRACT.md 保持一致；费用一律以后端返回为准，前端只做展示。
 */
(function () {
  // ===== 常量 =====
  const THEME_KEY = 'supernav-theme';
  const THEME_VALUES = ['light', 'gray', 'dark'];
  const MIN_POINTS = 2;
  const MAX_POINTS = 10;
  const DEFAULT_MODE = 'walking';
  const REQUEST_TIMEOUT_MS = 45000;

  const FALLBACK_MODES = [
    { value: 'walking', label: '步行' },
    { value: 'bicycling', label: '骑行' },
    { value: 'transit', label: '公交/地铁' },
    { value: 'taxi', label: '打车' },
    { value: 'driving', label: '驾车' }
  ];

  const FALLBACK_DEFAULTS = {
    fuel_price: 7.8,
    fuel_consumption: 8.0,
    transit_strategy: 0,
    taxi_base_fare: 13.0,
    taxi_base_km: 3.0,
    taxi_per_km: 2.3
  };

  const TRANSIT_STRATEGIES = [
    { value: 0, label: '推荐' },
    { value: 8, label: '时间短' },
    { value: 2, label: '少换乘' },
    { value: 1, label: '最经济' }
  ];

  const MODE_COLORS = {
    walking: '#22c55e',
    bicycling: '#06b6d4',
    transit: '#3b82f6',
    taxi: '#f59e0b',
    driving: '#a855f7'
  };

  const EXAMPLES = [
    {
      label: '北京南站 → 故宫博物院 · 公交',
      points: ['北京南站', '故宫博物院'],
      modes: ['transit']
    },
    {
      label: '北京南站 → 首都机场 · 打车',
      points: ['北京南站', '首都机场'],
      modes: ['taxi']
    },
    {
      label: '国贸 → 中关村 · 骑行',
      points: ['国贸', '中关村'],
      modes: ['bicycling']
    },
    {
      label: '北京南站 → 故宫 → 北京西站 · 公交 + 骑行',
      points: ['北京南站', '故宫博物院', '北京西站'],
      modes: ['transit', 'bicycling']
    }
  ];

  // ===== 状态 =====
  const state = {
    points: ['', ''],
    modes: [DEFAULT_MODE],
    modesMeta: FALLBACK_MODES.map(copyMode),
    defaults: Object.assign({}, FALLBACK_DEFAULTS),
    settingsTouched: false,
    statusLoaded: false,
    status: null,
    loading: false,
    lastResult: null
  };

  const dom = {};

  class PlanError extends Error {
    constructor(message, hint) {
      super(message);
      this.name = 'PlanError';
      this.hint = hint || '';
    }
  }

  // ===== 工具函数 =====
  function copyMode(mode) {
    return { value: mode.value, label: mode.label };
  }

  function el(tag, className, text) {
    const node = document.createElement(tag);
    if (className) {
      node.className = className;
    }
    if (text !== undefined && text !== null) {
      node.textContent = String(text);
    }
    return node;
  }

  function svgEl(tag, attrs) {
    const node = document.createElementNS('http://www.w3.org/2000/svg', tag);
    if (attrs) {
      Object.keys(attrs).forEach(function (name) {
        node.setAttribute(name, String(attrs[name]));
      });
    }
    return node;
  }

  function clear(node) {
    if (!node) {
      return;
    }
    while (node.firstChild) {
      node.removeChild(node.firstChild);
    }
  }

  function isNonEmptyString(value) {
    return typeof value === 'string' && value.trim() !== '';
  }

  function parseNumber(value) {
    if (value === null || value === undefined) {
      return null;
    }
    const text = String(value).trim();
    if (text === '') {
      return null;
    }
    const number = Number(text);
    return Number.isFinite(number) ? number : null;
  }

  function safeGetItem(key) {
    try {
      return window.localStorage.getItem(key);
    } catch (error) {
      return null;
    }
  }

  function safeSetItem(key, value) {
    try {
      window.localStorage.setItem(key, value);
    } catch (error) {
      // 隐私模式 / localStorage 不可用：主题切换仍然生效，只是不持久化。
    }
  }

  function formatMoney(value) {
    const number = parseNumber(value);
    if (number === null) {
      return '—';
    }
    return '¥' + number.toFixed(2);
  }

  function formatDistance(value) {
    const number = parseNumber(value);
    if (number === null || number < 0) {
      return '—';
    }
    if (number < 1000) {
      return Math.round(number) + ' 米';
    }
    const km = number / 1000;
    return (km < 10 ? km.toFixed(2) : km.toFixed(1)) + ' 公里';
  }

  function formatDuration(value) {
    const number = parseNumber(value);
    if (number === null || number < 0) {
      return '—';
    }
    const seconds = Math.round(number);
    if (seconds < 60) {
      return seconds + ' 秒';
    }
    const totalMinutes = Math.round(seconds / 60);
    const hours = Math.floor(totalMinutes / 60);
    const minutes = totalMinutes % 60;
    if (hours > 0) {
      return minutes > 0 ? hours + ' 小时 ' + minutes + ' 分钟' : hours + ' 小时';
    }
    return minutes + ' 分钟';
  }

  function formatDateTime(value) {
    if (!isNonEmptyString(value)) {
      return '';
    }
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) {
      return value;
    }
    try {
      return date.toLocaleString('zh-CN', {
        year: 'numeric',
        month: '2-digit',
        day: '2-digit',
        hour: '2-digit',
        minute: '2-digit'
      });
    } catch (error) {
      return value;
    }
  }

  function defaultMode() {
    return DEFAULT_MODE;
  }

  function modeLabel(mode) {
    for (let i = 0; i < state.modesMeta.length; i += 1) {
      if (state.modesMeta[i].value === mode) {
        return state.modesMeta[i].label;
      }
    }
    for (let j = 0; j < FALLBACK_MODES.length; j += 1) {
      if (FALLBACK_MODES[j].value === mode) {
        return FALLBACK_MODES[j].label;
      }
    }
    return isNonEmptyString(mode) ? mode : '未知方式';
  }

  function modeLabelForLeg(leg) {
    if (leg && isNonEmptyString(leg.mode_label)) {
      return leg.mode_label;
    }
    return modeLabel(leg && leg.mode);
  }

  function modeColor(mode) {
    return MODE_COLORS[mode] || 'var(--accent)';
  }

  function stepTypeLabel(type) {
    const labels = {
      walking: '步行',
      walk: '步行',
      bicycling: '骑行',
      bicycle: '骑行',
      bus: '公交',
      busline: '公交',
      subway: '地铁',
      metro: '地铁',
      railway: '火车',
      train: '火车',
      taxi: '打车',
      driving: '驾车',
      transit: '公交/地铁'
    };
    if (typeof type === 'string' && labels[type]) {
      return labels[type];
    }
    return isNonEmptyString(type) ? type : '路段';
  }

  function isLngLatPair(value) {
    if (!Array.isArray(value) || value.length < 2) {
      return false;
    }
    const lng = Number(value[0]);
    const lat = Number(value[1]);
    return (
      Number.isFinite(lng) &&
      Number.isFinite(lat) &&
      Math.abs(lng) <= 180 &&
      Math.abs(lat) <= 90
    );
  }

  // ===== DOM 缓存 =====
  function cacheDom() {
    const ids = {
      demoBanner: 'demo-banner',
      demoBannerReason: 'demo-banner-reason',
      statusNotice: 'status-notice',
      statusNoticeText: 'status-notice-text',
      statusRetry: 'status-retry',
      planForm: 'plan-form',
      pointsList: 'points-list',
      pointsHint: 'points-hint',
      addPoint: 'add-point',
      submitBtn: 'submit-btn',
      submitLabel: 'submit-label',
      exampleList: 'example-list',
      fuelPrice: 'fuel-price',
      fuelConsumption: 'fuel-consumption',
      transitStrategy: 'transit-strategy',
      resetOptions: 'reset-options',
      alert: 'alert',
      alertMessage: 'alert-message',
      alertHint: 'alert-hint',
      alertRetry: 'alert-retry',
      alertDismiss: 'alert-dismiss',
      result: 'result',
      resultSource: 'result-source',
      totalsDistance: 'totals-distance',
      totalsDuration: 'totals-duration',
      totalsCost: 'totals-cost',
      totalsBreakdown: 'totals-breakdown',
      totalsCombo: 'totals-combo',
      generatedAt: 'generated-at',
      routeSection: 'route-section',
      routeCanvas: 'route-canvas',
      routeLegend: 'route-legend',
      legsList: 'legs-list',
      legsCount: 'legs-count',
      emptyState: 'empty-state'
    };
    Object.keys(ids).forEach(function (key) {
      dom[key] = document.getElementById(ids[key]);
    });
    dom.themeButtons = Array.prototype.slice.call(document.querySelectorAll('[data-theme-choice]'));
  }

  // ===== 主题 =====
  function initTheme() {
    let theme = safeGetItem(THEME_KEY);
    if (THEME_VALUES.indexOf(theme) < 0) {
      theme = 'light';
    }
    applyTheme(theme, false);
  }

  function applyTheme(theme, persist) {
    const next = THEME_VALUES.indexOf(theme) >= 0 ? theme : 'light';
    document.documentElement.setAttribute('data-theme', next);
    dom.themeButtons.forEach(function (button) {
      const active = button.getAttribute('data-theme-choice') === next;
      button.setAttribute('aria-pressed', active ? 'true' : 'false');
    });
    if (persist) {
      safeSetItem(THEME_KEY, next);
    }
  }

  // ===== 表单 =====
  function normalizeModes() {
    const needed = Math.max(0, state.points.length - 1);
    if (state.modes.length > needed) {
      state.modes = state.modes.slice(0, needed);
    }
    while (state.modes.length < needed) {
      state.modes.push(DEFAULT_MODE);
    }
    state.modes = state.modes.map(function (mode) {
      return mode && typeof mode === 'string' ? mode : DEFAULT_MODE;
    });
  }

  function pointKind(index) {
    if (index === 0) {
      return 'start';
    }
    if (index === state.points.length - 1) {
      return 'end';
    }
    return 'via';
  }

  function pointKindLabel(index) {
    const kind = pointKind(index);
    if (kind === 'start') {
      return '起点';
    }
    if (kind === 'end') {
      return '终点';
    }
    return '途经点 ' + index;
  }

  function renderForm() {
    normalizeModes();
    clear(dom.pointsList);
    state.points.forEach(function (address, index) {
      dom.pointsList.appendChild(buildPointRow(address, index));
      if (index < state.points.length - 1) {
        dom.pointsList.appendChild(buildModeRow(index));
      }
    });
    updateAddPointState();
    updatePointsHint();
    updateSegmentLabels();
  }

  function buildPointRow(address, index) {
    const row = el('div', 'point-row');
    row.setAttribute('data-kind', pointKind(index));

    const label = el('span', 'point-index');
    label.appendChild(el('span', 'point-dot'));
    label.appendChild(el('span', 'point-index-text', pointKindLabel(index)));

    const input = el('input', 'point-input');
    input.type = 'text';
    input.value = address;
    input.id = 'point-input-' + index;
    input.setAttribute('data-point-index', String(index));
    input.setAttribute('aria-label', pointKindLabel(index) + '地址');
    input.setAttribute('autocomplete', 'off');
    input.setAttribute('spellcheck', 'false');
    if (pointKind(index) === 'start') {
      input.setAttribute('placeholder', '起点地址，例如：北京南站');
    } else if (pointKind(index) === 'end') {
      input.setAttribute('placeholder', '终点地址，例如：首都机场');
    } else {
      input.setAttribute('placeholder', '途经点地址');
    }

    const actions = el('div', 'point-actions');
    if (pointKind(index) === 'via') {
      const remove = el('button', 'btn btn-small btn-quiet remove-point', '删除');
      remove.type = 'button';
      remove.setAttribute('data-remove-index', String(index));
      remove.setAttribute('aria-label', '删除' + pointKindLabel(index));
      remove.title = '删除途经点';
      actions.appendChild(remove);
    }

    row.appendChild(label);
    row.appendChild(input);
    row.appendChild(actions);
    return row;
  }

  function buildModeRow(index) {
    const row = el('div', 'mode-row');

    const label = el('span', 'mode-row-label', '第 ' + (index + 1) + ' 段');
    const route = el('span', 'mode-row-route');
    route.setAttribute('data-mode-route-index', String(index));

    const select = el('select', 'mode-select');
    select.setAttribute('data-mode-index', String(index));
    select.setAttribute('aria-label', '第 ' + (index + 1) + ' 段交通方式');
    state.modes[index] = fillModeSelect(select, state.modes[index]);

    row.appendChild(label);
    row.appendChild(route);
    row.appendChild(select);
    return row;
  }

  function fillModeSelect(select, value) {
    clear(select);
    const available = state.modesMeta.map(function (mode) {
      return mode.value;
    });
    let current = value;
    if (available.indexOf(current) < 0) {
      current = available.indexOf(DEFAULT_MODE) >= 0 ? DEFAULT_MODE : available[0];
    }
    state.modesMeta.forEach(function (mode) {
      const option = el('option', '', mode.label);
      option.value = mode.value;
      select.appendChild(option);
    });
    select.value = current;
    return select.value;
  }

  function refreshModeOptions() {
    if (!dom.pointsList) {
      return;
    }
    const selects = dom.pointsList.querySelectorAll('select[data-mode-index]');
    Array.prototype.forEach.call(selects, function (select) {
      const index = Number(select.getAttribute('data-mode-index'));
      if (!Number.isInteger(index) || index < 0 || index >= state.modes.length) {
        return;
      }
      state.modes[index] = fillModeSelect(select, state.modes[index]);
    });
  }

  function updateSegmentLabels() {
    if (!dom.pointsList) {
      return;
    }
    const labels = dom.pointsList.querySelectorAll('[data-mode-route-index]');
    Array.prototype.forEach.call(labels, function (label) {
      const index = Number(label.getAttribute('data-mode-route-index'));
      const from = (state.points[index] || '').trim();
      const to = (state.points[index + 1] || '').trim();
      label.textContent = (from || '起点') + ' → ' + (to || '终点');
    });
  }

  function updateAddPointState() {
    const atMax = state.points.length >= MAX_POINTS;
    dom.addPoint.disabled = atMax;
    dom.addPoint.title = atMax ? '最多 ' + MAX_POINTS + ' 个地点' : '在当前终点前插入途经点';
  }

  function updatePointsHint() {
    const count = state.points.length;
    dom.pointsHint.textContent =
      '共 ' + count + ' 个地点、' + Math.max(0, count - 1) + ' 段；最多 ' + MAX_POINTS + ' 个地点，每段可单独选择交通方式。';
  }

  function focusPointInput(index) {
    const input = dom.pointsList.querySelector('input[data-point-index="' + index + '"]');
    if (!input) {
      return;
    }
    input.focus();
    try {
      input.scrollIntoView({ behavior: 'smooth', block: 'center' });
    } catch (error) {
      input.scrollIntoView();
    }
  }

  function markPointInvalid(index) {
    const input = dom.pointsList.querySelector('input[data-point-index="' + index + '"]');
    if (input) {
      input.classList.add('is-invalid');
    }
  }

  function clearInvalidMarks() {
    if (!dom.pointsList) {
      return;
    }
    const inputs = dom.pointsList.querySelectorAll('.point-input.is-invalid');
    Array.prototype.forEach.call(inputs, function (input) {
      input.classList.remove('is-invalid');
    });
  }

  function addWaypoint() {
    if (state.points.length >= MAX_POINTS) {
      return;
    }
    const insertIndex = state.points.length - 1;
    state.points.splice(insertIndex, 0, '');
    state.modes.splice(insertIndex, 0, DEFAULT_MODE);
    renderForm();
    focusPointInput(insertIndex);
  }

  function removeWaypoint(index) {
    if (index <= 0 || index >= state.points.length - 1) {
      return;
    }
    state.points.splice(index, 1);
    state.modes.splice(index, 1);
    renderForm();
    focusPointInput(Math.min(index, state.points.length - 2));
  }

  // ===== 计价设置 =====
  function renderTransitStrategies() {
    clear(dom.transitStrategy);
    TRANSIT_STRATEGIES.forEach(function (strategy) {
      const option = el('option', '', strategy.label + '（' + strategy.value + '）');
      option.value = String(strategy.value);
      dom.transitStrategy.appendChild(option);
    });
  }

  function setSelectValue(select, value) {
    const values = Array.prototype.map.call(select.options, function (option) {
      return option.value;
    });
    if (values.indexOf(value) < 0) {
      const option = el('option', '', '自定义（' + value + '）');
      option.value = String(value);
      select.appendChild(option);
    }
    select.value = String(value);
  }

  function applyDefaults() {
    const defaults = state.defaults || FALLBACK_DEFAULTS;
    if (state.settingsTouched) {
      return;
    }
    if (parseNumber(defaults.fuel_price) !== null) {
      dom.fuelPrice.value = String(defaults.fuel_price);
    }
    if (parseNumber(defaults.fuel_consumption) !== null) {
      dom.fuelConsumption.value = String(defaults.fuel_consumption);
    }
    if (parseNumber(defaults.transit_strategy) !== null) {
      setSelectValue(dom.transitStrategy, String(defaults.transit_strategy));
    }
  }

  function buildOptions() {
    const options = {};
    const fuelPrice = parseNumber(dom.fuelPrice.value);
    if (fuelPrice !== null && fuelPrice >= 0) {
      options.fuel_price = fuelPrice;
    }
    const fuelConsumption = parseNumber(dom.fuelConsumption.value);
    if (fuelConsumption !== null && fuelConsumption >= 0) {
      options.fuel_consumption = fuelConsumption;
    }
    const strategy = parseNumber(dom.transitStrategy.value);
    if (strategy !== null && Number.isInteger(strategy) && strategy >= 0 && strategy <= 8) {
      options.transit_strategy = strategy;
    }
    return options;
  }

  // ===== 快捷示例 =====
  function renderExamples() {
    clear(dom.exampleList);
    EXAMPLES.forEach(function (example) {
      const button = el('button', 'example-btn', example.label);
      button.type = 'button';
      button.addEventListener('click', function () {
        applyExample(example);
      });
      dom.exampleList.appendChild(button);
    });
  }

  function applyExample(example) {
    state.points = example.points.slice();
    state.modes = example.modes.slice();
    renderForm();
    clearAlert();
    hideResult();
    focusPointInput(0);
  }

  // ===== 状态接口 =====
  function showStatusNotice() {
    dom.statusNotice.hidden = false;
    dom.statusNoticeText.textContent =
      '无法连接后端服务（/api/status）。请先在项目根目录启动后端：python -m uvicorn backend.app:app --host 127.0.0.1 --port 8000，然后点击「重试连接」。';
  }

  function hideStatusNotice() {
    dom.statusNotice.hidden = true;
  }

  function applyStatus(data) {
    state.status = data && typeof data === 'object' ? data : {};
    state.statusLoaded = true;
    const status = state.status;

    if (Array.isArray(status.modes) && status.modes.length) {
      const modes = [];
      status.modes.forEach(function (mode) {
        if (!mode || typeof mode !== 'object' || !isNonEmptyString(mode.value)) {
          return;
        }
        const label = isNonEmptyString(mode.label) ? mode.label : modeLabel(mode.value);
        modes.push({ value: mode.value, label: label });
      });
      if (modes.length) {
        state.modesMeta = modes;
      }
    }

    if (status.defaults && typeof status.defaults === 'object') {
      state.defaults = Object.assign({}, FALLBACK_DEFAULTS, status.defaults);
    }

    refreshModeOptions();
    applyDefaults();

    if (status.demo === true) {
      setDemoBanner(true, isNonEmptyString(status.demo_reason) ? status.demo_reason : '');
    } else {
      setDemoBanner(false, '');
    }
    hideStatusNotice();
  }

  async function loadStatus() {
    try {
      const response = await fetch('/api/status', {
        headers: { Accept: 'application/json' },
        cache: 'no-store'
      });
      if (!response.ok) {
        throw new Error('HTTP ' + response.status);
      }
      const data = await response.json();
      applyStatus(data);
      return true;
    } catch (error) {
      showStatusNotice();
      return false;
    }
  }

  async function handleStatusRetry() {
    if (dom.statusRetry.disabled) {
      return;
    }
    dom.statusRetry.disabled = true;
    dom.statusNoticeText.textContent = '正在重新连接后端…';
    try {
      await loadStatus();
    } finally {
      dom.statusRetry.disabled = false;
    }
  }

  function setDemoBanner(visible, reason) {
    dom.demoBanner.hidden = !visible;
    if (!visible) {
      return;
    }
    dom.demoBannerReason.textContent = reason ? '原因：' + reason : '';
    dom.demoBannerReason.hidden = !reason;
  }

  // ===== 校验 / 请求体 =====
  function validateForm() {
    const trimmed = state.points.map(function (address) {
      return String(address || '').trim();
    });

    if (trimmed.length < MIN_POINTS) {
      return { message: '至少需要起点和终点（2 个地点）。' };
    }
    if (trimmed.length > MAX_POINTS) {
      return { message: '最多支持 ' + MAX_POINTS + ' 个地点。' };
    }

    const emptyIndexes = [];
    trimmed.forEach(function (address, index) {
      if (address === '') {
        emptyIndexes.push(index);
      }
    });
    if (emptyIndexes.length) {
      return {
        message: '请填写' + emptyIndexes.map(function (index) {
          return pointKindLabel(index);
        }).join('、') + '的地址。',
        hint: '每一段两端都需要地点，才能规划路线。',
        emptyIndexes: emptyIndexes,
        focusIndex: emptyIndexes[0]
      };
    }

    normalizeModes();
    if (state.modes.length !== trimmed.length - 1) {
      return { message: '交通方式数量与路段数量不一致，请刷新页面后重试。' };
    }

    const available = state.modesMeta.map(function (mode) {
      return mode.value;
    });
    for (let i = 0; i < state.modes.length; i += 1) {
      if (available.indexOf(state.modes[i]) < 0) {
        return { message: '第 ' + (i + 1) + ' 段的交通方式无效，请重新选择。' };
      }
    }
    return null;
  }

  function buildPayload() {
    normalizeModes();
    return {
      points: state.points.map(function (address) {
        return { address: String(address || '').trim() };
      }),
      modes: state.modes.slice(),
      options: buildOptions()
    };
  }

  // ===== 错误处理 =====
  function showAlert(details) {
    dom.alertMessage.textContent = details && details.message ? details.message : '请求失败。';
    const hint = details && details.hint ? details.hint : '';
    dom.alertHint.textContent = hint;
    dom.alertHint.hidden = !hint;
    dom.alert.hidden = false;
    try {
      dom.alert.focus({ preventScroll: true });
    } catch (error) {
      dom.alert.focus();
    }
  }

  function clearAlert() {
    dom.alert.hidden = true;
    dom.alertMessage.textContent = '';
    dom.alertHint.textContent = '';
    dom.alertHint.hidden = true;
  }

  function extractError(data, status, rawText) {
    if (data && typeof data === 'object') {
      if (data.error && typeof data.error === 'object') {
        const message = isNonEmptyString(data.error.message) ? data.error.message : '';
        const hint = isNonEmptyString(data.error.hint) ? data.error.hint : '';
        const code = isNonEmptyString(data.error.code) ? data.error.code : '';
        if (message || code) {
          return {
            message: message || '请求失败（' + code + '）',
            hint: hint
          };
        }
      }
      if (isNonEmptyString(data.detail)) {
        return { message: data.detail, hint: '' };
      }
      if (Array.isArray(data.detail)) {
        const parts = data.detail.map(function (item) {
          if (!item || typeof item !== 'object') {
            return '';
          }
          const location = Array.isArray(item.loc)
            ? item.loc.filter(function (part) {
                return part !== 'body';
              }).join('.')
            : '';
          const message = isNonEmptyString(item.msg) ? item.msg : '参数校验失败';
          return (location ? location + '：' : '') + message;
        }).filter(isNonEmptyString);
        return {
          message: '请求参数校验失败（HTTP ' + status + '）。',
          hint: parts.join('；')
        };
      }
      if (isNonEmptyString(data.message)) {
        return { message: data.message, hint: '' };
      }
    }

    if (status === 404 || status === 405) {
      return {
        message: '接口不可用（HTTP ' + status + '）。',
        hint: '请确认后端已启动；如果当前是静态服务器预览页面（例如 8012 端口），则 /api 不可用属于预期，请改用后端地址访问。'
      };
    }

    const snippet = isNonEmptyString(rawText) ? rawText.replace(/\s+/g, ' ').trim().slice(0, 200) : '';
    return {
      message: '请求失败（HTTP ' + status + '）。',
      hint: snippet ? '服务端返回：' + snippet : ''
    };
  }

  // ===== 规划请求 =====
  async function requestPlan(payload) {
    const controller = typeof AbortController === 'function' ? new AbortController() : null;
    let timer = null;
    if (controller) {
      timer = window.setTimeout(function () {
        controller.abort();
      }, REQUEST_TIMEOUT_MS);
    }

    let response;
    try {
      response = await fetch('/api/plan', {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          Accept: 'application/json'
        },
        body: JSON.stringify(payload),
        signal: controller ? controller.signal : undefined
      });
    } catch (error) {
      if (error && error.name === 'AbortError') {
        throw new PlanError(
          '请求超时（超过 ' + Math.round(REQUEST_TIMEOUT_MS / 1000) + ' 秒）。',
          '后端可能正在等待高德接口，请稍后重试；若持续超时请检查后端日志。'
        );
      }
      throw new PlanError(
        '无法连接后端服务。',
        '请确认后端已启动：在项目根目录执行 python -m uvicorn backend.app:app --host 127.0.0.1 --port 8000，然后重试。'
      );
    } finally {
      if (timer !== null) {
        window.clearTimeout(timer);
      }
    }

    let text = '';
    let data = null;
    try {
      text = await response.text();
      if (isNonEmptyString(text)) {
        data = JSON.parse(text);
      }
    } catch (error) {
      data = null;
    }

    if (!response.ok) {
      const details = extractError(data, response.status, text);
      throw new PlanError(details.message, details.hint);
    }
    if (!data || typeof data !== 'object') {
      throw new PlanError(
        '后端返回了无法解析的响应。',
        text ? '响应片段：' + text.replace(/\s+/g, ' ').slice(0, 200) : ''
      );
    }
    if (!Array.isArray(data.legs) || !data.totals || typeof data.totals !== 'object') {
      throw new PlanError(
        '后端响应缺少 legs / totals 字段，与接口契约不符。',
        '请检查后端输出结构（docs/API-CONTRACT.md 第 3.3 节）。'
      );
    }
    return data;
  }

  function setLoading(loading) {
    state.loading = loading;
    dom.submitBtn.disabled = loading;
    dom.submitBtn.classList.toggle('is-loading', loading);
    dom.submitBtn.setAttribute('aria-busy', loading ? 'true' : 'false');
    dom.submitLabel.textContent = loading ? '规划中…' : '开始规划';
    dom.planForm.classList.toggle('is-loading', loading);
    dom.result.classList.toggle('is-loading', loading);
  }

  async function handleSubmit() {
    if (state.loading) {
      return;
    }
    clearInvalidMarks();
    clearAlert();

    const validation = validateForm();
    if (validation) {
      if (validation.emptyIndexes) {
        validation.emptyIndexes.forEach(markPointInvalid);
      }
      showAlert({
        message: validation.message,
        hint: validation.hint || '请检查地点与每段的交通方式后重试。'
      });
      if (typeof validation.focusIndex === 'number') {
        focusPointInput(validation.focusIndex);
      }
      return;
    }

    const payload = buildPayload();
    setLoading(true);
    try {
      const data = await requestPlan(payload);
      renderResult(data);
    } catch (error) {
      hideResult();
      if (error instanceof PlanError) {
        showAlert({ message: error.message, hint: error.hint });
      } else {
        showAlert({
          message: '规划失败：发生了未预期的错误。',
          hint: error && error.message ? String(error.message) : ''
        });
      }
    } finally {
      setLoading(false);
    }
  }

  // ===== 结果渲染 =====
  function hideResult() {
    dom.result.hidden = true;
    dom.emptyState.hidden = false;
  }

  function renderResult(data) {
    state.lastResult = data;
    const wasHidden = dom.result.hidden;
    renderTotals(data);
    renderLegs(data);
    renderRoute(Array.isArray(data.legs) ? data.legs : []);
    updateSourceBadge(data.mode);
    clearAlert();
    dom.result.hidden = false;
    dom.emptyState.hidden = true;
    if (wasHidden) {
      try {
        dom.result.scrollIntoView({ behavior: 'smooth', block: 'start' });
      } catch (error) {
        dom.result.scrollIntoView();
      }
    }
  }

  function updateSourceBadge(mode) {
    const isDemo = mode === 'demo';
    const isLive = mode === 'live';

    if (isDemo) {
      dom.resultSource.textContent = '演示数据';
      dom.resultSource.setAttribute('data-mode', 'demo');
    } else if (isLive) {
      dom.resultSource.textContent = '实时数据';
      dom.resultSource.setAttribute('data-mode', 'live');
    } else {
      dom.resultSource.textContent = '来源：' + (isNonEmptyString(mode) ? mode : '未标注');
      dom.resultSource.setAttribute('data-mode', 'unknown');
    }

    if (isDemo) {
      const reason = state.status && isNonEmptyString(state.status.demo_reason)
        ? state.status.demo_reason
        : '未配置高德 Key，使用演示数据';
      setDemoBanner(true, reason);
    } else if (isLive) {
      setDemoBanner(false, '');
    } else if (state.status && state.status.demo === true) {
      setDemoBanner(
        true,
        isNonEmptyString(state.status.demo_reason) ? state.status.demo_reason : '未配置高德 Key，使用演示数据'
      );
    } else {
      setDemoBanner(false, '');
    }
  }

  function renderTotals(data) {
    const totals = data && data.totals && typeof data.totals === 'object' ? data.totals : {};
    const cost = totals.cost && typeof totals.cost === 'object' ? totals.cost : {};
    const totalAmount = parseNumber(cost.total);

    dom.totalsDistance.textContent = formatDistance(totals.distance_m);
    dom.totalsDuration.textContent = formatDuration(totals.duration_s);
    dom.totalsCost.textContent = formatMoney(totalAmount);
    dom.totalsCost.classList.toggle('is-free', totalAmount === 0);

    clear(dom.totalsBreakdown);
    const breakdown = Array.isArray(cost.breakdown) ? cost.breakdown : [];
    if (breakdown.length) {
      breakdown.forEach(function (entry) {
        if (!entry || typeof entry !== 'object') {
          return;
        }
        const item = el('li', 'breakdown-item');
        const label = isNonEmptyString(entry.label)
          ? entry.label
          : (isNonEmptyString(entry.key) ? entry.key : '费用');
        item.appendChild(el('span', 'breakdown-label', label));
        item.appendChild(el('span', 'breakdown-value', formatMoney(entry.amount)));
        dom.totalsBreakdown.appendChild(item);
      });
    } else {
      const item = el('li', 'breakdown-item');
      if (totalAmount !== null && totalAmount > 0) {
        item.appendChild(el('span', 'breakdown-label', '合计（明细未提供）'));
        item.appendChild(el('span', 'breakdown-value', formatMoney(totalAmount)));
      } else {
        item.appendChild(el('span', 'breakdown-label', '无费用'));
        item.appendChild(el('span', 'breakdown-value', '免费'));
      }
      dom.totalsBreakdown.appendChild(item);
    }

    renderCombo(Array.isArray(data.legs) ? data.legs : []);

    const generated = formatDateTime(data.generated_at);
    dom.generatedAt.textContent = generated ? '数据生成时间：' + generated : '';
    dom.generatedAt.hidden = !generated;
  }

  function renderCombo(legs) {
    clear(dom.totalsCombo);
    if (!legs.length) {
      dom.totalsCombo.hidden = true;
      return;
    }
    dom.totalsCombo.hidden = false;
    dom.totalsCombo.appendChild(el('span', 'combo-label', '组合：'));
    legs.forEach(function (leg, index) {
      if (!leg || typeof leg !== 'object') {
        return;
      }
      if (index > 0) {
        dom.totalsCombo.appendChild(el('span', 'combo-arrow', '→'));
      }
      const chip = el('span', 'combo-mode');
      const dot = el('span', 'combo-dot');
      dot.style.backgroundColor = modeColor(leg.mode);
      chip.appendChild(dot);
      chip.appendChild(document.createTextNode(modeLabelForLeg(leg)));
      dom.totalsCombo.appendChild(chip);
    });
  }

  function renderLegs(data) {
    const legs = Array.isArray(data.legs) ? data.legs : [];
    clear(dom.legsList);
    dom.legsCount.textContent = legs.length ? '共 ' + legs.length + ' 段' : '';
    legs.forEach(function (leg, index) {
      dom.legsList.appendChild(buildLegCard(leg, index, data));
    });
    if (!legs.length) {
      dom.legsList.appendChild(el('p', 'empty-text', '后端未返回任何路段数据。'));
    }
  }

  function buildLegCard(leg, index, data) {
    const safeLeg = leg && typeof leg === 'object' ? leg : {};
    const card = el('article', 'card leg-card');
    card.setAttribute('data-mode', isNonEmptyString(safeLeg.mode) ? safeLeg.mode : 'unknown');

    const cost = safeLeg.cost && typeof safeLeg.cost === 'object' ? safeLeg.cost : {};
    const costAmount = parseNumber(cost.total);

    const head = el('div', 'leg-head');
    head.appendChild(el('span', 'leg-index', '第 ' + (index + 1) + ' 段'));
    head.appendChild(buildModeBadge(safeLeg));
    const costNode = el('span', 'leg-cost', formatMoney(costAmount));
    if (costAmount === 0) {
      costNode.classList.add('is-free');
    }
    head.appendChild(costNode);
    card.appendChild(head);

    const route = el('div', 'leg-route');
    route.appendChild(el('span', 'leg-endpoint', endpointName(safeLeg, 'from', data, index)));
    route.appendChild(el('span', 'leg-arrow', '→'));
    route.appendChild(el('span', 'leg-endpoint', endpointName(safeLeg, 'to', data, index + 1)));
    card.appendChild(route);

    const metrics = el('dl', 'leg-metrics');
    metrics.appendChild(buildMetric('距离', formatDistance(safeLeg.distance_m)));
    metrics.appendChild(buildMetric('耗时', formatDuration(safeLeg.duration_s)));
    metrics.appendChild(buildMetric('该段费用', formatMoney(costAmount)));
    card.appendChild(metrics);

    const breakdown = Array.isArray(cost.breakdown) ? cost.breakdown : [];
    if (breakdown.length) {
      const list = el('ul', 'leg-breakdown');
      breakdown.forEach(function (entry) {
        if (!entry || typeof entry !== 'object') {
          return;
        }
        const chip = el('li', 'fee-chip');
        const label = isNonEmptyString(entry.label)
          ? entry.label
          : (isNonEmptyString(entry.key) ? entry.key : '费用');
        chip.appendChild(el('span', 'fee-label', label));
        chip.appendChild(el('span', 'fee-value', formatMoney(entry.amount)));
        list.appendChild(chip);
      });
      if (list.firstChild) {
        card.appendChild(list);
      }
    } else if (costAmount === 0) {
      card.appendChild(el('p', 'leg-free-note', '本段免费（无费用明细）'));
    } else if (costAmount !== null && costAmount > 0) {
      card.appendChild(el('p', 'leg-free-note', '费用明细未提供，仅显示该段合计'));
    }

    if (isNonEmptyString(safeLeg.summary)) {
      card.appendChild(el('p', 'leg-summary', safeLeg.summary));
    }

    const steps = Array.isArray(safeLeg.steps) ? safeLeg.steps : [];
    if (steps.length) {
      const list = el('ol', 'leg-steps');
      steps.forEach(function (step) {
        if (step && typeof step === 'object') {
          list.appendChild(buildStep(step));
        }
      });
      if (list.firstChild) {
        card.appendChild(list);
      }
    }

    const warnings = Array.isArray(safeLeg.warnings)
      ? safeLeg.warnings.filter(isNonEmptyString)
      : [];
    if (warnings.length) {
      const box = el('div', 'leg-warnings');
      box.appendChild(el('span', 'warn-tag', '提示'));
      const list = el('ul', 'warn-list');
      warnings.forEach(function (warning) {
        list.appendChild(el('li', '', warning));
      });
      box.appendChild(list);
      card.appendChild(box);
    }

    return card;
  }

  function buildModeBadge(leg) {
    const badge = el('span', 'mode-badge');
    badge.setAttribute('data-mode', isNonEmptyString(leg.mode) ? leg.mode : 'unknown');
    const dot = el('span', 'mode-dot');
    dot.style.backgroundColor = modeColor(leg.mode);
    badge.appendChild(dot);
    badge.appendChild(document.createTextNode(modeLabelForLeg(leg)));
    return badge;
  }

  function buildMetric(label, value) {
    const wrap = el('div', 'metric');
    wrap.appendChild(el('dt', 'metric-label', label));
    wrap.appendChild(el('dd', 'metric-value', value));
    return wrap;
  }

  function buildStep(step) {
    const item = el('li', 'step');
    item.appendChild(el('span', 'step-type', stepTypeLabel(step.type)));
    const text = isNonEmptyString(step.text) ? step.text : '';
    if (text) {
      item.appendChild(el('span', 'step-text', text));
    }
    const parts = [];
    if (parseNumber(step.distance_m) !== null && Number(step.distance_m) > 0) {
      parts.push(formatDistance(step.distance_m));
    }
    if (parseNumber(step.duration_s) !== null && Number(step.duration_s) > 0) {
      parts.push(formatDuration(step.duration_s));
    }
    if (parts.length) {
      item.appendChild(el('span', 'step-meta', parts.join(' · ')));
    }
    return item;
  }

  function endpointName(leg, side, data, pointIndex) {
    const endpoint = leg && leg[side] && typeof leg[side] === 'object' ? leg[side] : null;
    if (endpoint && isNonEmptyString(endpoint.name)) {
      return endpoint.name;
    }
    const point = data && Array.isArray(data.points) ? data.points[pointIndex] : null;
    if (point && isNonEmptyString(point.name)) {
      return point.name;
    }
    if (point && isNonEmptyString(point.input)) {
      return point.input;
    }
    if (point && point.location && parseNumber(point.location.lng) !== null && parseNumber(point.location.lat) !== null) {
      return Number(point.location.lng).toFixed(5) + ',' + Number(point.location.lat).toFixed(5);
    }
    return side === 'from' ? '起点' : '终点';
  }

  // ===== 路线示意图（SVG）=====
  function renderRoute(legs) {
    clear(dom.routeCanvas);
    clear(dom.routeLegend);

    const segments = [];
    legs.forEach(function (leg, index) {
      if (!leg || typeof leg !== 'object') {
        return;
      }
      const points = Array.isArray(leg.polyline)
        ? leg.polyline.filter(isLngLatPair).map(function (pair) {
            return [Number(pair[0]), Number(pair[1])];
          })
        : [];
      if (points.length >= 2) {
        segments.push({ leg: leg, index: index, points: points });
      }
    });

    if (!segments.length) {
      dom.routeSection.hidden = true;
      return;
    }
    dom.routeSection.hidden = false;

    const width = 760;
    const height = 380;
    const padding = 34;
    const innerWidth = width - padding * 2;
    const innerHeight = height - padding * 2;

    const allPoints = [];
    segments.forEach(function (segment) {
      segment.points.forEach(function (point) {
        allPoints.push(point);
      });
    });

    let sumLat = 0;
    allPoints.forEach(function (point) {
      sumLat += point[1];
    });
    const meanLat = sumLat / allPoints.length;
    const cosLat = Math.cos((meanLat * Math.PI) / 180) || 1;

    let minX = Infinity;
    let maxX = -Infinity;
    let minY = Infinity;
    let maxY = -Infinity;
    allPoints.forEach(function (point) {
      const x = point[0] * cosLat;
      const y = -point[1];
      if (x < minX) {
        minX = x;
      }
      if (x > maxX) {
        maxX = x;
      }
      if (y < minY) {
        minY = y;
      }
      if (y > maxY) {
        maxY = y;
      }
    });

    const spanX = maxX - minX;
    const spanY = maxY - minY;
    let scale;
    if (spanX < 1e-9 && spanY < 1e-9) {
      scale = 0;
    } else if (spanX < 1e-9) {
      scale = innerHeight / spanY;
    } else if (spanY < 1e-9) {
      scale = innerWidth / spanX;
    } else {
      scale = Math.min(innerWidth / spanX, innerHeight / spanY);
    }
    const offsetX = (width - spanX * scale) / 2;
    const offsetY = (height - spanY * scale) / 2;

    function project(point) {
      return {
        x: offsetX + (point[0] * cosLat - minX) * scale,
        y: offsetY + (-point[1] - minY) * scale
      };
    }

    const svg = svgEl('svg', {
      viewBox: '0 0 ' + width + ' ' + height,
      class: 'route-svg',
      role: 'img',
      'aria-label': '路线示意图，共 ' + segments.length + ' 段',
      preserveAspectRatio: 'xMidYMid meet'
    });

    segments.forEach(function (segment) {
      const projected = segment.points.map(project);
      const polyline = svgEl('polyline', {
        points: projected.map(function (point) {
          return point.x.toFixed(2) + ',' + point.y.toFixed(2);
        }).join(' '),
        fill: 'none',
        stroke: modeColor(segment.leg.mode),
        'stroke-width': '4',
        'stroke-linecap': 'round',
        'stroke-linejoin': 'round',
        'data-mode': isNonEmptyString(segment.leg.mode) ? segment.leg.mode : 'unknown'
      });
      svg.appendChild(polyline);
    });

    segments.forEach(function (segment, index) {
      if (index === 0) {
        return;
      }
      const position = project(segment.points[0]);
      svg.appendChild(svgEl('circle', {
        cx: position.x.toFixed(2),
        cy: position.y.toFixed(2),
        r: '5',
        class: 'waypoint-circle'
      }));
    });

    const firstSegment = segments[0];
    const lastSegment = segments[segments.length - 1];
    const startPosition = project(firstSegment.points[0]);
    const endPosition = project(lastSegment.points[lastSegment.points.length - 1]);
    svg.appendChild(buildSvgMarker(startPosition, '起', 'marker-start'));
    svg.appendChild(buildSvgMarker(endPosition, '终', 'marker-end'));

    dom.routeCanvas.appendChild(svg);

    segments.forEach(function (segment) {
      const item = el('li', 'route-legend-item');
      const line = el('span', 'legend-line');
      line.style.backgroundColor = modeColor(segment.leg.mode);
      item.appendChild(line);
      item.appendChild(
        document.createTextNode('第 ' + (segment.index + 1) + ' 段 · ' + modeLabelForLeg(segment.leg))
      );
      dom.routeLegend.appendChild(item);
    });
  }

  function buildSvgMarker(position, text, className) {
    const group = svgEl('g', {
      class: className + ' marker',
      transform: 'translate(' + position.x.toFixed(2) + ',' + position.y.toFixed(2) + ')'
    });
    group.appendChild(svgEl('circle', { r: '11', class: 'marker-circle' }));
    const label = svgEl('text', {
      'text-anchor': 'middle',
      'dominant-baseline': 'central',
      class: 'marker-text'
    });
    label.textContent = text;
    group.appendChild(label);
    return group;
  }

  // ===== 事件绑定 / 初始化 =====
  function bindEvents() {
    dom.themeButtons.forEach(function (button) {
      button.addEventListener('click', function () {
        applyTheme(button.getAttribute('data-theme-choice'), true);
      });
    });

    dom.planForm.addEventListener('submit', function (event) {
      event.preventDefault();
      handleSubmit();
    });

    dom.addPoint.addEventListener('click', addWaypoint);

    dom.pointsList.addEventListener('input', function (event) {
      const target = event.target;
      if (!target || typeof target.getAttribute !== 'function') {
        return;
      }
      // 注意：select 也会触发 input 事件，必须用属性是否存在判断，避免 Number(null) === 0 误伤起点。
      const rawIndex = target.getAttribute('data-point-index');
      if (rawIndex === null) {
        return;
      }
      const index = Number(rawIndex);
      if (!Number.isInteger(index) || index < 0 || index >= state.points.length) {
        return;
      }
      state.points[index] = target.value;
      target.classList.remove('is-invalid');
      updateSegmentLabels();
    });

    dom.pointsList.addEventListener('change', function (event) {
      const target = event.target;
      if (!target || typeof target.getAttribute !== 'function') {
        return;
      }
      // 地点输入框失焦时也会触发 change，必须排除非交通方式控件。
      const rawIndex = target.getAttribute('data-mode-index');
      if (rawIndex === null) {
        return;
      }
      const index = Number(rawIndex);
      if (!Number.isInteger(index) || index < 0 || index >= state.modes.length) {
        return;
      }
      state.modes[index] = target.value;
    });

    dom.pointsList.addEventListener('click', function (event) {
      const target = event.target;
      if (!target || typeof target.closest !== 'function') {
        return;
      }
      const button = target.closest('[data-remove-index]');
      if (!button) {
        return;
      }
      event.preventDefault();
      const index = Number(button.getAttribute('data-remove-index'));
      if (Number.isInteger(index)) {
        removeWaypoint(index);
      }
    });

    [dom.fuelPrice, dom.fuelConsumption, dom.transitStrategy].forEach(function (input) {
      input.addEventListener('input', function () {
        state.settingsTouched = true;
      });
      input.addEventListener('change', function () {
        state.settingsTouched = true;
      });
    });

    dom.resetOptions.addEventListener('click', function () {
      state.settingsTouched = false;
      applyDefaults();
    });

    dom.alertRetry.addEventListener('click', function () {
      handleSubmit();
    });
    dom.alertDismiss.addEventListener('click', clearAlert);
    dom.statusRetry.addEventListener('click', handleStatusRetry);
  }

  function init() {
    cacheDom();
    renderTransitStrategies();
    initTheme();
    renderExamples();
    applyDefaults();
    renderForm();
    bindEvents();
    setLoading(false);
    loadStatus();
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init, { once: true });
  } else {
    init();
  }
})();

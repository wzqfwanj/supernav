'use strict';

/**
 * SuperNav · 交通规划 — 前端逻辑
 * 纯原生 JS（无框架、无构建、无外部依赖）。
 * 所有接口字段与 docs/API-CONTRACT.md 保持一致；费用一律以后端返回为准，前端只做展示。
 *
 * 两种规划模式：
 * - 方式对比（默认）：POST /api/compare，一次对比 5 种方式的路线、耗时与费用；
 * - 组合行程：POST /api/plan，逐段选择交通方式后汇总。
 */
(function () {
  // ===== 常量 =====
  const THEME_KEY = 'supernav-theme';
  const SAVED_LOCATION_KEY = 'supernav-saved-location';
  const THEME_VALUES = ['light', 'gray', 'dark'];
  const MIN_POINTS = 2;
  const MAX_POINTS = 10;
  const DEFAULT_MODE = 'walking';
  const PLAN_MODE_COMPARE = 'compare';
  const PLAN_MODE_COMBINE = 'combine';
  const PLAN_TIMEOUT_MS = 45000;
  const COMPARE_TIMEOUT_MS = 40000;
  // 方式卡展示顺序：公交/地铁 → 驾车 → 打车 → 骑行 → 步行
  // （契约 /api/compare 的 modes 数组顺序为 walking、bicycling、transit、taxi、driving，前端按交互要求重排展示）
  const COMPARE_ORDER = ['transit', 'driving', 'taxi', 'bicycling', 'walking'];

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
    pointLocations: [null, null],
    modes: [DEFAULT_MODE],
    modesMeta: FALLBACK_MODES.map(copyMode),
    defaults: Object.assign({}, FALLBACK_DEFAULTS),
    planMode: PLAN_MODE_COMPARE,
    resultKind: null,
    settingsTouched: false,
    statusLoaded: false,
    status: null,
    loading: false,
    lastResult: null,
    lastCompare: null,
    compareEntries: [],
    selectedCompareMode: null
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

  // ===== 上次定位：浏览器本地保存，刷新后自动恢复 =====
  function saveSavedLocation(location) {
    safeSetItem(SAVED_LOCATION_KEY, JSON.stringify(location));
  }

  function loadSavedLocation() {
    const raw = safeGetItem(SAVED_LOCATION_KEY);
    if (!raw) {
      return null;
    }
    try {
      const data = JSON.parse(raw);
      const lng = parseNumber(data && data.lng);
      const lat = parseNumber(data && data.lat);
      if (lng === null || lat === null) {
        return null;
      }
      if (lng < -180 || lng > 180 || lat < -90 || lat > 90) {
        return null;
      }
      return {
        lng: lng,
        lat: lat,
        address: isNonEmptyString(data.address) ? data.address.trim() : '',
        updatedAt: parseNumber(data.updatedAt) || 0
      };
    } catch (error) {
      return null;
    }
  }

  function restoreSavedLocation() {
    const saved = loadSavedLocation();
    if (!saved) {
      return;
    }
    state.pointLocations[0] = { lng: saved.lng, lat: saved.lat, address: saved.address };
    state.points[0] =
      saved.address ||
      '当前位置（' + saved.lng.toFixed(6) + ', ' + saved.lat.toFixed(6) + '）';
  }

  function locateFallbackLabel(lng, lat) {
    return '当前位置（' + lng.toFixed(6) + ', ' + lat.toFixed(6) + '）';
  }

  async function fetchRegeoAddress(lng, lat) {
    const controller = typeof AbortController === 'function' ? new AbortController() : null;
    const timer = controller
      ? window.setTimeout(function () {
          controller.abort();
        }, 10000)
      : null;
    try {
      const response = await fetch(
        'api/regeo?location=' + encodeURIComponent(lng.toFixed(6) + ',' + lat.toFixed(6)),
        { headers: { Accept: 'application/json' }, signal: controller ? controller.signal : undefined }
      );
      if (!response.ok) {
        return '';
      }
      const data = await response.json();
      return isNonEmptyString(data.formatted_address) ? data.formatted_address.trim() : '';
    } catch (error) {
      return '';
    } finally {
      if (timer !== null) {
        window.clearTimeout(timer);
      }
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
      plannerSub: 'planner-sub',
      modeSwitchHint: 'mode-switch-hint',
      requestStatus: 'request-status',
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
      compareSection: 'compare-section',
      compareGrid: 'compare-grid',
      modeError: 'mode-error',
      modeErrorMessage: 'mode-error-message',
      modeErrorHint: 'mode-error-hint',
      detail: 'detail',
      detailMode: 'detail-mode',
      detailSummary: 'detail-summary',
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
    dom.planModeButtons = Array.prototype.slice.call(document.querySelectorAll('[data-plan-mode]'));
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

  // ===== 规划模式（方式对比 / 组合行程）=====
  function setPlanMode(mode) {
    const next = mode === PLAN_MODE_COMBINE ? PLAN_MODE_COMBINE : PLAN_MODE_COMPARE;
    if (state.planMode === next || state.loading) {
      return;
    }
    state.planMode = next;
    updatePlanModeUI();
    renderForm();
    clearAlert();
    hideResult();
  }

  function updatePlanModeUI() {
    const isCompare = state.planMode === PLAN_MODE_COMPARE;
    dom.planModeButtons.forEach(function (button) {
      const active = button.getAttribute('data-plan-mode') === state.planMode;
      button.setAttribute('aria-pressed', active ? 'true' : 'false');
    });
    dom.plannerSub.textContent = isCompare
      ? '一次对比步行、骑行、公交/地铁、打车、驾车 5 种方式的路线、耗时与费用；也可切换到「组合行程」逐段搭配。'
      : '为每一段单独选择交通方式，按组合计算全程合计；也可切换回「方式对比」。';
    dom.modeSwitchHint.textContent = isCompare
      ? '输入起点与终点（可加途经点）→「开始规划」一次对比 5 种方式 → 点击卡片查看详情。'
      : '为每段选择交通方式 →「开始规划」生成一份组合行程详情。';
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
      if (state.planMode === PLAN_MODE_COMBINE && index < state.points.length - 1) {
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

    const located = state.pointLocations[index] || null;
    const input = el('input', 'point-input');
    input.type = 'text';
    input.value = address;
    input.id = 'point-input-' + index;
    input.setAttribute('data-point-index', String(index));
    input.setAttribute('aria-label', pointKindLabel(index) + '地址' + (located ? '（已使用浏览器定位）' : ''));
    input.setAttribute('autocomplete', 'off');
    input.setAttribute('spellcheck', 'false');
    if (located) {
      input.classList.add('is-located');
      input.title =
        '已定位并保存：' +
        (located.address || located.lng.toFixed(6) + ',' + located.lat.toFixed(6));
    }
    if (pointKind(index) === 'start') {
      input.setAttribute('placeholder', '起点地址，例如：北京南站（也可点「定位」）');
    } else if (pointKind(index) === 'end') {
      input.setAttribute('placeholder', '终点地址，例如：首都机场');
    } else {
      input.setAttribute('placeholder', '途经点地址');
    }

    const actions = el('div', 'point-actions');
    if (pointKind(index) === 'start') {
      const locate = el('button', 'btn btn-small btn-locate', located ? '重新定位' : '定位');
      locate.type = 'button';
      locate.setAttribute('data-locate-index', String(index));
      locate.setAttribute('aria-label', '使用浏览器定位作为起点');
      locate.title = located ? '重新获取当前浏览器位置' : '使用浏览器定位作为起点';
      actions.appendChild(locate);
    }
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
    if (state.planMode === PLAN_MODE_COMPARE) {
      dom.pointsHint.textContent =
        '共 ' + count + ' 个地点、' + Math.max(0, count - 1) + ' 段；最多 ' + MAX_POINTS +
        ' 个地点。点击「开始规划」将一次对比 5 种交通方式。';
      return;
    }
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
    state.pointLocations.splice(insertIndex, 0, null);
    state.modes.splice(insertIndex, 0, DEFAULT_MODE);
    renderForm();
    focusPointInput(insertIndex);
  }

  function removeWaypoint(index) {
    if (index <= 0 || index >= state.points.length - 1) {
      return;
    }
    state.points.splice(index, 1);
    state.pointLocations.splice(index, 1);
    state.modes.splice(index, 1);
    renderForm();
    focusPointInput(Math.min(index, state.points.length - 2));
  }

  function locateErrorMessage(error) {
    const code = error && error.code;
    if (code === 1) {
      return {
        message: '浏览器定位权限被拒绝。',
        hint: '请在浏览器地址栏的权限设置中允许本站定位，然后重试；也可以直接输入起点地址。'
      };
    }
    if (code === 2) {
      return {
        message: '暂时无法获取当前位置。',
        hint: '请检查设备定位服务是否开启（或网络是否可用），也可以直接输入起点地址。'
      };
    }
    if (code === 3) {
      return {
        message: '定位超时。',
        hint: '请重试，或直接输入起点地址。'
      };
    }
    return {
      message: '定位失败。',
      hint: '请检查浏览器定位权限后重试，或直接输入起点地址。'
    };
  }

  function setLocateButtonBusy(index, busy) {
    const button = dom.pointsList.querySelector('[data-locate-index="' + index + '"]');
    if (!button) {
      return;
    }
    button.disabled = busy;
    if (busy) {
      button.textContent = '定位中…';
    } else {
      button.textContent = state.pointLocations[index] ? '重新定位' : '定位';
    }
  }

  function handleLocate(index) {
    if (state.loading) {
      return;
    }
    if (!window.isSecureContext) {
      showAlert({
        message: '浏览器定位需要 HTTPS 环境。',
        hint: '请通过 https 地址访问本站（本地可用 http://127.0.0.1 / localhost），或直接输入起点地址。'
      });
      return;
    }
    if (!navigator.geolocation || typeof navigator.geolocation.getCurrentPosition !== 'function') {
      showAlert({
        message: '当前浏览器不支持定位。',
        hint: '请直接输入起点地址。'
      });
      return;
    }
    setLocateButtonBusy(index, true);
    const finish = function () {
      setLocateButtonBusy(index, false);
    };
    try {
      navigator.geolocation.getCurrentPosition(
        function (position) {
          const lng = Number(position.coords.longitude);
          const lat = Number(position.coords.latitude);
          if (!Number.isFinite(lng) || !Number.isFinite(lat)) {
            finish();
            showAlert({ message: '定位结果无效。', hint: '请重试或直接输入起点地址。' });
            return;
          }
          const located = {
            lng: Number(lng.toFixed(6)),
            lat: Number(lat.toFixed(6)),
            address: ''
          };
          state.pointLocations[index] = located;
          // 保持「定位中…」，等逆地理编码拿到详细地址后再回显并保存
          fetchRegeoAddress(located.lng, located.lat).then(function (address) {
            // 用户可能在等待期间手动修改了起点，此时不要覆盖
            if (state.pointLocations[index] !== located) {
              return;
            }
            const detail = address || locateFallbackLabel(located.lng, located.lat);
            located.address = detail;
            state.points[index] = detail;
            saveSavedLocation({
              lng: located.lng,
              lat: located.lat,
              address: detail,
              updatedAt: Date.now()
            });
            setLocateButtonBusy(index, false);
            renderForm();
            focusPointInput(index);
          });
        },
        function (error) {
          finish();
          showAlert(locateErrorMessage(error));
        },
        { enableHighAccuracy: true, timeout: 10000, maximumAge: 60000 }
      );
    } catch (error) {
      finish();
      showAlert(locateErrorMessage(error));
    }
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
    state.pointLocations = example.points.map(function () {
      return null;
    });
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
      '无法连接后端服务（api/status）。请先在项目根目录启动后端：python -m uvicorn backend.app:app --host 127.0.0.1 --port 8000，然后点击「重试连接」。';
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
      const response = await fetch('api/status', {
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

    if (state.planMode === PLAN_MODE_COMBINE) {
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
    }
    return null;
  }

  function buildPointsPayload() {
    return state.points.map(function (address, index) {
      const located = state.pointLocations[index] || null;
      if (located) {
        return {
          location: located.lng.toFixed(6) + ',' + located.lat.toFixed(6),
          name: String(address || '').trim() || '我的位置'
        };
      }
      return { address: String(address || '').trim() };
    });
  }

  function buildPlanPayload() {
    normalizeModes();
    return {
      points: buildPointsPayload(),
      modes: state.modes.slice(),
      options: buildOptions()
    };
  }

  function buildComparePayload() {
    return {
      points: buildPointsPayload(),
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

  // ===== 接口请求 =====
  async function requestEndpoint(path, payload, timeoutMs, hints) {
    const controller = typeof AbortController === 'function' ? new AbortController() : null;
    let timer = null;
    if (controller) {
      timer = window.setTimeout(function () {
        controller.abort();
      }, timeoutMs);
    }

    let response;
    try {
      response = await fetch(path, {
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
          '请求超时（超过 ' + Math.round(timeoutMs / 1000) + ' 秒）。',
          hints && hints.timeout
            ? hints.timeout
            : '后端可能正在等待高德接口，请稍后重试；若持续超时请检查后端日志。'
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
    return data;
  }

  async function requestPlan(payload) {
    const data = await requestEndpoint('api/plan', payload, PLAN_TIMEOUT_MS, {});
    if (!Array.isArray(data.legs) || !data.totals || typeof data.totals !== 'object') {
      throw new PlanError(
        '后端响应缺少 legs / totals 字段，与接口契约不符。',
        '请检查后端输出结构（docs/API-CONTRACT.md 第 3.3 节）。'
      );
    }
    return data;
  }

  async function requestCompare(payload) {
    const data = await requestEndpoint('api/compare', payload, COMPARE_TIMEOUT_MS, {
      timeout: '对比 5 种方式需要多次路径规划，可能较慢；请稍后重试，或检查后端日志。'
    });
    if (!Array.isArray(data.modes) || data.modes.length === 0) {
      throw new PlanError(
        '后端响应缺少 modes 数组，与 /api/compare 契约不符。',
        '请检查后端输出结构（docs/API-CONTRACT.md 第 3.4 节）。'
      );
    }
    return data;
  }

  // ===== 提交 / loading =====
  function setLoading(loading) {
    state.loading = loading;
    const isCompare = state.planMode === PLAN_MODE_COMPARE;
    dom.submitBtn.disabled = loading;
    dom.submitBtn.classList.toggle('is-loading', loading);
    dom.submitBtn.setAttribute('aria-busy', loading ? 'true' : 'false');
    dom.submitLabel.textContent = loading
      ? (isCompare ? '正在对比 5 种方式…' : '规划中…')
      : '开始规划';
    dom.planForm.classList.toggle('is-loading', loading);
    dom.result.classList.toggle('is-loading', loading);
    dom.planModeButtons.forEach(function (button) {
      button.disabled = loading;
    });
    if (dom.requestStatus) {
      dom.requestStatus.textContent = loading
        ? (isCompare ? '正在对比 5 种方式，请稍候…' : '正在规划，请稍候…')
        : '';
      dom.requestStatus.hidden = !loading;
    }
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
        hint: validation.hint || (state.planMode === PLAN_MODE_COMPARE
          ? '请检查地点后重试。'
          : '请检查地点与每段的交通方式后重试。')
      });
      if (typeof validation.focusIndex === 'number') {
        focusPointInput(validation.focusIndex);
      }
      return;
    }

    const isCompare = state.planMode === PLAN_MODE_COMPARE;
    setLoading(true);
    try {
      if (isCompare) {
        const compareData = await requestCompare(buildComparePayload());
        renderCompareResult(compareData);
      } else {
        const planData = await requestPlan(buildPlanPayload());
        renderPlanResult(planData);
      }
    } catch (error) {
      hideResult();
      if (error instanceof PlanError) {
        showAlert({ message: error.message, hint: error.hint });
      } else {
        showAlert({
          message: (isCompare ? '方式对比失败：' : '规划失败：') + '发生了未预期的错误。',
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
    state.resultKind = null;
    state.compareEntries = [];
    state.selectedCompareMode = null;
    state.lastCompare = null;
  }

  function scrollResultIntoView(wasHidden) {
    if (!wasHidden) {
      return;
    }
    try {
      dom.result.scrollIntoView({ behavior: 'smooth', block: 'start' });
    } catch (error) {
      dom.result.scrollIntoView();
    }
  }

  function hideDetailModeHeader() {
    dom.detailMode.hidden = true;
    clear(dom.detailMode);
    dom.detailMode.removeAttribute('data-mode');
    dom.detailSummary.textContent = '';
    dom.detailSummary.hidden = true;
  }

  function renderPlanResult(data) {
    state.lastResult = data;
    state.resultKind = 'plan';
    state.compareEntries = [];
    state.selectedCompareMode = null;
    state.lastCompare = null;
    const wasHidden = dom.result.hidden;
    dom.compareSection.hidden = true;
    dom.modeError.hidden = true;
    dom.detail.hidden = false;
    hideDetailModeHeader();
    renderTotals(data, true);
    renderLegs(data);
    renderRoute(Array.isArray(data.legs) ? data.legs : []);
    updateSourceBadge(data.mode);
    clearAlert();
    dom.result.hidden = false;
    dom.emptyState.hidden = true;
    scrollResultIntoView(wasHidden);
  }

  // ----- 方式对比 -----
  function zeroTotals() {
    return { distance_m: 0, duration_s: 0, cost: { total: 0, breakdown: [] } };
  }

  function normalizeCompareError(rawError, mode) {
    if (rawError && typeof rawError === 'object') {
      return {
        code: isNonEmptyString(rawError.code) ? rawError.code : '',
        message: isNonEmptyString(rawError.message) ? rawError.message : '该方式暂无可用路线。',
        hint: isNonEmptyString(rawError.hint) ? rawError.hint : '可切换到其他方式查看可用路线。'
      };
    }
    if (isNonEmptyString(rawError)) {
      return { code: '', message: rawError, hint: '可切换到其他方式查看可用路线。' };
    }
    return {
      code: '',
      message: '该方式暂无可用路线。',
      hint: modeLabel(mode) + ' 未能返回路线，可切换到其他方式查看。'
    };
  }

  function normalizeCompareEntry(raw, mode) {
    const entry = raw && typeof raw === 'object' ? raw : null;
    if (!entry) {
      return {
        mode: mode,
        mode_label: modeLabel(mode),
        available: false,
        legs: [],
        totals: zeroTotals(),
        summary: '',
        error: {
          code: '',
          message: '后端未返回该方式的结果。',
          hint: '请检查后端 /api/compare 响应是否包含全部 5 种方式（docs/API-CONTRACT.md 3.4）。'
        }
      };
    }
    let available = false;
    if (typeof entry.available === 'boolean') {
      available = entry.available;
    } else {
      available = Array.isArray(entry.legs);
    }
    const legs = available && Array.isArray(entry.legs) ? entry.legs : [];
    const totals = available && entry.totals && typeof entry.totals === 'object' ? entry.totals : zeroTotals();
    return {
      mode: mode,
      mode_label: isNonEmptyString(entry.mode_label) ? entry.mode_label : modeLabel(mode),
      available: available,
      legs: legs,
      totals: totals,
      summary: isNonEmptyString(entry.summary) ? entry.summary : '',
      error: available ? null : normalizeCompareError(entry.error, mode)
    };
  }

  function buildCompareEntries(data) {
    const rawModes = Array.isArray(data.modes) ? data.modes : [];
    const byMode = {};
    rawModes.forEach(function (entry) {
      if (entry && typeof entry === 'object' && isNonEmptyString(entry.mode)) {
        byMode[entry.mode] = entry;
      }
    });
    const order = COMPARE_ORDER.slice();
    rawModes.forEach(function (entry) {
      if (entry && typeof entry === 'object' && isNonEmptyString(entry.mode) && order.indexOf(entry.mode) < 0) {
        order.push(entry.mode);
      }
    });
    return order.map(function (mode) {
      return normalizeCompareEntry(byMode[mode], mode);
    });
  }

  function pickInitialCompareMode(entries) {
    for (let i = 0; i < COMPARE_ORDER.length; i += 1) {
      for (let j = 0; j < entries.length; j += 1) {
        if (entries[j].mode === COMPARE_ORDER[i] && entries[j].available) {
          return entries[j].mode;
        }
      }
    }
    return entries.length ? entries[0].mode : null;
  }

  function findCompareEntry(mode) {
    for (let i = 0; i < state.compareEntries.length; i += 1) {
      if (state.compareEntries[i].mode === mode) {
        return state.compareEntries[i];
      }
    }
    return null;
  }

  function buildCompareMetric(label, value) {
    const wrap = el('span', 'compare-metric');
    wrap.appendChild(el('span', 'compare-metric-label', label));
    wrap.appendChild(el('strong', 'compare-metric-value', value));
    return wrap;
  }

  function renderCompareCards() {
    clear(dom.compareGrid);
    state.compareEntries.forEach(function (entry) {
      const card = el('button', 'compare-item');
      card.type = 'button';
      card.setAttribute('data-compare-mode', entry.mode);
      card.setAttribute('aria-pressed', entry.mode === state.selectedCompareMode ? 'true' : 'false');
      card.title = entry.available
        ? '查看「' + entry.mode_label + '」的路线详情'
        : '点击查看「' + entry.mode_label + '」不可用的原因';
      if (!entry.available) {
        card.classList.add('is-unavailable');
      }
      if (entry.mode === state.selectedCompareMode) {
        card.classList.add('is-selected');
      }

      const head = el('span', 'compare-head');
      const dot = el('span', 'mode-dot');
      dot.style.backgroundColor = modeColor(entry.mode);
      head.appendChild(dot);
      head.appendChild(el('span', 'compare-name', entry.mode_label));
      if (!entry.available) {
        head.appendChild(el('span', 'compare-badge', '无可用路线'));
      }
      card.appendChild(head);

      if (entry.available) {
        const metrics = el('span', 'compare-metrics');
        metrics.appendChild(buildCompareMetric('耗时', formatDuration(entry.totals.duration_s)));
        metrics.appendChild(buildCompareMetric('里程', formatDistance(entry.totals.distance_m)));
        metrics.appendChild(buildCompareMetric('费用', formatMoney(entry.totals.cost && entry.totals.cost.total)));
        card.appendChild(metrics);
        if (isNonEmptyString(entry.summary)) {
          card.appendChild(el('span', 'compare-summary', entry.summary));
        }
      } else {
        card.appendChild(el('span', 'compare-unavailable', '暂无路线 · 点击查看原因'));
      }

      card.addEventListener('click', function () {
        selectCompareMode(entry.mode);
      });
      dom.compareGrid.appendChild(card);
    });
  }

  function updateCompareSelection() {
    const cards = dom.compareGrid.querySelectorAll('[data-compare-mode]');
    Array.prototype.forEach.call(cards, function (card) {
      const selected = card.getAttribute('data-compare-mode') === state.selectedCompareMode;
      card.classList.toggle('is-selected', selected);
      card.setAttribute('aria-pressed', selected ? 'true' : 'false');
    });
  }

  function renderCompareResult(data) {
    state.lastCompare = data;
    state.lastResult = null;
    state.resultKind = 'compare';
    state.compareEntries = buildCompareEntries(data);
    state.selectedCompareMode = null;
    const wasHidden = dom.result.hidden;
    dom.compareSection.hidden = false;
    dom.modeError.hidden = true;
    dom.detail.hidden = false;
    renderCompareCards();
    updateSourceBadge(data.mode);
    clearAlert();
    const initialMode = pickInitialCompareMode(state.compareEntries);
    if (initialMode) {
      selectCompareMode(initialMode);
    } else {
      dom.detail.hidden = true;
      showModeError(null);
    }
    dom.result.hidden = false;
    dom.emptyState.hidden = true;
    scrollResultIntoView(wasHidden);
  }

  function selectCompareMode(mode) {
    if (state.resultKind !== 'compare') {
      return;
    }
    const entry = findCompareEntry(mode) || state.compareEntries[0];
    if (!entry) {
      return;
    }
    state.selectedCompareMode = entry.mode;
    updateCompareSelection();
    if (!entry.available) {
      dom.detail.hidden = true;
      showModeError(entry);
      return;
    }
    dom.modeError.hidden = true;
    dom.detail.hidden = false;
    renderCompareDetail(entry);
  }

  function renderCompareDetail(entry) {
    const compareData = state.lastCompare || {};
    const detailData = {
      mode: compareData.mode,
      generated_at: compareData.generated_at,
      points: Array.isArray(compareData.points) ? compareData.points : [],
      legs: entry.legs,
      totals: entry.totals
    };

    dom.detailMode.hidden = false;
    clear(dom.detailMode);
    const dot = el('span', 'mode-dot');
    dot.style.backgroundColor = modeColor(entry.mode);
    dom.detailMode.appendChild(dot);
    dom.detailMode.appendChild(document.createTextNode(entry.mode_label));
    dom.detailMode.setAttribute('data-mode', entry.mode);

    if (isNonEmptyString(entry.summary)) {
      dom.detailSummary.textContent = entry.summary;
      dom.detailSummary.hidden = false;
    } else {
      dom.detailSummary.textContent = '';
      dom.detailSummary.hidden = true;
    }

    renderTotals(detailData, false);
    renderLegs(detailData);
    renderRoute(entry.legs);
  }

  function showModeError(entry) {
    if (!entry) {
      dom.modeErrorMessage.textContent = '没有可用的方式对比结果。';
      dom.modeErrorHint.textContent = '请重新规划，或检查后端 /api/compare 是否正常返回。';
      dom.modeErrorHint.hidden = false;
      dom.modeError.hidden = false;
      return;
    }
    const error = entry.error || {};
    dom.modeErrorMessage.textContent =
      '「' + entry.mode_label + '」' + (error.message || '该方式暂无可用路线。');
    const hintParts = [];
    if (error.hint) {
      hintParts.push(error.hint);
    }
    if (error.code) {
      hintParts.push('错误码：' + error.code);
    }
    const hint = hintParts.join('；');
    dom.modeErrorHint.textContent = hint;
    dom.modeErrorHint.hidden = !hint;
    dom.modeError.hidden = false;
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

  function renderTotals(data, showCombo) {
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

    if (showCombo === false) {
      clear(dom.totalsCombo);
      dom.totalsCombo.hidden = true;
    } else {
      renderCombo(Array.isArray(data.legs) ? data.legs : []);
    }

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
    const typeLabel = stepTypeLabel(step.type);
    item.appendChild(el('span', 'step-type', typeLabel));
    let text = isNonEmptyString(step.text) ? step.text : '';
    // 避免「步行 步行 500 米」这类重复：文本以类型名开头时去掉前缀
    if (typeLabel && text.indexOf(typeLabel) === 0) {
      text = text.slice(typeLabel.length).trim();
    }
    if (text) {
      item.appendChild(el('span', 'step-text', text));
    }
    const parts = [];
    const distanceText =
      parseNumber(step.distance_m) !== null && Number(step.distance_m) > 0
        ? formatDistance(step.distance_m)
        : '';
    // 文本里已经写了距离（如「步行 500 米至站点」）时，不重复显示距离
    if (distanceText && text.indexOf(distanceText) < 0) {
      parts.push(distanceText);
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

    dom.planModeButtons.forEach(function (button) {
      button.addEventListener('click', function () {
        setPlanMode(button.getAttribute('data-plan-mode'));
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
      if (state.pointLocations[index]) {
        // 用户手动修改起点文本后，不再使用浏览器定位坐标
        state.pointLocations[index] = null;
        target.classList.remove('is-located');
        target.removeAttribute('title');
        const locateButton = dom.pointsList.querySelector('[data-locate-index="' + index + '"]');
        if (locateButton) {
          locateButton.textContent = '定位';
          locateButton.title = '使用浏览器定位作为起点';
        }
        target.setAttribute('aria-label', pointKindLabel(index) + '地址');
      }
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
      const locateButton = target.closest('[data-locate-index]');
      if (locateButton) {
        event.preventDefault();
        const locateIndex = Number(locateButton.getAttribute('data-locate-index'));
        if (Number.isInteger(locateIndex)) {
          handleLocate(locateIndex);
        }
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
    updatePlanModeUI();
    renderExamples();
    applyDefaults();
    restoreSavedLocation();
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

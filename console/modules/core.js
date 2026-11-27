export const byId = id => document.getElementById(id);

export const STATUS_LABELS = {
  queued: '待送达',
  submitting: '发送中',
  cancel_requested: '取消中',
  accepted: '已入队',
  delivered: '已送达',
  unconfirmed: '未确认',
  running: '执行中',
  completed: '已完成',
  failed: '失败',
  interrupted: '中断',
  canceled: '已取消',
};

export const ROLES = {
  captain: 'Captain',
  worker: 'Worker',
  reviewer: 'Reviewer',
  helper: 'Helper',
  'reviewer+helper': 'Reviewer + Helper',
};

export const PRODUCTS = {
  antigravity: 'Antigravity',
  claude: 'Claude Code',
  codex: 'Codex',
  agy_acp: 'AGY ACP',
  agy_cli: 'AGY CLI',
};

export const ROLE_DOCS = [
  ['team_rules', '团队公共协作规则'],
  ['captain', 'Captain'],
  ['worker', 'Worker'],
  ['reviewer', 'Reviewer'],
  ['helper', 'Helper'],
];

export const REV_SOURCE = {
  hub: '管理台',
  external: '外部',
  rollback: '回档',
  initial: '基线',
};

export const THEME_KEY = 'team-hub-theme';
export const STATE_KEY = 'team-hub-console-token';

export function readLaunchToken() {
  const value = new URLSearchParams(location.hash.slice(1)).get('token');
  if (value) history.replaceState(null, '', location.pathname);
  return value;
}

export const session = {
  token: readLaunchToken() || sessionStorage.getItem(STATE_KEY) || '',
  pollSeq: 0,
  unlocked: false,
  snapshot: null,
  selectedTeamId: null,
  activeTab: 'members',
};

export function isPeerTeam(team) {
  return (team?.mode || 'structured') === 'peer';
}

export function applyTheme(theme, persist = true) {
  document.documentElement.dataset.theme = theme;
  const lbl = byId('theme-label');
  if (lbl) lbl.textContent = theme === 'dark' ? '浅色' : '深色';
  if (persist) localStorage.setItem(THEME_KEY, theme);
}

export function initTheme() {
  const saved = localStorage.getItem(THEME_KEY);
  const preferDark = window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches;
  applyTheme(saved === 'dark' || saved === 'light' ? saved : (preferDark ? 'dark' : 'light'), false);
}

export function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

export function fmtTime(ts) {
  if (!ts) return '';
  return new Date(ts * 1000).toLocaleString('zh-CN', { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hour12: false });
}

export function shortenPath(p, maxLen = 46) {
  if (!p || p.length <= maxLen) return p || '';
  const sep = p.includes('\\') ? '\\' : '/';
  const parts = p.split(sep).filter(Boolean);
  if (parts.length <= 2) return '…' + p.slice(-(maxLen - 1));
  const head = p.startsWith(sep) ? sep + parts[0] : parts[0];
  const tail = parts.slice(-2).join(sep);
  const combined = `${head}${sep}…${sep}${tail}`;
  return combined.length <= maxLen + 8 ? combined : `…${sep}${tail}`;
}

export function toast(text, type = 'ok') {
  const node = el('div', `toast toast-${type}`, text);
  byId('toast-root').append(node);
  setTimeout(() => node.remove(), 3400);
}

export function flashButtonDone(btn, doneText = '✓ 已保存') {
  const orig = btn.textContent;
  btn.textContent = doneText;
  setTimeout(() => { if (btn.textContent === doneText) btn.textContent = orig; }, 1400);
}

export function setSelectOptions(select, entries, currentValue) {
  const sig = JSON.stringify(entries);
  if (select.dataset.sig !== sig) {
    select.dataset.sig = sig;
    select.replaceChildren(...entries.map(([value, label]) => {
      const opt = el('option', '', label);
      opt.value = value;
      return opt;
    }));
  }
  if (currentValue !== undefined && select.value !== String(currentValue)) {
    select.value = String(currentValue);
  }
}

export function moveIndicator(container, activeEl, indicator) {
  if (!container || !activeEl || !indicator || activeEl.hidden) {
    if (indicator) indicator.style.width = '0px';
    return;
  }
  const cRect = container.getBoundingClientRect();
  const aRect = activeEl.getBoundingClientRect();
  indicator.style.width = `${aRect.width}px`;
  indicator.style.transform = `translateX(${aRect.left - cRect.left + container.scrollLeft}px)`;
}

export function syncIndicators() {
  const mainActive = document.querySelector('#main-tabs button[aria-selected="true"]');
  moveIndicator(byId('main-tabs'), mainActive, byId('main-tab-indicator'));
  const inspActive = document.querySelector('#inspector-tabs button[aria-selected="true"]');
  moveIndicator(byId('inspector-tabs'), inspActive, byId('inspector-indicator'));
}

export async function api(path, body) {
  const headers = { Authorization: `Bearer ${session.token}` };
  if (body !== undefined) headers['Content-Type'] = 'application/json';
  const res = await fetch(path, {
    method: body === undefined ? 'GET' : 'POST',
    headers,
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    const err = new Error(data.error || `请求失败（${res.status}）`);
    err.status = res.status;
    err.unauthorized = res.status === 401;
    throw err;
  }
  return data;
}

export function setUnlocked(state) {
  session.unlocked = state;
  byId('unlock').hidden = state;
  byId('dashboard').hidden = !state;
  byId('team-switcher').hidden = !state;
  byId('header-metrics').hidden = !state;
  byId('lock').hidden = !state;
  if (!state) byId('external-alert-bar').hidden = true;
}

export function setConnection(up) {
  const badge = byId('connection');
  if (!session.unlocked) {
    badge.dataset.state = 'locked';
    badge.textContent = '未解锁';
  } else {
    badge.dataset.state = up ? 'up' : 'down';
    badge.textContent = up ? '已连接' : '连接中断';
  }
}

export function lockConsole(message = '') {
  sessionStorage.removeItem(STATE_KEY);
  session.token = '';
  session.snapshot = null;
  setUnlocked(false);
  setConnection(false);
  byId('auth-error').textContent = message;
  byId('token').focus();
}

export function currentTeam() {
  return session.snapshot?.teams?.find(t => t.id === session.selectedTeamId) || session.snapshot?.teams?.[0] || null;
}

export function isPendingStatus(s) {
  return s === 'queued' || s === 'submitting' || s === 'cancel_requested';
}

export function isIssueStatus(s) {
  return s === 'unconfirmed' || s === 'failed' || s === 'canceled' || s === 'interrupted';
}

export function formatCommitSummary(commits) {
  return Object.entries(commits || {}).map(([k, v]) =>
    `${k}: ${v.status === 'committed' ? v.commit : v.reason || v.status}`).join(' · ');
}

export function handleError(err, inlineEl = null) {
  if (err.unauthorized) {
    lockConsole(err.message);
    return;
  }
  if (inlineEl) inlineEl.textContent = err.message;
  toast(err.message, 'error');
}

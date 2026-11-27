const byId = id => document.getElementById(id);

const STATUS_LABELS = {
  queued: '待送达',
  submitting: '发送中',
  accepted: '已送达',
  unconfirmed: '未确认',
  running: '执行中',
  completed: '已完成',
  failed: '失败',
  interrupted: '中断',
  canceled: '已取消',
};

const ROLES = {
  captain: 'Captain',
  worker: 'Worker',
  reviewer: 'Reviewer',
  helper: 'Helper',
  'reviewer+helper': 'Reviewer + Helper',
};

const PRODUCTS = {
  antigravity: 'Antigravity',
  claude: 'Claude Code',
  codex: 'Codex',
  agy_acp: 'AGY ACP',
  agy_cli: 'AGY CLI',
};

const ROLE_DOCS = [
  ['team_rules', '团队公共规则'],
  ['captain', 'Captain'],
  ['worker', 'Worker'],
  ['reviewer', 'Reviewer'],
  ['helper', 'Helper'],
];

const REV_SOURCE = {
  hub: '管理台',
  external: '外部',
  rollback: '回档',
  initial: '基线',
};

const THEME_KEY = 'team-hub-theme';
const STATE_KEY = 'team-hub-console-token';

let token = readLaunchToken() || sessionStorage.getItem(STATE_KEY) || '';
let pollSeq = 0;
let unlocked = false;
let snapshot = null;
let selectedTeamId = null;
let activeTab = 'members';
let msgFilter = 'all';
let msgMemberFilter = '';
let editingMemberId = null;
let bindingCtx = null;
let newTeamMode = 'structured';

function isPeerTeam(team) {
  return (team?.mode || 'structured') === 'peer';
}

// Docs Studio state
let activeDocKey = 'agents';     // 'agents' | 'team_rules' | 'captain' | 'worker' | 'reviewer' | 'helper'
let activeAgentsMode = 'shared'; // 'shared' | 'worktree'
let activeDocWorktree = '';
let activeInspector = 'history'; // 'history' | 'similar' | 'append' | 'sync'
let currentDocData = null;
let agentsOverview = null;
let activeDiffCtx = null; // { title, diff }
let diffScope = 'changes'; // 'changes' | 'full'
let similarThreshold = 0.55;
let similarActionMode = 'remove'; // 'remove' | 'replace'
let similarMatches = [];
let previewRevId = null;
let aiPreviewTeamId = null;
let aiPreviewWorktree = '';
let gitIdentityDirty = false;

/* Theme toggle (color tokens only, zero layout change) */
function initTheme() {
  const saved = localStorage.getItem(THEME_KEY);
  const preferDark = window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches;
  applyTheme(saved === 'dark' || saved === 'light' ? saved : (preferDark ? 'dark' : 'light'), false);
}

function applyTheme(theme, persist = true) {
  document.documentElement.dataset.theme = theme;
  const lbl = byId('theme-label');
  if (lbl) lbl.textContent = theme === 'dark' ? '浅色' : '深色';
  if (persist) localStorage.setItem(THEME_KEY, theme);
}

initTheme();

function readLaunchToken() {
  const value = new URLSearchParams(location.hash.slice(1)).get('token');
  if (value) history.replaceState(null, '', location.pathname);
  return value;
}

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

function fmtTime(ts) {
  if (!ts) return '';
  return new Date(ts * 1000).toLocaleString('zh-CN', { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hour12: false });
}

function shortenPath(p, maxLen = 46) {
  if (!p || p.length <= maxLen) return p || '';
  const sep = p.includes('\\') ? '\\' : '/';
  const parts = p.split(sep).filter(Boolean);
  if (parts.length <= 2) return '…' + p.slice(-(maxLen - 1));
  const head = p.startsWith(sep) ? sep + parts[0] : parts[0];
  const tail = parts.slice(-2).join(sep);
  const combined = `${head}${sep}…${sep}${tail}`;
  return combined.length <= maxLen + 8 ? combined : `…${sep}${tail}`;
}

function toast(text, type = 'ok') {
  const node = el('div', `toast toast-${type}`, text);
  byId('toast-root').append(node);
  setTimeout(() => node.remove(), 3400);
}

function flashButtonDone(btn, doneText = '✓ 已保存') {
  const orig = btn.textContent;
  btn.textContent = doneText;
  setTimeout(() => { if (btn.textContent === doneText) btn.textContent = orig; }, 1400);
}

function setSelectOptions(select, entries, currentValue) {
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

function moveIndicator(container, activeEl, indicator) {
  if (!container || !activeEl || !indicator || activeEl.hidden) {
    if (indicator) indicator.style.width = '0px';
    return;
  }
  const cRect = container.getBoundingClientRect();
  const aRect = activeEl.getBoundingClientRect();
  indicator.style.width = `${aRect.width}px`;
  indicator.style.transform = `translateX(${aRect.left - cRect.left + container.scrollLeft}px)`;
}

function syncIndicators() {
  const mainActive = document.querySelector('#main-tabs button[aria-selected="true"]');
  moveIndicator(byId('main-tabs'), mainActive, byId('main-tab-indicator'));
  const inspActive = document.querySelector('#inspector-tabs button[aria-selected="true"]');
  moveIndicator(byId('inspector-tabs'), inspActive, byId('inspector-indicator'));
}

async function api(path, body) {
  const headers = { Authorization: `Bearer ${token}` };
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

function setUnlocked(state) {
  unlocked = state;
  byId('unlock').hidden = state;
  byId('dashboard').hidden = !state;
  byId('team-switcher').hidden = !state;
  byId('header-metrics').hidden = !state;
  byId('lock').hidden = !state;
  if (!state) byId('external-alert-bar').hidden = true;
}

function setConnection(up) {
  const badge = byId('connection');
  if (!unlocked) {
    badge.dataset.state = 'locked';
    badge.textContent = '未解锁';
  } else {
    badge.dataset.state = up ? 'up' : 'down';
    badge.textContent = up ? '已连接' : '连接中断';
  }
}

function lockConsole(message = '') {
  sessionStorage.removeItem(STATE_KEY);
  token = '';
  snapshot = null;
  setUnlocked(false);
  setConnection(false);
  byId('auth-error').textContent = message;
  byId('token').focus();
}

function currentTeam() {
  return snapshot?.teams?.find(t => t.id === selectedTeamId) || snapshot?.teams?.[0] || null;
}

function isPendingStatus(s) { return s === 'queued' || s === 'submitting'; }
function isIssueStatus(s) { return s === 'unconfirmed' || s === 'failed' || s === 'canceled' || s === 'interrupted'; }

function formatCommitSummary(commits) {
  return Object.entries(commits || {}).map(([k, v]) =>
    `${k}: ${v.status === 'committed' ? v.commit : v.reason || v.status}`).join(' · ');
}

function switchTab(name) {
  const team = currentTeam();
  const target = (team && isPeerTeam(team) && (name === 'docs' || name === 'workspace')) ? 'members' : name;
  activeTab = target;
  for (const btn of document.querySelectorAll('#main-tabs [role="tab"]')) {
    btn.setAttribute('aria-selected', String(btn.dataset.tab === target));
  }
  for (const panel of document.querySelectorAll('.tab-panel')) {
    panel.hidden = panel.id !== `panel-${target}`;
  }
  requestAnimationFrame(syncIndicators);
  if (target === 'docs' && team && !isPeerTeam(team)) {
    loadDocsStudio();
  }
}

function switchInspector(name) {
  const isAgents = activeDocKey === 'agents';
  const target = (!isAgents && name !== 'history') ? 'history' : name;
  activeInspector = target;
  for (const btn of document.querySelectorAll('#inspector-tabs .seg-btn')) {
    const k = btn.dataset.insp;
    btn.hidden = (!isAgents && k !== 'history');
    btn.setAttribute('aria-selected', String(k === target));
  }
  for (const pane of document.querySelectorAll('.insp-pane')) {
    pane.hidden = pane.id !== `insp-${target}`;
  }
  requestAnimationFrame(syncIndicators);
}

/* External change top bar */
function renderExternalAlerts(alerts) {
  const bar = byId('external-alert-bar');
  const wrap = byId('external-alert-content');
  const sig = JSON.stringify(alerts || []);
  if (bar.dataset.sig === sig) return;
  bar.dataset.sig = sig;
  if (!alerts || !alerts.length) {
    bar.hidden = true;
    wrap.replaceChildren();
    return;
  }
  bar.hidden = false;
  const items = el('div', 'alert-bar-items');
  items.append(el('strong', '', '外部改动：'));
  for (const a of alerts) {
    const label = a.doc_key === 'agents'
      ? `${a.team_name} · ${a.target || '总工作区'}`
      : `${a.team_name} · ${a.doc_label}`;
    const btn = el('button', 'alert-jump', `${label} →`);
    btn.type = 'button';
    btn.addEventListener('click', () => {
      selectedTeamId = a.team_id;
      activeDocKey = a.doc_key;
      if (a.doc_key === 'agents') {
        activeAgentsMode = 'worktree';
        activeDocWorktree = a.target || '';
      }
      if (snapshot) render(snapshot, true);
      switchTab('docs');
      loadDocsStudio();
    });
    items.append(btn);
  }
  wrap.replaceChildren(items);
}

function renderSummary(team) {
  const peer = isPeerTeam(team);
  byId('active-team-name').textContent = team.name;
  const badge = byId('active-team-mode-badge');
  badge.hidden = false;
  badge.className = peer ? 'tag-soft tag-shared' : 'tag-soft';
  badge.textContent = peer ? '无分工 · 平级' : '分工 Team';

  const crumb = byId('active-team-path');
  crumb.textContent = peer
    ? '跨项目平级沟通 · 不注入团队身份'
    : (team.workspace ? shortenPath(team.workspace, 44) : '未配置工作区');
  crumb.title = peer ? '无分工 Team 不绑定工作区，不使用 Hook 注入身份' : (team.workspace || '');

  byId('tab-docs').hidden = peer;
  byId('tab-workspace').hidden = peer;
  byId('metric-hooks-sep').hidden = peer;
  byId('metric-hooks-wrap').hidden = peer;

  const members = team.members || [];
  const online = members.filter(m => m.online).length;
  const worktrees = team.worktrees || [];
  const synced = worktrees.filter(w => w.up_to_date).length;
  const pending = (team.activity || []).filter(a => isPendingStatus(a.status)).length;
  const docAlerts = (team.external_alerts || []).length;

  byId('stat-online').textContent = `${online}/${members.length}`;
  byId('stat-worktrees').textContent = `${synced}/${worktrees.length}`;
  byId('stat-queued').textContent = String(pending);

  byId('badge-members').textContent = String(members.length);
  byId('badge-worktrees').textContent = String(worktrees.length);
  const qBadge = byId('badge-queued');
  qBadge.textContent = String(pending);
  qBadge.hidden = pending === 0;
  const dBadge = byId('badge-doc-alerts');
  dBadge.textContent = String(docAlerts);
  dBadge.hidden = peer || docAlerts === 0;
}

function renderGlobalAccessPanel(team) {
  const peer = isPeerTeam(team);
  const panel = byId('peer-settings-panel');
  panel.hidden = !peer;
  if (!peer) return;
  const ga = snapshot?.global_access || {};
  const bar = byId('global-access-bar');
  const items = [
    ['Antigravity 插件', Boolean(ga.antigravity_plugin)],
    ['Claude 全局 MCP', Boolean(ga.claude_mcp)],
    ['Codex 全局 MCP', Boolean(ga.codex_mcp)],
    ['Codex 仅验票钩子', Boolean(ga.codex_ticket_hook)],
  ];
  bar.replaceChildren(...items.map(([label, ok]) => {
    const chip = el('span', 'hook-chip');
    chip.append(el('span', ok ? 'dot dot-ok' : 'dot dot-warn'), el('span', '', `${label} · ${ok ? '就绪' : '未配置'}`));
    return chip;
  }));
  const anyConfigured = Boolean(ga.claude_mcp || ga.codex_mcp || ga.codex_ticket_hook);
  byId('install-global-access').textContent = ga.ready ? '重新同步全局接入' : '配置全局接入';
  byId('remove-global-access').hidden = !anyConfigured;
}

/* Tab 1: Members (clean open flat rows) */
function renderMembers(team) {
  const peer = isPeerTeam(team);
  const list = byId('members-list');
  const members = team.members || [];
  byId('members-empty').hidden = members.length > 0;
  list.hidden = members.length === 0;
  renderGlobalAccessPanel(team);
  const sig = JSON.stringify({ peer, members });
  if (list.dataset.sig === sig) return;
  list.dataset.sig = sig;

  list.replaceChildren(...members.map(slot => {
    const li = el('li', 'member-row');
    li.dataset.role = peer ? 'peer' : (slot.role || 'worker').split('+')[0];

    const main = el('div', 'member-main');
    const top = el('div', 'member-top');
    const dotClass = slot.paused ? 'dot dot-danger'
      : slot.online ? 'dot dot-ok'
      : (slot.window_bound || slot.desktop_bound || slot.codex_bound || slot.acp_bound || slot.cli_bound) ? 'dot dot-warn' : 'dot';
    top.append(
      el('span', dotClass),
      el('span', 'member-title', slot.name),
    );
    if (!peer) {
      top.append(el('span', 'muted-label', `v${slot.role_revision || 1}`));
      if (slot.model && slot.model.toLowerCase().includes('gemini')) {
        top.append(el('span', 'tag-soft tag-shared', 'Gemini 把关'));
      }
    }

    const metaParts = peer
      ? [PRODUCTS[slot.product] || slot.product]
      : [
          PRODUCTS[slot.product] || slot.product,
          slot.model || null,
          slot.worktree || '项目内不限',
        ].filter(Boolean);
    const meta = el('div', 'member-meta', metaParts.join(' · '));
    const descText = peer ? (slot.note || slot.responsibility || '') : (slot.responsibility || '');
    const desc = el('p', 'member-desc', descText);
    main.append(top, meta);
    if (descText) main.append(desc);

    const controls = el('div', 'member-controls');
    if (!peer) {
      const roleSel = el('select', 'role-quick');
      roleSel.setAttribute('aria-label', `${slot.name} 职位`);
      setSelectOptions(roleSel, Object.entries(ROLES), slot.role || 'worker');
      roleSel.addEventListener('change', async () => {
        roleSel.disabled = true;
        try {
          await api('/control/team', {
            action: 'member', team_id: team.id, member: slot.member, name: slot.name,
            product: slot.product, role: roleSel.value, model: slot.model || '',
            worktree: slot.worktree || '', responsibility: slot.responsibility || '',
          });
          await refresh();
          toast(`${slot.name} → ${ROLES[roleSel.value]}`);
        } catch (err) {
          handleError(err);
          roleSel.value = slot.role || 'worker';
        } finally {
          roleSel.disabled = false;
        }
      });
      controls.append(roleSel);
    }

    const isBound = slot.codex_bound || slot.window_bound || slot.desktop_bound || slot.acp_bound || slot.cli_bound;
    const bindBtn = el('button', isBound ? 'btn-ghost btn-sm' : 'btn-tint btn-sm', isBound ? '换绑' : '绑定');
    bindBtn.type = 'button';
    bindBtn.addEventListener('click', () => openBindDialog(team, slot));

    const pauseBtn = el('button', 'btn-ghost btn-sm', slot.paused ? '恢复' : '暂停');
    pauseBtn.type = 'button';
    pauseBtn.addEventListener('click', async () => {
      pauseBtn.disabled = true;
      try {
        await api('/control/pause', { member: slot.member, paused: !slot.paused });
        await refresh();
        toast(slot.paused ? `${slot.name} 已恢复` : `${slot.name} 已暂停`, slot.paused ? 'ok' : 'warn');
      } catch (err) {
        handleError(err);
      } finally {
        pauseBtn.disabled = false;
      }
    });

    const editBtn = el('button', 'btn-ghost btn-sm', '编辑');
    editBtn.type = 'button';
    editBtn.addEventListener('click', () => openMemberDialog(team, slot));

    controls.append(bindBtn, pauseBtn, editBtn);
    li.append(main, controls);
    return li;
  }));
}

/* Tab 2: 3-Column Docs Studio */
function renderLeftRail(team) {
  const alerts = team.external_alerts || [];
  const alertWtSet = new Set(alerts.filter(a => a.doc_key === 'agents').map(a => a.target || ''));
  const alertRoleSet = new Set(alerts.filter(a => a.doc_key !== 'agents').map(a => a.doc_key));

  // Agents grouped tree (zero repeated group pills, zero duplicate branch names!)
  const tree = byId('agents-tree-list');
  const nodes = [];
  const sg = agentsOverview?.shared_group || {};
  const wts = agentsOverview?.worktrees || [];

  if (sg.count > 0) {
    const li = el('li');
    const btn = el('button', 'tree-item-btn tree-shared');
    btn.type = 'button';
    btn.setAttribute('aria-current', String(activeDocKey === 'agents' && activeAgentsMode === 'shared'));
    const main = el('span', 'tree-main');
    main.append(el('span', 'tree-title', '★ 主项目共享规范'));
    const right = el('span', 'tree-right');
    right.append(
      el('span', 'muted-label', `${sg.synced_count}/${sg.count}`),
      el('span', sg.drifted_count > 0 ? 'dot dot-warn' : 'dot dot-shared'),
    );
    btn.append(main, right);
    btn.addEventListener('click', () => {
      activeDocKey = 'agents';
      activeAgentsMode = 'shared';
      loadDocsStudio();
    });
    li.append(btn);
    nodes.push(li);
  }

  const groups = [
    ['shared', '共享组'],
    ['root', '容器根'],
    ['standalone', '独立'],
  ];
  for (const [gKey, gTitle] of groups) {
    const list = wts.filter(w => w.group === gKey);
    if (!list.length) continue;
    const head = el('li', 'rail-group-label');
    head.append(el('span', '', gTitle), el('span', '', String(list.length)));
    nodes.push(head);

    for (const wt of list) {
      const li = el('li');
      const btn = el('button', 'tree-item-btn');
      btn.type = 'button';
      const isCurrent = activeDocKey === 'agents' && activeAgentsMode === 'worktree' && activeDocWorktree === wt.relative;
      btn.setAttribute('aria-current', String(isCurrent));

      const main = el('span', 'tree-main');
      main.append(el('span', 'tree-title', wt.label));
      // Only show branch when it differs from directory name!
      if (wt.branch && wt.branch !== wt.relative) {
        main.append(el('span', 'tree-sub', wt.branch));
      }

      const right = el('span', 'tree-right');
      if (alertWtSet.has(wt.relative)) {
        right.append(el('span', 'status-txt-warn', '外改'), el('span', 'dot dot-warn'));
      } else if (!wt.exists) {
        right.append(el('span', 'status-txt-muted', '未建'), el('span', 'dot dot-hollow'));
      } else if (wt.group === 'shared') {
        if (wt.shared_status === 'drifted') {
          right.append(el('span', 'status-txt-warn', '漂移'), el('span', 'dot dot-warn'));
        } else {
          right.append(el('span', 'dot dot-ok'));
        }
      }
      btn.append(main, right);
      btn.addEventListener('click', () => {
        activeDocKey = 'agents';
        activeAgentsMode = 'worktree';
        activeDocWorktree = wt.relative;
        loadDocsStudio();
      });
      li.append(btn);
      nodes.push(li);
    }
  }
  tree.replaceChildren(...nodes);

  // Role & team rules list
  const roleList = byId('role-doc-list');
  roleList.replaceChildren(...ROLE_DOCS.map(([key, label]) => {
    const li = el('li');
    const btn = el('button', 'tree-item-btn');
    btn.type = 'button';
    btn.setAttribute('aria-current', String(activeDocKey === key));
    btn.append(el('span', 'tree-title', label));
    if (alertRoleSet.has(key)) {
      const r = el('span', 'tree-right');
      r.append(el('span', 'dot dot-warn'));
      btn.append(r);
    }
    btn.addEventListener('click', () => {
      activeDocKey = key;
      loadDocsStudio();
    });
    li.append(btn);
    return li;
  }));
}

function renderDocRevisions(revisions) {
  const list = byId('doc-revisions');
  byId('doc-rev-count').textContent = `${revisions.length} 条版本`;
  if (!revisions.length) {
    list.replaceChildren(el('li', 'muted-label', '暂无历史记录'));
    return;
  }
  list.replaceChildren(...revisions.map(rev => {
    const li = el('li', 'insp-row');
    const top = el('div', 'insp-row-top');
    const left = el('div', 'insp-row-left');
    left.append(
      el('strong', '', `#${rev.id}`),
      el('span', rev.source === 'external' ? 'tag-soft tag-warn' : 'tag-soft', REV_SOURCE[rev.source] || rev.source),
    );
    const right = el('div', 'insp-row-right');
    const viewBtn = el('button', 'btn-link', '预览');
    viewBtn.type = 'button';
    viewBtn.addEventListener('click', () => openRevisionPreview(rev.id));
    const rbBtn = el('button', 'btn-link', '回档');
    rbBtn.type = 'button';
    rbBtn.addEventListener('click', () => doRollback(rev.id));
    right.append(viewBtn, rbBtn);
    top.append(left, right);

    const sub = el('div', 'muted-label', `${rev.note || '无备注'} · ${fmtTime(rev.created_at)}`);
    li.append(top, sub);
    return li;
  }));
}

function rowPlainText(row) {
  return (row.segments || []).map(s => s.text).join('');
}

function triggerSimilarFromHunk(oldText, newText, mode) {
  if (mode === 'append') {
    byId('append-text-input').value = newText;
    switchInspector('append');
    runAppendPreview();
    return;
  }
  byId('similar-query-input').value = oldText;
  setSimilarActionMode(mode);
  if (mode === 'replace') {
    byId('similar-replace-input').value = newText;
  }
  switchInspector('similar');
  runSimilarSearch();
}

function renderInlineDiffView() {
  const viewer = byId('doc-diff-view');
  const ta = byId('doc-textarea');
  if (!activeDiffCtx || !activeDiffCtx.diff) {
    viewer.hidden = true;
    ta.hidden = false;
    return;
  }
  const { title, diff } = activeDiffCtx;
  viewer.hidden = false;
  ta.hidden = true;
  byId('diff-view-title').textContent = title;
  const stats = byId('diff-view-stats');
  stats.replaceChildren(
    el('span', 'diff-stat-add', `+${diff.added || 0}`),
    el('span', 'diff-stat-del', `-${diff.removed || 0}`),
  );

  const rows = diff.rows || [];
  let displayRows = rows;
  if (diffScope === 'changes') {
    const keep = new Set();
    rows.forEach((r, idx) => {
      if (r.type === 'del' || r.type === 'add') {
        for (let k = Math.max(0, idx - 3); k <= Math.min(rows.length - 1, idx + 3); k += 1) {
          keep.add(k);
        }
      }
    });
    displayRows = [];
    let skipped = 0;
    rows.forEach((r, idx) => {
      if (keep.has(idx)) {
        if (skipped > 0) {
          displayRows.push({ type: 'gap', skipped });
          skipped = 0;
        }
        displayRows.push(r);
      } else {
        skipped += 1;
      }
    });
    if (skipped > 0) displayRows.push({ type: 'gap', skipped });
  }

  const body = byId('doc-diff-body');
  if (!displayRows.length) {
    body.replaceChildren(el('div', 'diff-gap', '无文本差异'));
    return;
  }

  const isAgents = activeDocKey === 'agents';
  const rendered = [];
  for (let i = 0; i < displayRows.length; i += 1) {
    const r = displayRows[i];
    if (r.type === 'gap') {
      rendered.push(el('div', 'diff-gap', `⋯ 略过 ${r.skipped} 行未改动 ⋯`));
      continue;
    }
    // At the start of a contiguous change hunk (del/add), inject an inline cross-worktree sync bar for AGENTS.md.
    const isChange = r.type === 'del' || r.type === 'add';
    const prevIsChange = i > 0 && (displayRows[i - 1].type === 'del' || displayRows[i - 1].type === 'add');
    if (isAgents && isChange && !prevIsChange) {
      const delLines = [];
      const addLines = [];
      let j = i;
      while (j < displayRows.length && (displayRows[j].type === 'del' || displayRows[j].type === 'add')) {
        const txt = rowPlainText(displayRows[j]).trim();
        if (txt) {
          if (displayRows[j].type === 'del') delLines.push(txt);
          else addLines.push(txt);
        }
        j += 1;
      }
      if (delLines.length || addLines.length) {
        const bar = el('div', 'diff-hunk-bar');
        const label = delLines.length && addLines.length
          ? `变动块（-${delLines.length} / +${addLines.length} 行）`
          : delLines.length ? `删除块（-${delLines.length} 行）` : `新增块（+${addLines.length} 行）`;
        bar.append(el('span', '', label));
        const acts = el('div', 'diff-hunk-actions');
        if (delLines.length && addLines.length) {
          const oldStr = delLines.join('\n');
          const newStr = addLines.join('\n');
          const btn1 = el('button', 'diff-hunk-btn', '查其他树旧文并替换');
          btn1.type = 'button';
          btn1.addEventListener('click', () => triggerSimilarFromHunk(oldStr, newStr, 'replace'));
          const btn2 = el('button', 'btn-link', '反查新文');
          btn2.type = 'button';
          btn2.title = '如果红绿方向相反，以绿行作旧文去查其他树并替换为红行';
          btn2.addEventListener('click', () => triggerSimilarFromHunk(newStr, oldStr, 'replace'));
          acts.append(btn1, btn2);
        } else if (delLines.length) {
          const btn = el('button', 'diff-hunk-btn', '查其他树旧文并移除');
          btn.type = 'button';
          btn.addEventListener('click', () => triggerSimilarFromHunk(delLines.join('\n'), '', 'remove'));
          acts.append(btn);
        } else if (addLines.length) {
          const btn = el('button', 'diff-hunk-btn', '批量加到其他树');
          btn.type = 'button';
          btn.addEventListener('click', () => triggerSimilarFromHunk('', addLines.join('\n'), 'append'));
          acts.append(btn);
        }
        bar.append(acts);
        rendered.push(bar);
      }
    }

    const line = el('div', `diff-line diff-line-${r.type}`);
    line.append(el('span', 'diff-gutter', String(r.line_no || '')));
    const code = el('div', 'diff-code');
    const segs = r.segments || [];
    if (!segs.length || (segs.length === 1 && segs[0].text === '')) {
      code.textContent = ' ';
    } else {
      for (const seg of segs) {
        if (seg.type === 'emph' && (r.type === 'del' || r.type === 'add')) {
          code.append(el('span', `diff-emph-${r.type}`, seg.text));
        } else {
          code.append(document.createTextNode(seg.text));
        }
      }
    }
    line.append(code);
    rendered.push(line);
  }
  body.replaceChildren(...rendered);
}

function openInlineDiff(title, diff) {
  activeDiffCtx = { title, diff };
  renderInlineDiffView();
}

function closeInlineDiff() {
  activeDiffCtx = null;
  renderInlineDiffView();
}

function renderSyncInspector() {
  const sg = agentsOverview?.shared_group || {};
  const sharedWts = (agentsOverview?.worktrees || []).filter(w => w.group === 'shared');
  const isSharedMode = activeDocKey === 'agents' && activeAgentsMode === 'shared';

  byId('sync-summary').textContent = sg.count
    ? `共享组 ${sg.count} 棵 · ${sg.synced_count} 一致 / ${sg.drifted_count} 漂移`
    : '暂无共享组工作树';
  byId('sync-jump-shared').hidden = isSharedMode || !sg.count;
  byId('sync-footer').hidden = !isSharedMode;
  byId('sync-error').textContent = '';

  const matrix = byId('sync-wt-matrix');
  if (!sharedWts.length) {
    matrix.replaceChildren(el('p', 'muted-label', '未检测到共享组工作树'));
    return;
  }

  matrix.replaceChildren(...sharedWts.map(wt => {
    const row = el('div', 'insp-row');
    const top = el('div', 'insp-row-top');
    const left = el('div', 'insp-row-left');
    left.append(
      el('span', wt.shared_status === 'drifted' ? 'dot dot-warn' : 'dot dot-ok'),
      el('strong', '', wt.label),
    );
    if (wt.shared_status === 'drifted') {
      left.append(el('span', 'status-txt-warn', '漂移'));
    }

    const right = el('div', 'insp-row-right');
    if (wt.drift_inline) {
      const diffBtn = el('button', 'btn-link', '对比');
      diffBtn.type = 'button';
      diffBtn.addEventListener('click', () => {
        openInlineDiff(`漂移对比：基准 (${sg.canonical_worktree}) → ${wt.label}`, wt.drift_inline);
      });
      right.append(diffBtn);
    }

    if (isSharedMode) {
      const wLbl = el('label', 'check-inline');
      const wCb = el('input');
      wCb.type = 'checkbox';
      wCb.dataset.role = 'write';
      wCb.value = wt.relative;
      wCb.checked = true;
      wLbl.append(wCb, el('span', '', '写'));

      const cLbl = el('label', 'check-inline');
      const cCb = el('input');
      cCb.type = 'checkbox';
      cCb.dataset.role = 'commit';
      cCb.value = wt.relative;
      cCb.checked = false;
      cLbl.append(cCb, el('span', '', '提交'));

      wCb.addEventListener('change', () => {
        if (!wCb.checked) { cCb.checked = false; cCb.disabled = true; }
        else { cCb.disabled = false; }
      });
      cCb.addEventListener('change', () => { if (cCb.checked) wCb.checked = true; });
      right.append(wLbl, cLbl);
    }

    top.append(left, right);
    row.append(top);
    return row;
  }));
}

function syncAppendButton() {
  const sel = readInspMatrix(byId('append-wt-matrix'));
  const btn = byId('append-apply-btn');
  btn.disabled = sel.write.length === 0;
  btn.textContent = sel.write.length ? `添加到 ${sel.write.length} 棵` : '批量添加';
}

function renderAppendInspector(scope = 'shared') {
  const sections = agentsOverview?.all_sections || [];
  setSelectOptions(byId('append-section-select'), sections.map(s => [s, `## ${s}`]), sections[0] || '');
  const wts = agentsOverview?.worktrees || [];
  const matrix = byId('append-wt-matrix');
  matrix.replaceChildren(...wts.map(wt => {
    const pick = scope === 'shared' ? wt.group === 'shared' : wt.exists;
    return buildAppendRow({
      worktree: wt.relative,
      label: wt.label,
      sub: wt.exists ? '' : '将新建 AGENTS.md',
      badge: '',
      defaultWrite: pick,
      defaultCommit: false,
    });
  }));
  syncAppendButton();
}

function buildAppendRow(item) {
  const row = el('div', 'insp-row');
  const top = el('div', 'insp-row-top');
  const left = el('div', 'insp-row-left');
  left.append(el('strong', '', item.label));
  if (item.badge) left.append(el('span', item.badgeClass || 'tag-soft', item.badge));

  const right = el('div', 'insp-row-right');
  const wLbl = el('label', 'check-inline');
  const wCb = el('input');
  wCb.type = 'checkbox';
  wCb.dataset.role = 'write';
  wCb.value = item.worktree;
  wCb.checked = Boolean(item.defaultWrite);
  wLbl.append(wCb, el('span', '', '加'));

  const cLbl = el('label', 'check-inline');
  const cCb = el('input');
  cCb.type = 'checkbox';
  cCb.dataset.role = 'commit';
  cCb.value = item.worktree;
  cCb.checked = Boolean(item.defaultCommit && item.defaultWrite);
  cCb.disabled = !wCb.checked;
  cLbl.append(cCb, el('span', '', '提交'));

  wCb.addEventListener('change', () => {
    if (!wCb.checked) { cCb.checked = false; cCb.disabled = true; }
    else { cCb.disabled = false; }
    syncAppendButton();
  });
  cCb.addEventListener('change', () => {
    if (cCb.checked) wCb.checked = true;
    syncAppendButton();
  });

  right.append(wLbl, cLbl);
  top.append(left, right);
  row.append(top);
  if (item.sub) row.append(el('div', 'muted-label', item.sub));
  return row;
}

function readInspMatrix(container) {
  const write = Array.from(container.querySelectorAll('input[data-role="write"]:checked'), cb => cb.value);
  const commit = Array.from(container.querySelectorAll('input[data-role="commit"]:checked'), cb => cb.value)
    .filter(v => write.includes(v));
  return { write, commit };
}

async function loadDocsStudio() {
  const team = currentTeam();
  if (!team) return;
  byId('doc-error').textContent = '';
  try {
    const overview = await api('/control/doc', { team_id: team.id, action: 'agents_overview' });
    agentsOverview = overview;
    const sg = overview.shared_group || {};
    if (activeDocKey === 'agents' && activeAgentsMode === 'shared' && !sg.count) {
      activeAgentsMode = 'worktree';
      activeDocWorktree = overview.worktrees?.[0]?.relative || '';
    }

    const fetchTarget = activeDocKey === 'agents'
      ? (activeAgentsMode === 'shared' ? (sg.canonical_worktree || '') : activeDocWorktree)
      : '';

    const data = await api('/control/doc', {
      team_id: team.id,
      action: 'get',
      doc_key: activeDocKey,
      worktree: fetchTarget,
    });
    currentDocData = data;
    renderLeftRail(team);

    const ta = byId('doc-textarea');
    byId('doc-note').value = '';
    byId('selection-actions').hidden = true;
    closeInlineDiff();

    const showExternalBanner = Boolean(data.external_changed && !(activeDocKey === 'agents' && activeAgentsMode === 'shared'));
    byId('doc-external-banner').hidden = !showExternalBanner;
    if (showExternalBanner) {
      const ed = data.external_diff;
      byId('doc-external-text').textContent = ed
        ? `磁盘有外部改动（+${ed.added} / -${ed.removed} 行）`
        : '磁盘有外部改动';
      byId('doc-toggle-ext-diff').hidden = !ed;
    }

    if (activeDocKey === 'agents' && activeAgentsMode === 'shared') {
      byId('doc-editor-title').textContent = '主项目共享规范';
      byId('doc-editor-sub').textContent = `共享组 ${sg.count} 棵 · 基准 ${sg.canonical_worktree || '-'}`;
      byId('doc-path-hint').textContent = `同步到 ${sg.count} 棵共享组工作树`;
      ta.value = sg.canonical_shared || '';
      byId('doc-reset-btn').hidden = true;
      byId('doc-drift-banner').hidden = true;
      byId('doc-single-commit-wrap').hidden = true;
      byId('doc-save-btn').textContent = '同步保存';
      renderSyncInspector();
      renderAppendInspector('shared');
      renderDocRevisions(data.revisions || []);
      switchInspector(activeInspector === 'similar' || activeInspector === 'append' ? activeInspector : 'sync');
    } else if (activeDocKey === 'agents') {
      const wtInfo = (overview.worktrees || []).find(w => w.relative === activeDocWorktree);
      byId('doc-editor-title').textContent = wtInfo ? wtInfo.label : (activeDocWorktree || '总工作区');
      byId('doc-editor-sub').textContent = data.has_shared_markers
        ? `独立内容 · 已自动挂载共享规范 (${data.shared_line_count} 行)`
        : (wtInfo?.group_label || '独立工作树');
      byId('doc-path-hint').textContent = shortenPath(data.path, 36);
      byId('doc-path-hint').title = data.path;
      // Single clean textarea showing ONLY independent content when worktree belongs to shared group!
      ta.value = data.has_shared_markers ? (data.independent_content || '') : (data.content || '');
      byId('doc-reset-btn').hidden = true;
      const hasDrift = Boolean(wtInfo && wtInfo.shared_status === 'drifted' && wtInfo.drift_inline);
      byId('doc-drift-banner').hidden = !hasDrift;
      if (hasDrift) {
        const di = wtInfo.drift_inline;
        byId('doc-drift-text').textContent = `共享规范与基准（${sg.canonical_worktree}）有漂移（+${di.added} / -${di.removed} 行）`;
      }
      byId('doc-single-commit-wrap').hidden = false;
      byId('doc-single-commit').checked = false;
      byId('doc-save-btn').textContent = '保存';
      renderSyncInspector();
      renderAppendInspector(sg.count ? 'shared' : 'all');
      renderDocRevisions(data.revisions || []);
      switchInspector(activeInspector === 'sync' ? 'history' : activeInspector);
    } else {
      byId('doc-editor-title').textContent = data.label;
      byId('doc-editor-sub').textContent = data.is_override ? '项目覆盖' : 'Hub 默认';
      byId('doc-path-hint').textContent = shortenPath(data.path, 36);
      byId('doc-path-hint').title = data.path;
      ta.value = data.content || '';
      byId('doc-reset-btn').hidden = !data.has_default || !data.is_override;
      byId('doc-drift-banner').hidden = true;
      byId('doc-single-commit-wrap').hidden = true;
      byId('doc-save-btn').textContent = '保存';
      renderDocRevisions(data.revisions || []);
      switchInspector('history');
    }
  } catch (err) {
    handleError(err, byId('doc-error'));
  }
}

/* Selection floating actions */
function getEditorSelection() {
  const ta = byId('doc-textarea');
  if (!ta) return '';
  const s = ta.selectionStart || 0;
  const e = ta.selectionEnd || 0;
  return e > s ? ta.value.slice(s, e).trim() : '';
}

function updateSelectionBar() {
  if (activeDocKey !== 'agents') {
    byId('selection-actions').hidden = true;
    return;
  }
  const sel = getEditorSelection();
  byId('selection-actions').hidden = sel.length < 3;
}

/* Similar search in right inspector (zero modal backdrop!) */
function setSimilarActionMode(mode) {
  similarActionMode = mode === 'replace' ? 'replace' : 'remove';
  for (const btn of document.querySelectorAll('[data-sim-action]')) {
    btn.classList.toggle('active', btn.dataset.simAction === similarActionMode);
  }
  const isRep = similarActionMode === 'replace';
  byId('similar-replace-input').hidden = !isRep;
  byId('similar-swap-btn').hidden = !isRep;
  const actionBtn = byId('similar-remove-btn');
  actionBtn.classList.toggle('btn-primary', isRep);
  actionBtn.classList.toggle('btn-danger', !isRep);
  syncSimilarCommitChecks();
}

function syncSimilarCommitChecks() {
  const checkedIds = new Set(Array.from(byId('similar-results-list').querySelectorAll('input[data-match-id]:checked'), cb => cb.dataset.matchId));
  const selected = similarMatches.filter(m => checkedIds.has(m.id));
  const rmBtn = byId('similar-remove-btn');
  const verb = similarActionMode === 'replace' ? '替换' : '移除';
  rmBtn.disabled = selected.length === 0;
  rmBtn.textContent = selected.length ? `${verb} ${selected.length} 条` : verb;

  const commitBox = byId('similar-commit-box');
  const checks = byId('similar-commit-checks');
  const prev = new Set(Array.from(checks.querySelectorAll('input:checked'), cb => cb.value));
  const affected = new Map();
  for (const m of selected) {
    if (!affected.has(m.worktree)) affected.set(m.worktree, m.worktree_label);
  }
  commitBox.hidden = affected.size === 0;
  checks.replaceChildren(...Array.from(affected.entries()).map(([wt, label]) => {
    const lbl = el('label', 'check-inline');
    const cb = el('input');
    cb.type = 'checkbox';
    cb.value = wt;
    cb.checked = prev.has(wt);
    lbl.append(cb, el('span', '', label));
    return lbl;
  }));
}

function renderSimilarResults(data) {
  similarMatches = data.matches || [];
  const wts = new Set(similarMatches.map(m => m.worktree));
  byId('similar-summary').textContent = similarMatches.length
    ? `命中 ${similarMatches.length} 条 · ${wts.size} 棵工作树`
    : '无命中，可切换「宽松」重试';

  const list = byId('similar-results-list');
  if (!similarMatches.length) {
    list.replaceChildren(el('li', 'muted-label', '暂无匹配项'));
    syncSimilarCommitChecks();
    return;
  }

  list.replaceChildren(...similarMatches.map(m => {
    const li = el('li', 'insp-row');
    li.dataset.matchId = m.id;
    const top = el('div', 'insp-row-top');
    const left = el('div', 'insp-row-left');
    const lbl = el('label', 'check-inline');
    const cb = el('input');
    cb.type = 'checkbox';
    cb.dataset.matchId = m.id;
    cb.checked = Boolean(m.recommended);
    cb.addEventListener('change', syncSimilarCommitChecks);
    lbl.append(cb, el('strong', '', m.worktree_label));
    left.append(lbl, el('span', 'muted-label', `L${m.line_no}`));

    const scoreTag = el('span', m.score >= 0.70 ? 'tag-soft tag-ok' : 'tag-soft tag-warn', `${Math.round(m.score * 100)}%`);
    top.append(left, scoreTag);

    const snip = el('div', 'insp-snippet');
    for (const seg of m.diff_segments || [{ type: 'same', text: m.text }]) {
      if (seg.type === 'diff') snip.append(el('mark', 'diff-hit', seg.text));
      else snip.append(document.createTextNode(seg.text));
    }
    li.append(top, snip);
    return li;
  }));
  syncSimilarCommitChecks();
}

async function runSimilarSearch() {
  const team = currentTeam();
  if (!team) return;
  const query = byId('similar-query-input').value.trim();
  byId('similar-error').textContent = '';
  if (!query) {
    byId('similar-error').textContent = '请输入检索句';
    return;
  }
  const btn = byId('similar-run-btn');
  btn.disabled = true;
  try {
    const data = await api('/control/doc', {
      team_id: team.id,
      action: 'search_similar',
      query,
      min_score: similarThreshold,
      source_worktree: activeAgentsMode === 'worktree' ? activeDocWorktree : null,
    });
    renderSimilarResults(data);
  } catch (err) {
    handleError(err, byId('similar-error'));
  } finally {
    btn.disabled = false;
  }
}

async function runAppendPreview() {
  const team = currentTeam();
  if (!team) return;
  const text = byId('append-text-input').value.trim();
  byId('append-error').textContent = '';
  if (!text) {
    byId('append-error').textContent = '请输入要添加的文本';
    return;
  }
  const sel = readInspMatrix(byId('append-wt-matrix'));
  const commitSet = new Set(sel.commit);
  const targets = sel.write.length ? sel.write : (agentsOverview?.worktrees || []).filter(w => w.exists).map(w => w.relative);
  const btn = byId('append-preview-btn');
  btn.disabled = true;
  try {
    const res = await api('/control/doc', {
      team_id: team.id,
      action: 'preview_append',
      text,
      worktrees: targets,
      position: byId('append-position').value,
      section_heading: byId('append-section-select').value,
    });
    const matrix = byId('append-wt-matrix');
    matrix.replaceChildren(...(res.previews || []).map(p => buildAppendRow({
      worktree: p.worktree,
      label: p.worktree_label,
      badge: p.duplicate ? `已有 L${p.duplicate_line}` : '可加',
      badgeClass: p.duplicate ? 'tag-soft tag-warn' : 'tag-soft tag-ok',
      sub: p.duplicate ? `默认跳过：${p.duplicate_text.slice(0, 44)}` : p.resolved_position,
      defaultWrite: p.default_apply,
      defaultCommit: p.default_apply && commitSet.has(p.worktree),
    })));
    syncAppendButton();
    toast('已完成去重检测');
  } catch (err) {
    handleError(err, byId('append-error'));
  } finally {
    btn.disabled = false;
  }
}

async function openRevisionPreview(revId) {
  const team = currentTeam();
  if (!team) return;
  try {
    const rev = await api('/control/doc', { team_id: team.id, action: 'revision', revision_id: revId });
    previewRevId = rev.id;
    byId('rev-preview-title').textContent = `版本 #${rev.id}`;
    byId('rev-preview-meta').textContent = `${REV_SOURCE[rev.source] || rev.source} · ${fmtTime(rev.created_at)} · ${rev.note || '无备注'}`;
    byId('rev-preview-content').textContent = rev.content;
    byId('rev-preview-dialog').showModal();
  } catch (err) {
    handleError(err);
  }
}

async function doRollback(revId) {
  const team = currentTeam();
  if (!team) return;
  if (!confirm(`确认回档到版本 #${revId}？`)) return;
  try {
    const autoCommit = activeDocKey === 'agents' && activeAgentsMode === 'worktree' && byId('doc-single-commit').checked;
    await api('/control/doc', { team_id: team.id, action: 'rollback', revision_id: revId, auto_commit: autoCommit });
    byId('rev-preview-dialog').close();
    await refresh();
    await loadDocsStudio();
    toast(`已回档至 #${revId}`);
  } catch (err) {
    handleError(err, byId('doc-error'));
  }
}

/* Tab 3: Workspace */
function renderWorkspace(team, forceInputs = false) {
  if (forceInputs || document.activeElement !== byId('workspace-input')) {
    byId('workspace-input').value = team.workspace || '';
  }
  const hasSavedGit = Boolean(team.git_author_name && team.git_author_email);
  const currentGitEl = byId('git-identity-current');
  if (hasSavedGit) {
    currentGitEl.className = 'saved-identity-text';
    currentGitEl.textContent = `${team.git_author_name} <${team.git_author_email}>`;
  } else {
    currentGitEl.className = 'muted-label';
    currentGitEl.textContent = '当前未配置 Git 提交身份';
  }
  byId('clear-git-identity').hidden = !hasSavedGit;
  byId('save-git-identity-btn').textContent = hasSavedGit ? '替换' : '保存身份';

  if (forceInputs && !gitIdentityDirty) {
    byId('git-author-name').value = team.git_author_name || '';
    byId('git-author-email').value = team.git_author_email || '';
  }
  for (const product of Object.keys(PRODUCTS)) {
    const select = byId('role-' + product);
    if (forceInputs || document.activeElement !== select) {
      setSelectOptions(select, Object.entries(ROLES), team.roles?.[product]);
    }
  }

  const deployed = Boolean(team.hooks?.deployed);
  byId('install-hooks').hidden = deployed;
  byId('sync-hooks').hidden = !deployed;
  byId('remove-hooks').hidden = !deployed;

  const obsBar = byId('hook-observed-bar');
  const obsSig = JSON.stringify(team.hooks || {});
  if (obsBar.dataset.sig !== obsSig) {
    obsBar.dataset.sig = obsSig;
    obsBar.replaceChildren(
      ...Object.entries(PRODUCTS).map(([key, label]) => {
        const seen = team.hooks?.observed?.[key];
        const wrap = el('span', 'hook-item');
        wrap.append(
          el('span', seen ? 'dot dot-ok' : deployed ? 'dot dot-warn' : 'dot'),
          el('span', '', !deployed ? `${label} 未部署` : seen ? `${label} · ${seen}` : `${label} 待触发`),
        );
        return wrap;
      }),
    );
  }

  const wtList = byId('worktree-list');
  const worktrees = team.worktrees || [];
  const wtSig = JSON.stringify(worktrees);
  if (wtList.dataset.sig !== wtSig) {
    wtList.dataset.sig = wtSig;
    if (!worktrees.length) {
      wtList.replaceChildren(el('li', 'worktree-row', '请先配置工作区路径'));
    } else {
      wtList.replaceChildren(...worktrees.map(wt => {
        const li = el('li', 'worktree-row');
        const info = el('div', 'worktree-info');
        const title = el('div', 'worktree-name');
        title.append(el('span', '', wt.is_root ? '总工作区' : wt.relative));
        if (wt.branch && wt.branch !== wt.relative) {
          title.append(el('span', 'worktree-branch', wt.branch));
        }
        info.append(title, el('div', 'mono-path', wt.path));

        const badges = el('div', 'worktree-badges');
        if (wt.has_ai) {
          const aiBtn = el('button', 'btn-ghost btn-sm', '.ai 存档');
          aiBtn.type = 'button';
          aiBtn.addEventListener('click', () => openAiPreview(team, wt.relative));
          badges.append(aiBtn);
        }
        const status = el('span', 'hook-item');
        if (wt.up_to_date) status.append(el('span', 'dot dot-ok'), el('span', '', '已同步'));
        else if (wt.deployed) status.append(el('span', 'dot dot-warn'), el('span', '', '需同步'));
        else status.append(el('span', 'dot'), el('span', 'muted-label', '未部署'));
        badges.append(status);
        li.append(info, badges);
        return li;
      }));
    }
  }
}

async function openAiPreview(team, worktree) {
  aiPreviewTeamId = team.id;
  aiPreviewWorktree = worktree;
  byId('ai-preview-title').textContent = `.ai 存档 · ${worktree || '总工作区'}`;
  byId('ai-log-content').hidden = true;
  switchAiTab('current');
  try {
    const data = await api('/control/worktree-ai', { team_id: team.id, worktree });
    const metaBox = byId('ai-current-meta');
    if (data.current?.exists) {
      const m = data.current.meta || {};
      const chips = [];
      if (m.branch) chips.push(el('span', 'tag-soft', m.branch));
      if (m.work_commit) chips.push(el('span', 'tag-soft', m.work_commit.slice(0, 8)));
      if (m.updated_at) chips.push(el('span', 'muted-label', m.updated_at));
      if (data.current.open_memos?.length) {
        chips.push(el('span', 'tag-soft tag-warn', `Memos: ${data.current.open_memos.join(', ')}`));
      }
      metaBox.replaceChildren(...chips);
      byId('ai-current-content').textContent = data.current.content || '';
    } else {
      metaBox.replaceChildren();
      byId('ai-current-content').textContent = '暂无 .ai/CURRENT.md';
    }

    const logList = byId('ai-log-list');
    if (!data.logs?.length) {
      logList.replaceChildren(el('li', 'worktree-row', '暂无交接日志'));
    } else {
      logList.replaceChildren(...data.logs.map(log => {
        const li = el('li', 'worktree-row');
        const info = el('div', 'worktree-info');
        info.append(
          el('div', 'worktree-name', log.title),
          el('div', 'muted-label', `${log.branch} · ${log.work_commit || '-'} · ${log.agent || '-'} / ${log.model || '-'}`),
        );
        const btn = el('button', 'btn-ghost btn-sm', '查看');
        btn.type = 'button';
        btn.addEventListener('click', async () => {
          try {
            const detail = await api('/control/worktree-ai', {
              team_id: aiPreviewTeamId, worktree: aiPreviewWorktree, log_path: log.path,
            });
            const pre = byId('ai-log-content');
            pre.textContent = detail.selected_log?.content || '';
            pre.hidden = false;
            pre.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
          } catch (err) {
            handleError(err);
          }
        });
        li.append(info, btn);
        return li;
      }));
    }
    byId('ai-preview-dialog').showModal();
  } catch (err) {
    handleError(err);
  }
}

function switchAiTab(tab) {
  byId('ai-tab-current').classList.toggle('active', tab === 'current');
  byId('ai-tab-logs').classList.toggle('active', tab === 'logs');
  byId('ai-pane-current').hidden = tab !== 'current';
  byId('ai-pane-logs').hidden = tab !== 'logs';
}

/* Tab 4: Messages */
function renderMessages(team) {
  const memberSelect = byId('filter-member');
  const names = (team.members || []).map(m => m.name);
  setSelectOptions(memberSelect, [['', '全部成员'], ...names.map(n => [n, n])], msgMemberFilter);

  const activity = team.activity || [];
  const filtered = activity.filter(item => {
    if (msgFilter === 'pending' && !isPendingStatus(item.status)) return false;
    if (msgFilter === 'accepted' && item.status !== 'accepted') return false;
    if (msgFilter === 'issue' && !isIssueStatus(item.status)) return false;
    if (msgMemberFilter && item.sender !== msgMemberFilter && item.recipient !== msgMemberFilter) return false;
    return true;
  }).sort((a, b) => Number(isPendingStatus(b.status)) - Number(isPendingStatus(a.status)));

  const list = byId('messages-list');
  byId('messages-empty').hidden = filtered.length > 0;
  const sig = JSON.stringify(filtered);
  if (list.dataset.sig !== sig) {
    const openKeys = new Set(Array.from(list.querySelectorAll('details[open]'), n => n.dataset.key));
    list.dataset.sig = sig;
    list.replaceChildren(...filtered.map(rec => {
      const det = el('details', 'msg-item');
      det.dataset.key = rec.id;
      det.dataset.pending = String(isPendingStatus(rec.status));
      det.open = openKeys.has(rec.id);
      const sum = el('summary', 'msg-summary');
      const statusClass = rec.status === 'accepted' ? 'tag-soft tag-ok'
        : isPendingStatus(rec.status) ? 'tag-soft tag-warn' : 'tag-soft';
      sum.append(
        el('span', 'msg-route', `${rec.sender} → ${rec.recipient}`),
        el('span', 'msg-preview', (rec.body || '').replace(/\s+/g, ' ').slice(0, 80)),
        el('span', statusClass, STATUS_LABELS[rec.status] || rec.status),
      );
      const body = el('div', 'msg-body');
      body.append(el('p', '', rec.body || ''));
      const actions = el('div', 'msg-actions');
      const copyBtn = el('button', 'btn-ghost btn-sm', '复制');
      copyBtn.type = 'button';
      copyBtn.addEventListener('click', async () => {
        try {
          await navigator.clipboard.writeText(rec.body || '');
          flashButtonDone(copyBtn, '✓ 已复制');
          toast('已复制正文');
        } catch {
          toast('无法访问剪贴板', 'warn');
        }
      });
      actions.append(copyBtn);
      if (isPendingStatus(rec.status)) {
        const cancelBtn = el('button', 'btn-danger-ghost btn-sm', '取消送达');
        cancelBtn.type = 'button';
        cancelBtn.addEventListener('click', async () => {
          cancelBtn.disabled = true;
          try {
            await api('/control/message/cancel', { team_id: team.id, id: rec.id });
            det.style.opacity = '0';
            det.style.transform = 'translateX(12px)';
            setTimeout(async () => { await refresh(); toast('已取消送达', 'warn'); }, 180);
          } catch (err) {
            handleError(err);
            cancelBtn.disabled = false;
          }
        });
        actions.append(cancelBtn);
      }
      body.append(actions);
      det.append(sum, body);
      return det;
    }));
  }

  const legacy = team.legacy_tasks || [];
  byId('legacy-section').hidden = legacy.length === 0;
  byId('legacy-count').textContent = String(legacy.length);
  const legacyList = byId('legacy-list');
  const legacySig = JSON.stringify(legacy);
  if (legacyList.dataset.sig !== legacySig) {
    legacyList.dataset.sig = legacySig;
    legacyList.replaceChildren(...legacy.map(rec => {
      const det = el('details', 'msg-item');
      const sum = el('summary', 'msg-summary');
      sum.append(
        el('span', 'msg-route', `旧任务 · ${rec.recipient}`),
        el('span', 'msg-preview', (rec.instruction || '').replace(/\s+/g, ' ').slice(0, 80)),
        el('span', 'tag-soft', STATUS_LABELS[rec.status] || rec.status),
      );
      const body = el('div', 'msg-body');
      body.append(el('p', '', rec.instruction || ''));
      if (rec.result) body.append(el('p', '', rec.result));
      det.append(sum, body);
      return det;
    }));
  }
}

function updateBindingLiveHint(team) {
  if (!bindingCtx || !byId('binding-dialog').open) return;
  const slot = (team.members || []).find(m => m.member === bindingCtx.member);
  if (!slot) return;
  const live = byId('binding-live-state');
  const connected = slot.codex_bound || slot.online || slot.window_bound || slot.desktop_bound || slot.acp_bound || slot.cli_bound;
  if (connected && !bindingCtx.wasBound) live.textContent = `${slot.name} 已接入 Hub`;
  else if (slot.online) live.textContent = `${slot.name} 在线`;
}

function render(data, forceInputs = false) {
  snapshot = data;
  renderExternalAlerts(data.alerts || []);
  const teams = data.teams || [];
  const prevId = selectedTeamId;
  if (!teams.some(t => t.id === selectedTeamId)) {
    selectedTeamId = teams[0]?.id ?? null;
  }
  const teamChanged = prevId !== selectedTeamId;

  byId('empty-state').hidden = teams.length > 0;
  byId('workspace-shell').hidden = teams.length === 0;
  byId('team-choice').hidden = teams.length === 0;
  byId('header-metrics').hidden = teams.length === 0;
  setSelectOptions(
    byId('team-choice'),
    teams.map(t => [t.id, isPeerTeam(t) ? `${t.name} · 平级` : t.name]),
    selectedTeamId,
  );

  const team = currentTeam();
  if (!team) return;
  if (teamChanged) gitIdentityDirty = false;
  renderSummary(team);
  if (isPeerTeam(team) && (activeTab === 'docs' || activeTab === 'workspace')) {
    switchTab('members');
  }
  renderMembers(team);
  if (!isPeerTeam(team)) {
    renderWorkspace(team, forceInputs || teamChanged);
  }
  renderMessages(team);
  updateBindingLiveHint(team);
  if (!isPeerTeam(team)) {
    if (activeTab === 'docs' && (teamChanged || !agentsOverview)) {
      activeDocWorktree = '';
      loadDocsStudio();
    } else if (activeTab === 'docs') {
      renderLeftRail(team);
    }
  }
  requestAnimationFrame(syncIndicators);
}

function handleError(err, inlineEl = null) {
  if (err.unauthorized) {
    lockConsole(err.message);
    return;
  }
  if (inlineEl) inlineEl.textContent = err.message;
  toast(err.message, 'error');
}

async function refresh(forceInputs = false) {
  if (!token) return;
  const seq = ++pollSeq;
  try {
    const data = await api('/control');
    if (seq !== pollSeq) return;
    sessionStorage.setItem(STATE_KEY, token);
    byId('auth-error').textContent = '';
    setUnlocked(true);
    setConnection(true);
    render(data, forceInputs);
  } catch (err) {
    if (seq !== pollSeq) return;
    setConnection(false);
    if (unlocked && err.unauthorized) {
      lockConsole(err.message);
    } else if (!unlocked) {
      byId('auth-error').textContent = err.unauthorized ? err.message : '连不上本机 Hub，请确认服务已启动';
    }
  }
}

/* Dialogs: Member & Binding */
function populateWorktreeChoices(team, currentValue = '') {
  const entries = [['', '总工作区（项目内不限）']];
  for (const wt of team.worktrees || []) {
    if (!wt.is_root) {
      const label = wt.branch && wt.branch !== wt.relative ? `${wt.relative} (${wt.branch})` : wt.relative;
      entries.push([wt.relative, label]);
    }
  }
  if (currentValue && !entries.some(([v]) => v === currentValue)) {
    entries.push([currentValue, currentValue]);
  }
  setSelectOptions(byId('m-worktree'), entries, currentValue);
}

function openMemberDialog(team, slot = null) {
  const peer = isPeerTeam(team);
  editingMemberId = slot ? slot.member : null;
  byId('member-dialog-title').textContent = slot ? `编辑 · ${slot.name}` : '新增成员';
  byId('save-member-btn').textContent = slot ? '保存' : '创建';
  byId('delete-member-btn').hidden = !slot;
  byId('member-form-error').textContent = '';

  byId('m-role-model-row').hidden = peer;
  byId('m-worktree-field').hidden = peer;
  byId('m-role').required = !peer;
  byId('m-responsibility').required = !peer;
  byId('m-responsibility-label').innerHTML = peer
    ? '备注（可选）'
    : '职责 <span class="req">*</span>';
  byId('m-responsibility').placeholder = peer
    ? '例如：所在项目名或当前联调侧重点（可选）'
    : '简述负责模块或范围';
  byId('m-name').placeholder = peer ? '前端项目 / 后端服务' : 'Captain / next-worker';

  byId('m-name').value = slot?.name || '';
  byId('m-product').value = slot?.product || '';
  if (!peer) {
    setSelectOptions(byId('m-role'), Object.entries(ROLES), slot?.role || 'worker');
    byId('m-model').value = slot?.model || '';
    populateWorktreeChoices(team, slot?.worktree || '');
  }
  byId('m-responsibility').value = peer ? (slot?.note || slot?.responsibility || '') : (slot?.responsibility || '');
  byId('member-dialog').showModal();
  byId('m-name').focus();
}

function openBindDialog(team, slot) {
  const peer = isPeerTeam(team);
  bindingCtx = {
    teamId: team.id,
    member: slot.member,
    name: slot.name,
    product: slot.product,
    wasBound: Boolean(slot.window_bound || slot.desktop_bound || slot.codex_bound || slot.acp_bound || slot.cli_bound),
  };
  byId('binding-title').textContent = `绑定 · ${slot.name}`;
  byId('binding-error').textContent = '';
  byId('binding-output').hidden = true;
  byId('binding-live-state').textContent = '';

  const targetDir = peer
    ? ''
    : (slot.worktree && team.workspace
      ? `${team.workspace}\\${slot.worktree.replaceAll('/', '\\')}`
      : (team.workspace || ''));
  const noteEl = byId('binding-workspace-note');
  noteEl.textContent = targetDir;
  noteEl.hidden = !targetDir;

  const steps = byId('binding-steps');
  if (slot.product === 'codex') {
    steps.replaceChildren(
      el('li', '', peer ? '在 Codex Desktop 打开任意项目的目标原聊天（需先配置跨项目全局接入并信任用户级钩子）' : '在 Codex Desktop 打开该工作区目标聊天'),
      el('li', '', '复制 36 位 Session ID 粘贴到下方保存'),
    );
  } else if (slot.product === 'claude') {
    steps.replaceChildren(
      el('li', '', peer ? '确认已配置跨项目全局接入，然后点击下方生成绑定码' : '点击下方生成绑定码'),
      el('li', '', '复制到目标原聊天执行一次，后续重启自动恢复'),
    );
  } else if (slot.product === 'agy_acp') {
    steps.replaceChildren(
      el('li', '', '点击下方生成 10 位配对码'),
      el('li', '', '在 AGY ACP（DSH）目标会话的 Team Hub 入口输入配对码，后续重启自动恢复'),
    );
  } else if (slot.product === 'agy_cli') {
    steps.replaceChildren(
      el('li', '', '点击下方生成绑定指令（复用同一份 Aux Relay 插件，无需额外安装）'),
      el('li', '', '复制到目标 agy CLI 原会话发送，完成后会自动挂起后台收件监听'),
    );
  } else {
    steps.replaceChildren(
      el('li', '', '点击下方生成 10 位配对码'),
      el('li', '', '在目标原聊天侧栏打开 Aux Relay 输入配对码'),
    );
  }

  byId('binding-codex').hidden = slot.product !== 'codex';
  byId('binding-antigravity').hidden = slot.product !== 'antigravity' && slot.product !== 'agy_acp' && slot.product !== 'agy_cli';
  byId('codex-thread').required = slot.product === 'codex';
  byId('codex-thread').value = slot.thread_id || '';
  byId('window-label').value = slot.product === 'agy_acp'
    ? (slot.acp_label || '')
    : slot.product === 'agy_cli' ? (slot.cli_label || '') : (slot.window_label || '');
  byId('save-binding').textContent = slot.product === 'codex' ? '保存绑定' : '生成绑定码';
  byId('binding-dialog').showModal();
}

/* Events */
byId('theme-toggle').addEventListener('click', () => {
  const next = document.documentElement.dataset.theme === 'dark' ? 'light' : 'dark';
  applyTheme(next, true);
});

window.addEventListener('resize', () => requestAnimationFrame(syncIndicators));

byId('auth').addEventListener('submit', event => {
  event.preventDefault();
  token = byId('token').value.trim();
  refresh(true).then(() => { if (unlocked) byId('token').value = ''; });
});

byId('lock').addEventListener('click', () => lockConsole());

byId('team-choice').addEventListener('change', event => {
  selectedTeamId = Number(event.target.value);
  msgMemberFilter = '';
  agentsOverview = null;
  if (snapshot) render(snapshot, true);
});

for (const tabBtn of document.querySelectorAll('#main-tabs [role="tab"]')) {
  tabBtn.addEventListener('click', () => switchTab(tabBtn.dataset.tab));
}
for (const segBtn of document.querySelectorAll('#inspector-tabs .seg-btn')) {
  segBtn.addEventListener('click', () => switchInspector(segBtn.dataset.insp));
}
for (const aiTab of document.querySelectorAll('[data-ai-tab]')) {
  aiTab.addEventListener('click', () => switchAiTab(aiTab.dataset.aiTab));
}
for (const closeBtn of document.querySelectorAll('[data-close-dialog]')) {
  closeBtn.addEventListener('click', () => byId(closeBtn.dataset.closeDialog).close());
}

function setNewTeamMode(mode) {
  newTeamMode = mode === 'peer' ? 'peer' : 'structured';
  const peer = newTeamMode === 'peer';
  for (const btn of document.querySelectorAll('#new-team-mode-seg [data-team-mode]')) {
    btn.classList.toggle('active', btn.dataset.teamMode === newTeamMode);
  }
  byId('new-team-mode-hint').textContent = peer
    ? '跨项目平级沟通，不绑定工作区，不使用 Hook 注入团队身份。'
    : '绑定单一工作区，支持角色分工、Hook 注入与规范文档管理。';
  byId('new-team-workspace-field').hidden = peer;
  byId('new-team-workspace').required = !peer;
  byId('new-team-name').placeholder = peer ? '跨项目联调群' : 'my-project-team';
}

for (const btn of document.querySelectorAll('#new-team-mode-seg [data-team-mode]')) {
  btn.addEventListener('click', () => setNewTeamMode(btn.dataset.teamMode));
}

function openCreateTeamDialog(initialMode = 'structured') {
  byId('create-team-form').reset();
  setNewTeamMode(initialMode);
  byId('create-team-error').textContent = '';
  byId('create-team-dialog').showModal();
  byId('new-team-name').focus();
}

byId('open-create-team').addEventListener('click', () => openCreateTeamDialog('structured'));
byId('empty-create-structured').addEventListener('click', () => openCreateTeamDialog('structured'));
byId('empty-create-peer').addEventListener('click', () => openCreateTeamDialog('peer'));

byId('create-team-form').addEventListener('submit', async event => {
  event.preventDefault();
  const btn = event.submitter;
  btn.disabled = true;
  byId('create-team-error').textContent = '';
  try {
    const payload = {
      action: 'create',
      mode: newTeamMode,
      name: byId('new-team-name').value,
    };
    if (newTeamMode === 'structured') {
      payload.workspace = byId('new-team-workspace').value;
    }
    const reply = await api('/control/team', payload);
    selectedTeamId = reply.team_id;
    agentsOverview = null;
    byId('create-team-dialog').close();
    await refresh(true);
    toast(newTeamMode === 'peer' ? '无分工 Team 已创建' : '分工 Team 已创建');
  } catch (err) {
    handleError(err, byId('create-team-error'));
  } finally {
    btn.disabled = false;
  }
});

for (const openAdd of [byId('open-new-member'), byId('empty-new-member')]) {
  openAdd.addEventListener('click', () => { const t = currentTeam(); if (t) openMemberDialog(t, null); });
}

byId('m-product').addEventListener('change', event => {
  const team = currentTeam();
  if (!team || isPeerTeam(team) || editingMemberId) return;
  const p = event.target.value;
  if (p && team.roles?.[p]) byId('m-role').value = team.roles[p];
});

byId('member-form').addEventListener('submit', async event => {
  event.preventDefault();
  const team = currentTeam();
  if (!team) return;
  const peer = isPeerTeam(team);
  const btn = event.submitter;
  btn.disabled = true;
  byId('member-form-error').textContent = '';
  try {
    const payload = peer
      ? {
          action: 'member', team_id: team.id, member: editingMemberId || undefined,
          name: byId('m-name').value, product: byId('m-product').value,
          note: byId('m-responsibility').value,
        }
      : {
          action: 'member', team_id: team.id, member: editingMemberId || undefined,
          name: byId('m-name').value, product: byId('m-product').value, role: byId('m-role').value,
          model: byId('m-model').value, worktree: byId('m-worktree').value, responsibility: byId('m-responsibility').value,
        };
    await api('/control/team', payload);
    byId('member-dialog').close();
    await refresh();
    toast(editingMemberId ? '成员已更新' : '成员已添加');
  } catch (err) {
    handleError(err, byId('member-form-error'));
  } finally {
    btn.disabled = false;
  }
});

byId('delete-member-btn').addEventListener('click', async event => {
  const team = currentTeam();
  if (!team || !editingMemberId) return;
  const slot = team.members.find(m => m.member === editingMemberId);
  if (!confirm(`确认移出「${slot?.name || editingMemberId}」？`)) return;
  const btn = event.currentTarget;
  btn.disabled = true;
  try {
    await api('/control/team', { action: 'remove_member', team_id: team.id, member: editingMemberId });
    byId('member-dialog').close();
    await refresh();
    toast('成员已移出', 'warn');
  } catch (err) {
    handleError(err, byId('member-form-error'));
  } finally {
    btn.disabled = false;
  }
});

/* Docs Studio Events */
for (const ev of ['mouseup', 'keyup', 'select']) {
  byId('doc-textarea').addEventListener(ev, updateSelectionBar);
}

byId('sel-find-similar').addEventListener('click', () => {
  const sel = getEditorSelection();
  byId('similar-query-input').value = sel;
  switchInspector('similar');
  if (sel) runSimilarSearch();
});

byId('sel-batch-append').addEventListener('click', () => {
  const sel = getEditorSelection();
  if (sel) byId('append-text-input').value = sel;
  switchInspector('append');
});

byId('doc-reload-btn').addEventListener('click', () => loadDocsStudio());

byId('doc-toggle-ext-diff').addEventListener('click', () => {
  if (currentDocData?.external_diff) {
    openInlineDiff(`外改对比：Hub 基线 → 磁盘当前（${currentDocData.target || currentDocData.label}）`, currentDocData.external_diff);
  }
});

byId('doc-toggle-drift-diff').addEventListener('click', () => {
  const sg = agentsOverview?.shared_group || {};
  const wtInfo = (agentsOverview?.worktrees || []).find(w => w.relative === activeDocWorktree);
  if (wtInfo?.drift_inline) {
    openInlineDiff(`漂移对比：基准 (${sg.canonical_worktree}) → ${wtInfo.label}`, wtInfo.drift_inline);
  }
});

byId('doc-close-diff').addEventListener('click', closeInlineDiff);

for (const btn of document.querySelectorAll('[data-diff-scope]')) {
  btn.addEventListener('click', () => {
    diffScope = btn.dataset.diffScope;
    for (const o of document.querySelectorAll('[data-diff-scope]')) o.classList.toggle('active', o === btn);
    renderInlineDiffView();
  });
}

byId('doc-accept-external-btn').addEventListener('click', async event => {
  const team = currentTeam();
  if (!team) return;
  const btn = event.currentTarget;
  btn.disabled = true;
  try {
    await api('/control/doc', {
      team_id: team.id, action: 'accept_external', doc_key: activeDocKey,
      worktree: activeDocKey === 'agents' ? activeDocWorktree : '',
    });
    await refresh();
    await loadDocsStudio();
    toast('已采纳磁盘版本');
  } catch (err) {
    handleError(err, byId('doc-error'));
  } finally {
    btn.disabled = false;
  }
});

async function saveSharedFromStudio(triggerBtn) {
  const team = currentTeam();
  if (!team) return;
  const sel = readInspMatrix(byId('sync-wt-matrix'));
  if (!sel.write.length) {
    byId('sync-error').textContent = '请至少勾选一棵目标工作树';
    switchInspector('sync');
    return;
  }
  triggerBtn.disabled = true;
  byId('doc-error').textContent = '';
  byId('sync-error').textContent = '';
  try {
    const res = await api('/control/doc', {
      team_id: team.id,
      action: 'save_shared',
      content: byId('doc-textarea').value,
      target_worktrees: sel.write,
      commit_worktrees: sel.commit,
      note: byId('doc-note').value.trim(),
    });
    await refresh();
    await loadDocsStudio();
    flashButtonDone(triggerBtn, '✓ 已同步');
    const cs = formatCommitSummary(res.commits);
    toast(cs ? `已同步 ${res.synced.length} 棵（${cs}）` : `已同步 ${res.synced.length} 棵共享规范`);
  } catch (err) {
    handleError(err, byId('doc-error'));
  } finally {
    triggerBtn.disabled = false;
  }
}

byId('doc-save-btn').addEventListener('click', async event => {
  const team = currentTeam();
  if (!team) return;
  const btn = event.currentTarget;
  if (activeDocKey === 'agents' && activeAgentsMode === 'shared') {
    await saveSharedFromStudio(btn);
    return;
  }
  btn.disabled = true;
  byId('doc-error').textContent = '';
  try {
    const payload = {
      team_id: team.id,
      action: 'save',
      doc_key: activeDocKey,
      worktree: activeDocKey === 'agents' ? activeDocWorktree : '',
      content: byId('doc-textarea').value,
      note: byId('doc-note').value.trim(),
    };
    if (activeDocKey === 'agents') {
      if (currentDocData?.has_shared_markers) {
        payload.content_scope = 'independent';
      }
      if (byId('doc-single-commit').checked) {
        payload.auto_commit = true;
        payload.commit_worktrees = [activeDocWorktree];
      }
    }
    const res = await api('/control/doc', payload);
    await refresh();
    await loadDocsStudio();
    flashButtonDone(btn, '✓ 已保存');
    const cs = formatCommitSummary(res.commits);
    toast(cs ? `已保存（${cs}）` : '已保存');
  } catch (err) {
    handleError(err, byId('doc-error'));
  } finally {
    btn.disabled = false;
  }
});

byId('sync-jump-shared').addEventListener('click', () => {
  activeDocKey = 'agents';
  activeAgentsMode = 'shared';
  loadDocsStudio();
});

byId('mobile-toggle-rail').addEventListener('click', event => {
  const studio = byId('docs-studio');
  const open = studio.classList.toggle('rail-open');
  event.currentTarget.setAttribute('aria-expanded', String(open));
  event.currentTarget.textContent = open ? '收起目录' : '目录';
});

byId('mobile-jump-insp').addEventListener('click', () => {
  const insp = document.querySelector('.studio-inspector');
  if (insp) insp.scrollIntoView({ behavior: 'smooth', block: 'start' });
});

byId('sync-write-all').addEventListener('click', () => {
  for (const row of byId('sync-wt-matrix').querySelectorAll('.insp-row')) {
    const w = row.querySelector('input[data-role="write"]');
    const c = row.querySelector('input[data-role="commit"]');
    if (w) w.checked = true;
    if (c) c.disabled = false;
  }
});
byId('sync-commit-all').addEventListener('click', () => {
  for (const row of byId('sync-wt-matrix').querySelectorAll('.insp-row')) {
    const w = row.querySelector('input[data-role="write"]');
    const c = row.querySelector('input[data-role="commit"]');
    if (w && w.checked && c) { c.disabled = false; c.checked = true; }
  }
});
byId('sync-commit-none').addEventListener('click', () => {
  for (const c of byId('sync-wt-matrix').querySelectorAll('input[data-role="commit"]')) c.checked = false;
});

byId('doc-reset-btn').addEventListener('click', async event => {
  const team = currentTeam();
  if (!team) return;
  if (!confirm('确认恢复为 Hub 默认模板？')) return;
  const btn = event.currentTarget;
  btn.disabled = true;
  try {
    await api('/control/doc', { team_id: team.id, action: 'reset', doc_key: activeDocKey });
    await refresh();
    await loadDocsStudio();
    toast('已恢复默认');
  } catch (err) {
    handleError(err, byId('doc-error'));
  } finally {
    btn.disabled = false;
  }
});

byId('rev-rollback-confirm').addEventListener('click', () => {
  if (previewRevId != null) doRollback(previewRevId);
});

/* Similar Search Events */
byId('similar-run-btn').addEventListener('click', runSimilarSearch);
byId('similar-query-input').addEventListener('keydown', e => { if (e.key === 'Enter') { e.preventDefault(); runSimilarSearch(); } });

for (const pill of document.querySelectorAll('[data-sim-threshold]')) {
  pill.addEventListener('click', () => {
    similarThreshold = Number(pill.dataset.simThreshold);
    for (const o of document.querySelectorAll('[data-sim-threshold]')) o.classList.toggle('active', o === pill);
    if (byId('similar-query-input').value.trim()) runSimilarSearch();
  });
}

byId('similar-select-rec').addEventListener('click', () => {
  for (const cb of byId('similar-results-list').querySelectorAll('input[data-match-id]')) {
    const m = similarMatches.find(x => x.id === cb.dataset.matchId);
    cb.checked = Boolean(m?.recommended);
  }
  syncSimilarCommitChecks();
});
byId('similar-select-all').addEventListener('click', () => {
  for (const cb of byId('similar-results-list').querySelectorAll('input[data-match-id]')) cb.checked = true;
  syncSimilarCommitChecks();
});
byId('similar-select-none').addEventListener('click', () => {
  for (const cb of byId('similar-results-list').querySelectorAll('input[data-match-id]')) cb.checked = false;
  syncSimilarCommitChecks();
});
byId('similar-commit-all').addEventListener('click', () => {
  for (const cb of byId('similar-commit-checks').querySelectorAll('input[type="checkbox"]')) cb.checked = true;
});
byId('similar-commit-none').addEventListener('click', () => {
  for (const cb of byId('similar-commit-checks').querySelectorAll('input[type="checkbox"]')) cb.checked = false;
});

for (const btn of document.querySelectorAll('[data-sim-action]')) {
  btn.addEventListener('click', () => setSimilarActionMode(btn.dataset.simAction));
}

byId('similar-swap-btn').addEventListener('click', () => {
  const qEl = byId('similar-query-input');
  const rEl = byId('similar-replace-input');
  const tmp = qEl.value;
  qEl.value = rEl.value;
  rEl.value = tmp;
  if (qEl.value.trim()) runSimilarSearch();
});

byId('similar-remove-btn').addEventListener('click', async event => {
  const team = currentTeam();
  if (!team) return;
  const checkedIds = new Set(Array.from(byId('similar-results-list').querySelectorAll('input[data-match-id]:checked'), cb => cb.dataset.matchId));
  const items = similarMatches.filter(m => checkedIds.has(m.id)).map(m => ({
    worktree: m.worktree, start_offset: m.start_offset, end_offset: m.end_offset, expected_text: m.text,
  }));
  if (!items.length) return;
  const isRep = similarActionMode === 'replace';
  const replacement = byId('similar-replace-input').value.trim();
  if (isRep && !replacement) {
    byId('similar-error').textContent = '请输入替换后的新正文';
    byId('similar-replace-input').focus();
    return;
  }
  const commitWorktrees = Array.from(byId('similar-commit-checks').querySelectorAll('input:checked'), cb => cb.value);
  const btn = event.currentTarget;
  btn.disabled = true;
  byId('similar-error').textContent = '';
  try {
    const payload = {
      team_id: team.id,
      action: isRep ? 'batch_replace' : 'batch_remove',
      items,
      commit_worktrees: commitWorktrees,
      note: byId('similar-remove-note').value.trim(),
    };
    if (isRep) payload.replacement = replacement;
    const res = await api('/control/doc', payload);
    // Smooth row collapse animation before reload
    for (const row of byId('similar-results-list').querySelectorAll('.insp-row')) {
      if (checkedIds.has(row.dataset.matchId)) row.classList.add('removing');
    }
    setTimeout(async () => {
      await refresh();
      await loadDocsStudio();
      await runSimilarSearch();
      const cs = formatCommitSummary(res.commits);
      const count = isRep ? res.replaced_count : res.removed_count;
      const verb = isRep ? '替换' : '移除';
      toast(cs ? `已${verb} ${count} 条（${cs}）` : `已${verb} ${count} 条约束`);
    }, 200);
  } catch (err) {
    handleError(err, byId('similar-error'));
    btn.disabled = false;
  }
});

/* Batch Append Events */
byId('append-position').addEventListener('change', event => {
  byId('append-section-select').hidden = event.target.value !== 'section';
});
byId('append-scope-shared').addEventListener('click', () => renderAppendInspector('shared'));
byId('append-scope-all').addEventListener('click', () => renderAppendInspector('all'));
byId('append-preview-btn').addEventListener('click', runAppendPreview);

byId('append-apply-btn').addEventListener('click', async event => {
  const team = currentTeam();
  if (!team) return;
  const text = byId('append-text-input').value.trim();
  const sel = readInspMatrix(byId('append-wt-matrix'));
  byId('append-error').textContent = '';
  if (!text) { byId('append-error').textContent = '请输入文本'; return; }
  if (!sel.write.length) { byId('append-error').textContent = '请勾选目标工作树'; return; }
  const btn = event.currentTarget;
  btn.disabled = true;
  try {
    const res = await api('/control/doc', {
      team_id: team.id, action: 'batch_append', text, worktrees: sel.write,
      position: byId('append-position').value, section_heading: byId('append-section-select').value,
      commit_worktrees: sel.commit, note: byId('append-note').value.trim(),
    });
    byId('append-text-input').value = '';
    byId('append-note').value = '';
    await refresh();
    await loadDocsStudio();
    flashButtonDone(btn, '✓ 已添加');
    const cs = formatCommitSummary(res.commits);
    toast(cs ? `已添加至 ${res.updated_worktrees.length} 棵（${cs}）` : `已添加至 ${res.updated_worktrees.length} 棵`);
  } catch (err) {
    handleError(err, byId('append-error'));
  } finally {
    btn.disabled = false;
  }
});

/* Binding & Copy */
byId('binding-form').addEventListener('submit', async event => {
  event.preventDefault();
  const ctx = bindingCtx;
  if (!ctx) return;
  const btn = event.submitter;
  btn.disabled = true;
  byId('binding-error').textContent = '';
  try {
    if (ctx.product === 'codex') {
      await api('/control/codex-bind', { member: ctx.member, thread_id: byId('codex-thread').value.trim() });
      await refresh();
      byId('binding-dialog').close();
      toast(`${ctx.name} 已绑定`);
    } else {
      const path = ctx.product === 'claude' ? '/control/rebind'
        : ctx.product === 'agy_acp' ? '/control/acp'
        : ctx.product === 'agy_cli' ? '/control/agy-cli' : '/control/antigravity';
      const reply = await api(path, { member: ctx.member, team_id: ctx.teamId, label: byId('window-label').value.trim() });
      const cmd = ctx.product === 'claude'
        ? `bind_team_member(name="${ctx.member}", code="${reply.code}")`
        : ctx.product === 'agy_cli'
          ? `请调用 relay 插件的 team_cli_bind(code="${reply.code}") 绑定到 Team Hub；绑定成功后，请用返回的 wait_command 在后台启动一次收件监听，后续收到队友消息被唤醒并回复后再重新挂起该监听。`
          : reply.code;
      byId('binding-command').textContent = cmd;
      byId('binding-output').hidden = false;
      byId('binding-live-state').textContent = '等待目标聊天完成绑定…';
      toast('绑定码已生成');
    }
  } catch (err) {
    handleError(err, byId('binding-error'));
  } finally {
    btn.disabled = false;
  }
});

byId('copy-binding-btn').addEventListener('click', async event => {
  const btn = event.currentTarget;
  try {
    await navigator.clipboard.writeText(byId('binding-command').textContent);
    flashButtonDone(btn, '✓ 已复制');
    toast('已复制');
  } catch {
    toast('无法访问剪贴板', 'warn');
  }
});

/* Workspace & Git Identity */
function currentWorkspacePayload(team) {
  return {
    action: 'workspace', team_id: team.id, workspace: byId('workspace-input').value,
    roles: Object.fromEntries(Object.keys(PRODUCTS).map(p => [p, byId('role-' + p).value])),
  };
}

byId('workspace-path-form').addEventListener('submit', async event => {
  event.preventDefault();
  const team = currentTeam();
  if (!team) return;
  const btn = event.submitter;
  btn.disabled = true;
  byId('workspace-error').textContent = '';
  try {
    await api('/control/team', currentWorkspacePayload(team));
    agentsOverview = null;
    await refresh(true);
    flashButtonDone(btn, '✓ 已保存');
    toast('路径已更新');
  } catch (err) {
    handleError(err, byId('workspace-error'));
  } finally {
    btn.disabled = false;
  }
});

async function detectAndFillGitIdentity(sourceKey) {
  const team = currentTeam();
  if (!team) return;
  byId('git-identity-error').textContent = '';
  try {
    const info = await api('/control/git-identity/detect', { team_id: team.id });
    const found = info[sourceKey];
    if (!found) {
      toast(sourceKey === 'gh' ? '未检测到 gh 身份' : '未读到仓库 git config', 'warn');
      return;
    }
    gitIdentityDirty = true;
    byId('git-author-name').value = found.name;
    byId('git-author-email').value = found.email;
    toast(`已填入 ${sourceKey} 身份，点击保存生效`);
  } catch (err) {
    handleError(err, byId('git-identity-error'));
  }
}

byId('git-author-name').addEventListener('input', () => { gitIdentityDirty = true; });
byId('git-author-email').addEventListener('input', () => { gitIdentityDirty = true; });
byId('detect-gh-identity').addEventListener('click', () => detectAndFillGitIdentity('gh'));
byId('detect-git-identity').addEventListener('click', () => detectAndFillGitIdentity('git'));

byId('clear-git-identity').addEventListener('click', async event => {
  const team = currentTeam();
  if (!team) return;
  if (!confirm('确认移除当前保存的 Git 提交身份？')) return;
  const btn = event.currentTarget;
  btn.disabled = true;
  byId('git-identity-error').textContent = '';
  try {
    await api('/control/team', {
      action: 'git_identity', team_id: team.id,
      git_author_name: '', git_author_email: '',
    });
    gitIdentityDirty = false;
    await refresh(true);
    toast('已移除 Git 提交身份', 'warn');
  } catch (err) {
    handleError(err, byId('git-identity-error'));
  } finally {
    btn.disabled = false;
  }
});

byId('git-identity-form').addEventListener('submit', async event => {
  event.preventDefault();
  const team = currentTeam();
  if (!team) return;
  const btn = event.submitter;
  btn.disabled = true;
  byId('git-identity-error').textContent = '';
  try {
    const hadSaved = Boolean(team.git_author_name && team.git_author_email);
    await api('/control/team', {
      action: 'git_identity', team_id: team.id,
      git_author_name: byId('git-author-name').value, git_author_email: byId('git-author-email').value,
    });
    gitIdentityDirty = false;
    await refresh(true);
    flashButtonDone(btn, hadSaved ? '✓ 已替换' : '✓ 已保存');
    toast(hadSaved ? 'Git 身份已替换' : 'Git 身份已保存');
  } catch (err) {
    handleError(err, byId('git-identity-error'));
  } finally {
    btn.disabled = false;
  }
});

byId('defaults-form').addEventListener('submit', async event => {
  event.preventDefault();
  const team = currentTeam();
  if (!team) return;
  const btn = event.submitter;
  btn.disabled = true;
  try {
    await api('/control/team', currentWorkspacePayload(team));
    await refresh(true);
    flashButtonDone(btn, '✓ 已保存');
    toast('默认分工已保存');
  } catch (err) {
    handleError(err);
  } finally {
    btn.disabled = false;
  }
});

byId('refresh-worktrees').addEventListener('click', async event => {
  const team = currentTeam();
  if (!team) return;
  const btn = event.currentTarget;
  btn.disabled = true;
  try {
    await api('/control/worktrees', { team_id: team.id });
    agentsOverview = null;
    await refresh();
    toast('已刷新工作树');
  } catch (err) {
    handleError(err);
  } finally {
    btn.disabled = false;
  }
});

async function runProjectHookAction(btn, action, doneMsg) {
  const team = currentTeam();
  if (!team) return;
  btn.disabled = true;
  byId('workspace-error').textContent = '';
  try {
    if (action === 'install') await api('/control/team', currentWorkspacePayload(team));
    await api('/control/project', { team_id: team.id, action });
    await refresh(true);
    toast(doneMsg);
  } catch (err) {
    handleError(err, byId('workspace-error'));
  } finally {
    btn.disabled = false;
  }
}

byId('install-hooks').addEventListener('click', e => runProjectHookAction(e.currentTarget, 'install', 'Hook 已部署'));
byId('sync-hooks').addEventListener('click', e => runProjectHookAction(e.currentTarget, 'sync', 'Hook 已同步'));
byId('remove-hooks').addEventListener('click', e => runProjectHookAction(e.currentTarget, 'remove', 'Hook 已撤销'));

async function runArchiveCurrentTeam(btn) {
  const team = currentTeam();
  if (!team) return;
  if (!confirm(`确认移除 Team「${team.name}」？`)) return;
  btn.disabled = true;
  try {
    await api('/control/team', { action: 'archive', team_id: team.id });
    agentsOverview = null;
    await refresh(true);
    toast(`已移除 ${team.name}`, 'warn');
  } catch (err) {
    handleError(err);
  } finally {
    btn.disabled = false;
  }
}

byId('archive-team').addEventListener('click', e => runArchiveCurrentTeam(e.currentTarget));
byId('peer-archive-team').addEventListener('click', e => runArchiveCurrentTeam(e.currentTarget));

async function runGlobalAccessAction(btn, action, doneMsg) {
  btn.disabled = true;
  byId('global-access-error').textContent = '';
  try {
    await api('/control/global-access', { action });
    await refresh(true);
    toast(doneMsg, action === 'remove' ? 'warn' : 'ok');
  } catch (err) {
    handleError(err, byId('global-access-error'));
  } finally {
    btn.disabled = false;
  }
}

byId('install-global-access').addEventListener('click', e =>
  runGlobalAccessAction(e.currentTarget, 'install', '全局跨项目接入已配置'));
byId('remove-global-access').addEventListener('click', e => {
  if (!confirm('确认移除用户级全局 Claude / Codex 接入配置？')) return;
  runGlobalAccessAction(e.currentTarget, 'remove', '已移除全局跨项目接入');
});

/* Messages Events */
for (const pill of document.querySelectorAll('[data-filter]')) {
  pill.addEventListener('click', () => {
    msgFilter = pill.dataset.filter;
    for (const o of document.querySelectorAll('[data-filter]')) o.classList.toggle('active', o === pill);
    if (currentTeam()) renderMessages(currentTeam());
  });
}

byId('filter-member').addEventListener('change', event => {
  msgMemberFilter = event.target.value;
  if (currentTeam()) renderMessages(currentTeam());
});

byId('open-send-msg').addEventListener('click', () => {
  const team = currentTeam();
  if (!team || !(team.members || []).length) { toast('暂无可发送成员', 'warn'); return; }
  setSelectOptions(byId('send-msg-to'), team.members.map(m => [m.name, `${m.name} (${PRODUCTS[m.product] || m.product})`]));
  byId('send-msg-body').value = '';
  byId('send-msg-error').textContent = '';
  byId('send-msg-dialog').showModal();
  byId('send-msg-body').focus();
});

byId('send-msg-form').addEventListener('submit', async event => {
  event.preventDefault();
  const team = currentTeam();
  if (!team) return;
  const btn = event.submitter;
  btn.disabled = true;
  byId('send-msg-error').textContent = '';
  try {
    await api('/control/message/send', { team_id: team.id, to: byId('send-msg-to').value, text: byId('send-msg-body').value });
    byId('send-msg-dialog').close();
    await refresh();
    toast('消息已入队');
  } catch (err) {
    handleError(err, byId('send-msg-error'));
  } finally {
    btn.disabled = false;
  }
});

window.addEventListener('hashchange', () => {
  const launch = readLaunchToken();
  if (launch) { token = launch; refresh(true); }
});

if (token) refresh(true);
setInterval(() => { if (unlocked) refresh(false); }, 3500);

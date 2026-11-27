import {
  STATE_KEY,
  api,
  applyTheme,
  byId,
  currentTeam,
  el,
  handleError,
  initTheme,
  isPeerTeam,
  lockConsole,
  readLaunchToken,
  session,
  setConnection,
  setSelectOptions,
  setUnlocked,
  syncIndicators,
  toast,
} from './modules/core.js';
import {
  initMembersEvents,
  renderMembers,
  renderSummary,
  updateBindingLiveHint,
} from './modules/members.js';
import {
  initMessagesEvents,
  renderMessages,
  resetMessageMemberFilter,
} from './modules/messages.js';
import {
  initWorkspaceEvents,
  renderWorkspace,
  resetGitIdentityDirty,
} from './modules/workspace.js';
import {
  hasAgentsOverview,
  initDocsStudioEvents,
  loadDocsStudio,
  renderLeftRail,
  resetDocsOverview,
  selectAlertDoc,
  setActiveDocWorktree,
} from './modules/docs_studio.js';

let newTeamMode = 'structured';

initTheme();

function switchTab(name) {
  const team = currentTeam();
  const target = (team && isPeerTeam(team) && (name === 'docs' || name === 'workspace')) ? 'members' : name;
  session.activeTab = target;
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
      session.selectedTeamId = a.team_id;
      selectAlertDoc(a.doc_key, a.target || '');
      if (session.snapshot) render(session.snapshot, true);
      switchTab('docs');
      loadDocsStudio();
    });
    items.append(btn);
  }
  wrap.replaceChildren(items);
}

function render(data, forceInputs = false) {
  session.snapshot = data;
  renderExternalAlerts(data.alerts || []);
  const teams = data.teams || [];
  const prevId = session.selectedTeamId;
  if (!teams.some(t => t.id === session.selectedTeamId)) {
    session.selectedTeamId = teams[0]?.id ?? null;
  }
  const teamChanged = prevId !== session.selectedTeamId;

  byId('empty-state').hidden = teams.length > 0;
  byId('workspace-shell').hidden = teams.length === 0;
  byId('team-choice').hidden = teams.length === 0;
  byId('header-metrics').hidden = teams.length === 0;
  setSelectOptions(
    byId('team-choice'),
    teams.map(t => [t.id, isPeerTeam(t) ? `${t.name} · 平级` : t.name]),
    session.selectedTeamId,
  );

  const team = currentTeam();
  if (!team) return;
  if (teamChanged) resetGitIdentityDirty();
  renderSummary(team);
  if (isPeerTeam(team) && (session.activeTab === 'docs' || session.activeTab === 'workspace')) {
    switchTab('members');
  }
  renderMembers(team);
  if (!isPeerTeam(team)) {
    renderWorkspace(team, forceInputs || teamChanged);
  }
  renderMessages(team);
  updateBindingLiveHint(team);
  if (!isPeerTeam(team)) {
    if (session.activeTab === 'docs' && (teamChanged || !hasAgentsOverview())) {
      setActiveDocWorktree('');
      loadDocsStudio();
    } else if (session.activeTab === 'docs') {
      renderLeftRail(team);
    }
  }
  requestAnimationFrame(syncIndicators);
}

async function refresh(forceInputs = false) {
  if (!session.token) return;
  const seq = ++session.pollSeq;
  try {
    const data = await api('/control');
    if (seq !== session.pollSeq) return;
    sessionStorage.setItem(STATE_KEY, session.token);
    byId('auth-error').textContent = '';
    setUnlocked(true);
    setConnection(true);
    render(data, forceInputs);
  } catch (err) {
    if (seq !== session.pollSeq) return;
    setConnection(false);
    if (session.unlocked && err.unauthorized) {
      lockConsole(err.message);
    } else if (!session.unlocked) {
      byId('auth-error').textContent = err.unauthorized ? err.message : '连不上本机 Hub，请确认服务已启动';
    }
  }
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

function openCreateTeamDialog(initialMode = 'structured') {
  byId('create-team-form').reset();
  setNewTeamMode(initialMode);
  byId('create-team-error').textContent = '';
  byId('create-team-dialog').showModal();
  byId('new-team-name').focus();
}

async function runArchiveCurrentTeam(btn) {
  const team = currentTeam();
  if (!team) return;
  if (!confirm(`确认移除 Team「${team.name}」？`)) return;
  btn.disabled = true;
  try {
    await api('/control/team', { action: 'archive', team_id: team.id });
    resetDocsOverview();
    await refresh(true);
    toast(`已移除 ${team.name}`, 'warn');
  } catch (err) {
    handleError(err);
  } finally {
    btn.disabled = false;
  }
}

function bindGlobalEvents() {
  byId('theme-toggle').addEventListener('click', () => {
    const next = document.documentElement.dataset.theme === 'dark' ? 'light' : 'dark';
    applyTheme(next, true);
  });

  window.addEventListener('resize', () => requestAnimationFrame(syncIndicators));

  byId('auth').addEventListener('submit', event => {
    event.preventDefault();
    session.token = byId('token').value.trim();
    refresh(true).then(() => { if (session.unlocked) byId('token').value = ''; });
  });

  byId('lock').addEventListener('click', () => lockConsole());

  byId('team-choice').addEventListener('change', event => {
    session.selectedTeamId = Number(event.target.value);
    resetMessageMemberFilter();
    resetDocsOverview();
    if (session.snapshot) render(session.snapshot, true);
  });

  for (const tabBtn of document.querySelectorAll('#main-tabs [role="tab"]')) {
    tabBtn.addEventListener('click', () => switchTab(tabBtn.dataset.tab));
  }

  for (const closeBtn of document.querySelectorAll('[data-close-dialog]')) {
    closeBtn.addEventListener('click', () => byId(closeBtn.dataset.closeDialog).close());
  }

  for (const btn of document.querySelectorAll('#new-team-mode-seg [data-team-mode]')) {
    btn.addEventListener('click', () => setNewTeamMode(btn.dataset.teamMode));
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
      session.selectedTeamId = reply.team_id;
      resetDocsOverview();
      byId('create-team-dialog').close();
      await refresh(true);
      toast(newTeamMode === 'peer' ? '无分工 Team 已创建' : '分工 Team 已创建');
    } catch (err) {
      handleError(err, byId('create-team-error'));
    } finally {
      btn.disabled = false;
    }
  });

  byId('archive-team').addEventListener('click', e => runArchiveCurrentTeam(e.currentTarget));
  byId('peer-archive-team').addEventListener('click', e => runArchiveCurrentTeam(e.currentTarget));

  window.addEventListener('hashchange', () => {
    const launch = readLaunchToken();
    if (launch) {
      session.token = launch;
      refresh(true);
    }
  });
}

bindGlobalEvents();
initMembersEvents({ refresh });
initMessagesEvents({ refresh });
initWorkspaceEvents({ refresh, onWorkspaceChanged: resetDocsOverview });
initDocsStudioEvents({ refresh });

if (session.token) refresh(true);
setInterval(() => { if (session.unlocked) refresh(false); }, 3500);

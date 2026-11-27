import {
  PRODUCTS,
  ROLES,
  api,
  byId,
  currentTeam,
  el,
  flashButtonDone,
  handleError,
  isPeerTeam,
  isPendingStatus,
  session,
  setSelectOptions,
  shortenPath,
  toast,
} from './core.js';

let editingMemberId = null;
let bindingCtx = null;
let refreshFn = () => Promise.resolve();

export function renderSummary(team) {
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

export function renderGlobalAccessPanel(team) {
  const peer = isPeerTeam(team);
  const panel = byId('peer-settings-panel');
  panel.hidden = !peer;
  if (!peer) return;
  const ga = session.snapshot?.global_access || {};
  const bar = byId('global-access-bar');
  const items = [
    ['Antigravity 插件', Boolean(ga.antigravity_plugin)],
    ['Claude 全局 MCP', Boolean(ga.claude_mcp)],
    ['Codex 全局 MCP', Boolean(ga.codex_mcp)],
    ['Codex 验票钩子', Boolean(ga.codex_ticket_hook)],
    ['Codex 同轮引导钩子', Boolean(ga.codex_steer_hook)],
  ];
  bar.replaceChildren(...items.map(([label, ok]) => {
    const chip = el('span', 'hook-chip');
    chip.append(el('span', ok ? 'dot dot-ok' : 'dot dot-warn'), el('span', '', `${label} · ${ok ? '就绪' : '未配置'}`));
    return chip;
  }));
  const anyConfigured = Boolean(ga.claude_mcp || ga.codex_mcp || ga.codex_ticket_hook || ga.codex_steer_hook);
  byId('install-global-access').textContent = ga.ready ? '重新同步全局接入' : '配置全局接入';
  byId('remove-global-access').hidden = !anyConfigured;
}

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

export function openMemberDialog(team, slot = null) {
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

export function openBindDialog(team, slot) {
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

export function updateBindingLiveHint(team) {
  if (!bindingCtx || !byId('binding-dialog').open) return;
  const slot = (team.members || []).find(m => m.member === bindingCtx.member);
  if (!slot) return;
  const live = byId('binding-live-state');
  const connected = slot.codex_bound || slot.online || slot.window_bound || slot.desktop_bound || slot.acp_bound || slot.cli_bound;
  if (connected && !bindingCtx.wasBound) live.textContent = `${slot.name} 已接入 Hub`;
  else if (slot.online) live.textContent = `${slot.name} 在线`;
}

export function renderMembers(team) {
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
          await refreshFn();
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
        await refreshFn();
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

async function runGlobalAccessAction(btn, action, doneMsg) {
  btn.disabled = true;
  byId('global-access-error').textContent = '';
  try {
    await api('/control/global-access', { action });
    await refreshFn(true);
    toast(doneMsg, action === 'remove' ? 'warn' : 'ok');
  } catch (err) {
    handleError(err, byId('global-access-error'));
  } finally {
    btn.disabled = false;
  }
}

export function initMembersEvents({ refresh }) {
  refreshFn = refresh;

  for (const openAdd of [byId('open-new-member'), byId('empty-new-member')]) {
    openAdd.addEventListener('click', () => {
      const t = currentTeam();
      if (t) openMemberDialog(t, null);
    });
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
      await refreshFn();
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
      await refreshFn();
      toast('成员已移出', 'warn');
    } catch (err) {
      handleError(err, byId('member-form-error'));
    } finally {
      btn.disabled = false;
    }
  });

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
        await refreshFn();
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

  byId('install-global-access').addEventListener('click', e =>
    runGlobalAccessAction(e.currentTarget, 'install', '全局跨项目接入已配置'));
  byId('remove-global-access').addEventListener('click', e => {
    if (!confirm('确认移除用户级全局 Claude / Codex 接入配置？')) return;
    runGlobalAccessAction(e.currentTarget, 'remove', '已移除全局跨项目接入');
  });
}

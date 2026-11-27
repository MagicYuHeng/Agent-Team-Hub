import {
  PRODUCTS,
  ROLES,
  api,
  byId,
  currentTeam,
  el,
  flashButtonDone,
  handleError,
  setSelectOptions,
  toast,
} from './core.js';

let aiPreviewTeamId = null;
let aiPreviewWorktree = '';
let gitIdentityDirty = false;
let refreshFn = () => Promise.resolve();
let onWorkspaceReset = () => {};

export function resetGitIdentityDirty() {
  gitIdentityDirty = false;
}

function switchAiTab(tab) {
  byId('ai-tab-current').classList.toggle('active', tab === 'current');
  byId('ai-tab-logs').classList.toggle('active', tab === 'logs');
  byId('ai-pane-current').hidden = tab !== 'current';
  byId('ai-pane-logs').hidden = tab !== 'logs';
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

export function renderWorkspace(team, forceInputs = false) {
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

function currentWorkspacePayload(team) {
  return {
    action: 'workspace', team_id: team.id, workspace: byId('workspace-input').value,
    roles: Object.fromEntries(Object.keys(PRODUCTS).map(p => [p, byId('role-' + p).value])),
  };
}

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

async function runProjectHookAction(btn, action, doneMsg) {
  const team = currentTeam();
  if (!team) return;
  btn.disabled = true;
  byId('workspace-error').textContent = '';
  try {
    if (action === 'install') await api('/control/team', currentWorkspacePayload(team));
    await api('/control/project', { team_id: team.id, action });
    await refreshFn(true);
    toast(doneMsg);
  } catch (err) {
    handleError(err, byId('workspace-error'));
  } finally {
    btn.disabled = false;
  }
}

export function initWorkspaceEvents({ refresh, onWorkspaceChanged }) {
  refreshFn = refresh;
  onWorkspaceReset = onWorkspaceChanged || (() => {});

  for (const aiTab of document.querySelectorAll('[data-ai-tab]')) {
    aiTab.addEventListener('click', () => switchAiTab(aiTab.dataset.aiTab));
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
      onWorkspaceReset();
      await refreshFn(true);
      flashButtonDone(btn, '✓ 已保存');
      toast('路径已更新');
    } catch (err) {
      handleError(err, byId('workspace-error'));
    } finally {
      btn.disabled = false;
    }
  });

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
      await refreshFn(true);
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
      await refreshFn(true);
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
      await refreshFn(true);
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
      onWorkspaceReset();
      await refreshFn();
      toast('已刷新工作树');
    } catch (err) {
      handleError(err);
    } finally {
      btn.disabled = false;
    }
  });

  byId('install-hooks').addEventListener('click', e => runProjectHookAction(e.currentTarget, 'install', 'Hook 已部署'));
  byId('sync-hooks').addEventListener('click', e => runProjectHookAction(e.currentTarget, 'sync', 'Hook 已同步'));
  byId('remove-hooks').addEventListener('click', e => runProjectHookAction(e.currentTarget, 'remove', 'Hook 已撤销'));
}

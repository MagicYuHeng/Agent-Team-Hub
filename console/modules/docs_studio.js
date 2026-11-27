import {
  REV_SOURCE,
  ROLE_DOCS,
  api,
  byId,
  currentTeam,
  el,
  flashButtonDone,
  fmtTime,
  formatCommitSummary,
  handleError,
  setSelectOptions,
  shortenPath,
  syncIndicators,
  toast,
} from './core.js';
import { closeInlineDiff, initDocsDiffEvents, openInlineDiff } from './docs_diff.js';

let activeDocKey = 'agents';     // 'agents' | 'team_rules' | 'captain' | 'worker' | 'reviewer' | 'helper'
let activeAgentsMode = 'shared'; // 'shared' | 'worktree'
let activeDocWorktree = '';
let activeInspector = 'history'; // 'history' | 'similar' | 'append' | 'sync'
let currentDocData = null;
let agentsOverview = null;
let similarThreshold = 0.55;
let similarActionMode = 'remove'; // 'remove' | 'replace'
let similarMatches = [];
let previewRevId = null;
let refreshFn = () => Promise.resolve();

export function hasAgentsOverview() {
  return Boolean(agentsOverview);
}

export function resetDocsOverview() {
  agentsOverview = null;
}

export function setActiveDocWorktree(wt) {
  activeDocWorktree = wt || '';
}

export function selectAlertDoc(docKey, target = '') {
  activeDocKey = docKey;
  if (docKey === 'agents') {
    activeAgentsMode = 'worktree';
    activeDocWorktree = target || '';
  }
}

export function switchInspector(name) {
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

export function renderLeftRail(team) {
  const alerts = team.external_alerts || [];
  const alertWtSet = new Set(alerts.filter(a => a.doc_key === 'agents').map(a => a.target || ''));
  const alertRoleSet = new Set(alerts.filter(a => a.doc_key !== 'agents').map(a => a.doc_key));

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
        openInlineDiff(`漂移对比：基准 (${sg.canonical_worktree}) → ${wt.label}`, wt.drift_inline, { isAgents: true });
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

function readInspMatrix(container) {
  const write = Array.from(container.querySelectorAll('input[data-role="write"]:checked'), cb => cb.value);
  const commit = Array.from(container.querySelectorAll('input[data-role="commit"]:checked'), cb => cb.value)
    .filter(v => write.includes(v));
  return { write, commit };
}

function syncAppendButton() {
  const sel = readInspMatrix(byId('append-wt-matrix'));
  const btn = byId('append-apply-btn');
  btn.disabled = sel.write.length === 0;
  btn.textContent = sel.write.length ? `添加到 ${sel.write.length} 棵` : '批量添加';
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

export async function loadDocsStudio() {
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
    await refreshFn();
    await loadDocsStudio();
    toast(`已回档至 #${revId}`);
  } catch (err) {
    handleError(err, byId('doc-error'));
  }
}

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
    await refreshFn();
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

export function initDocsStudioEvents({ refresh }) {
  refreshFn = refresh;
  initDocsDiffEvents({ onHunkAction: triggerSimilarFromHunk });

  for (const segBtn of document.querySelectorAll('#inspector-tabs .seg-btn')) {
    segBtn.addEventListener('click', () => switchInspector(segBtn.dataset.insp));
  }

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
      openInlineDiff(
        `外改对比：Hub 基线 → 磁盘当前（${currentDocData.target || currentDocData.label}）`,
        currentDocData.external_diff,
        { isAgents: activeDocKey === 'agents' },
      );
    }
  });

  byId('doc-toggle-drift-diff').addEventListener('click', () => {
    const sg = agentsOverview?.shared_group || {};
    const wtInfo = (agentsOverview?.worktrees || []).find(w => w.relative === activeDocWorktree);
    if (wtInfo?.drift_inline) {
      openInlineDiff(`漂移对比：基准 (${sg.canonical_worktree}) → ${wtInfo.label}`, wtInfo.drift_inline, { isAgents: true });
    }
  });

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
      await refreshFn();
      await loadDocsStudio();
      toast('已采纳磁盘版本');
    } catch (err) {
      handleError(err, byId('doc-error'));
    } finally {
      btn.disabled = false;
    }
  });

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
      await refreshFn();
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
      await refreshFn();
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

  byId('similar-run-btn').addEventListener('click', runSimilarSearch);
  byId('similar-query-input').addEventListener('keydown', e => {
    if (e.key === 'Enter') { e.preventDefault(); runSimilarSearch(); }
  });

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
      for (const row of byId('similar-results-list').querySelectorAll('.insp-row')) {
        if (checkedIds.has(row.dataset.matchId)) row.classList.add('removing');
      }
      setTimeout(async () => {
        await refreshFn();
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
      await refreshFn();
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
}

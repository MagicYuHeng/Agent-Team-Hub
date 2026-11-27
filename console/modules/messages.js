import {
  PRODUCTS,
  STATUS_LABELS,
  api,
  byId,
  currentTeam,
  el,
  flashButtonDone,
  handleError,
  isIssueStatus,
  isPendingStatus,
  setSelectOptions,
  toast,
} from './core.js';

let msgFilter = 'all';
let msgMemberFilter = '';
let refreshFn = () => Promise.resolve();

export function resetMessageMemberFilter() {
  msgMemberFilter = '';
}

export function renderMessages(team) {
  const memberSelect = byId('filter-member');
  const names = (team.members || []).map(m => m.name);
  setSelectOptions(memberSelect, [['', '全部成员'], ...names.map(n => [n, n])], msgMemberFilter);

  const activity = team.activity || [];
  const filtered = activity.filter(item => {
    if (msgFilter === 'pending' && !isPendingStatus(item.status)) return false;
    if (msgFilter === 'accepted' && item.status !== 'accepted' && item.status !== 'delivered') return false;
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
      const statusClass = (rec.status === 'delivered' || rec.status === 'accepted') ? 'tag-soft tag-ok'
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
      if (rec.status === 'queued' || rec.status === 'submitting') {
        const cancelBtn = el('button', 'btn-danger-ghost btn-sm', '取消送达');
        cancelBtn.type = 'button';
        cancelBtn.addEventListener('click', async () => {
          cancelBtn.disabled = true;
          try {
            const reply = await api('/control/message/cancel', { team_id: team.id, id: rec.id });
            if (reply.cancel_requested) {
              await refreshFn();
              toast('已请求取消（等待外部投递结果）', 'warn');
              return;
            }
            det.style.opacity = '0';
            det.style.transform = 'translateX(12px)';
            setTimeout(async () => { await refreshFn(); toast('已取消送达', 'warn'); }, 180);
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

export function initMessagesEvents({ refresh }) {
  refreshFn = refresh;

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
    if (!team || !(team.members || []).length) {
      toast('暂无可发送成员', 'warn');
      return;
    }
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
      await refreshFn();
      toast('消息已入队');
    } catch (err) {
      handleError(err, byId('send-msg-error'));
    } finally {
      btn.disabled = false;
    }
  });
}

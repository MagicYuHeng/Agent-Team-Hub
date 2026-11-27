import { byId, el } from './core.js';

let activeDiffCtx = null; // { title, diff, isAgents }
let diffScope = 'changes'; // 'changes' | 'full'
let hunkActionHandler = () => {};

function rowPlainText(row) {
  return (row.segments || []).map(s => s.text).join('');
}

export function renderInlineDiffView() {
  const viewer = byId('doc-diff-view');
  const ta = byId('doc-textarea');
  if (!activeDiffCtx || !activeDiffCtx.diff) {
    viewer.hidden = true;
    ta.hidden = false;
    return;
  }
  const { title, diff, isAgents } = activeDiffCtx;
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

  const rendered = [];
  for (let i = 0; i < displayRows.length; i += 1) {
    const r = displayRows[i];
    if (r.type === 'gap') {
      rendered.push(el('div', 'diff-gap', `⋯ 略过 ${r.skipped} 行未改动 ⋯`));
      continue;
    }
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
          btn1.addEventListener('click', () => hunkActionHandler(oldStr, newStr, 'replace'));
          const btn2 = el('button', 'btn-link', '反查新文');
          btn2.type = 'button';
          btn2.title = '如果红绿方向相反，以绿行作旧文去查其他树并替换为红行';
          btn2.addEventListener('click', () => hunkActionHandler(newStr, oldStr, 'replace'));
          acts.append(btn1, btn2);
        } else if (delLines.length) {
          const btn = el('button', 'diff-hunk-btn', '查其他树旧文并移除');
          btn.type = 'button';
          btn.addEventListener('click', () => hunkActionHandler(delLines.join('\n'), '', 'remove'));
          acts.append(btn);
        } else if (addLines.length) {
          const btn = el('button', 'diff-hunk-btn', '批量加到其他树');
          btn.type = 'button';
          btn.addEventListener('click', () => hunkActionHandler('', addLines.join('\n'), 'append'));
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

export function openInlineDiff(title, diff, { isAgents = true } = {}) {
  activeDiffCtx = { title, diff, isAgents };
  renderInlineDiffView();
}

export function closeInlineDiff() {
  activeDiffCtx = null;
  renderInlineDiffView();
}

export function initDocsDiffEvents({ onHunkAction }) {
  hunkActionHandler = onHunkAction || (() => {});
  byId('doc-close-diff').addEventListener('click', closeInlineDiff);
  for (const btn of document.querySelectorAll('[data-diff-scope]')) {
    btn.addEventListener('click', () => {
      diffScope = btn.dataset.diffScope;
      for (const o of document.querySelectorAll('[data-diff-scope]')) o.classList.toggle('active', o === btn);
      renderInlineDiffView();
    });
  }
}

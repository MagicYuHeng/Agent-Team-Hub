"""Managed team documents (AGENTS.md, team_rules, role profiles), revision history, external-change detection, .ai read-only inspection, and safe AGENTS.md single-file git commit."""

import difflib
import re
import time
from collections import Counter
from pathlib import Path

from doc_engine.git_ops import (
    _git_dir,
    _parse_kv_header,
    commit_agents_file,
    detect_git_identity,
    inspect_worktree_ai,
)
from doc_engine.text_ops import (
    GROUP_LABELS,
    SHARED_END,
    SHARED_START,
    _bigram_jaccard,
    _build_inline_diff,
    _classify_worktree,
    _clause_similarity,
    _combine_independent_and_shared,
    _diff_segments,
    _extract_clause_units,
    _extract_independent,
    _extract_sections,
    _extract_shared,
    _insert_text_at_position,
    _intraline_segments,
    _normalize_plain,
    _normalize_skeleton,
    _replace_shared,
    sha256_text,
)
from teams import Conflict
from workspace import SOURCE, discover_worktrees, write_file


DOC_SPECS = {
    'agents': {'label': 'AGENTS.md', 'filename': 'AGENTS.md', 'has_default': False},
    'team_rules': {'label': '团队公共协作规则', 'filename': 'team_rules.md', 'has_default': True},
    'captain': {'label': 'Captain 角色规范', 'filename': 'captain.md', 'has_default': True},
    'worker': {'label': 'Worker 角色规范', 'filename': 'worker.md', 'has_default': True},
    'reviewer': {'label': 'Reviewer 角色规范', 'filename': 'reviewer.md', 'has_default': True},
    'helper': {'label': 'Helper 角色规范', 'filename': 'helper.md', 'has_default': True},
}


def _default_role_text(filename):
    path = SOURCE / 'roles' / filename
    return path.read_text(encoding='utf-8').strip() if path.is_file() else ''


def _doc_path(workspace, doc_key, worktree=''):
    root = Path(workspace).resolve()
    if doc_key == 'agents':
        wt = (root / worktree).resolve() if worktree else root
        if not wt.is_relative_to(root):
            raise ValueError('工作树路径超出总工作区范围。')
        return wt / 'AGENTS.md'
    spec = DOC_SPECS[doc_key]
    return root / '.team-hub' / 'roles' / spec['filename']


class DocStore:
    def __init__(self, store):
        self.store = store
        with store.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS doc_revisions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    team_id INTEGER NOT NULL,
                    doc_key TEXT NOT NULL,
                    target TEXT NOT NULL DEFAULT '',
                    content TEXT NOT NULL,
                    content_hash TEXT NOT NULL,
                    source TEXT NOT NULL,
                    note TEXT NOT NULL DEFAULT '',
                    created_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS doc_revisions_lookup
                    ON doc_revisions(team_id, doc_key, target, id DESC);
                CREATE TABLE IF NOT EXISTS doc_baselines (
                    team_id INTEGER NOT NULL,
                    doc_key TEXT NOT NULL,
                    target TEXT NOT NULL DEFAULT '',
                    content_hash TEXT NOT NULL,
                    updated_at REAL NOT NULL,
                    PRIMARY KEY (team_id, doc_key, target)
                );
            """)

    @staticmethod
    def _validate_doc_key(doc_key):
        if doc_key not in DOC_SPECS:
            raise ValueError('未知的受管文档类型。')
        return DOC_SPECS[doc_key]

    def _read_effective(self, workspace, doc_key, worktree=''):
        spec = self._validate_doc_key(doc_key)
        path = _doc_path(workspace, doc_key, worktree if doc_key == 'agents' else '')
        if path.is_file():
            return path.read_text(encoding='utf-8', errors='replace'), True, str(path)
        if spec['has_default']:
            return _default_role_text(spec['filename']), False, str(path)
        return '', False, str(path)

    def _record_revision(self, db, team_id, doc_key, target, content, source, note=''):
        digest = sha256_text(content)
        latest = db.execute(
            'SELECT content_hash FROM doc_revisions WHERE team_id=? AND doc_key=? AND target=? ORDER BY id DESC LIMIT 1',
            (team_id, doc_key, target),
        ).fetchone()
        if latest and latest['content_hash'] == digest:
            return digest
        db.execute(
            'INSERT INTO doc_revisions(team_id,doc_key,target,content,content_hash,source,note,created_at) VALUES (?,?,?,?,?,?,?,?)',
            (team_id, doc_key, target, content, digest, source, note, time.time()),
        )
        return digest

    def _set_baseline(self, db, team_id, doc_key, target, digest):
        db.execute(
            '''INSERT INTO doc_baselines(team_id,doc_key,target,content_hash,updated_at) VALUES (?,?,?,?,?)
               ON CONFLICT(team_id,doc_key,target) DO UPDATE SET content_hash=excluded.content_hash,updated_at=excluded.updated_at''',
            (team_id, doc_key, target, digest, time.time()),
        )

    def detect_external_changes(self, db, team):
        workspace = team.get('workspace') if isinstance(team, dict) else team['workspace']
        if not workspace or not Path(workspace).is_dir():
            return []
        team_id = team['id']
        team_name = team['name']
        alerts = []
        worktrees = discover_worktrees(workspace)

        checks = []
        for wt in worktrees:
            ag_file = Path(wt['path']) / 'AGENTS.md'
            if ag_file.is_file():
                checks.append(('agents', wt['relative'], ag_file))
        for doc_key, spec in DOC_SPECS.items():
            if doc_key == 'agents':
                continue
            override = Path(workspace) / '.team-hub' / 'roles' / spec['filename']
            if override.is_file():
                checks.append((doc_key, '', override))

        changed_db = False
        for doc_key, target, path in checks:
            try:
                text = path.read_text(encoding='utf-8', errors='replace')
            except OSError:
                continue
            digest = sha256_text(text)
            row = db.execute(
                'SELECT content_hash FROM doc_baselines WHERE team_id=? AND doc_key=? AND target=?',
                (team_id, doc_key, target),
            ).fetchone()
            if row is None:
                self._set_baseline(db, team_id, doc_key, target, digest)
                self._record_revision(db, team_id, doc_key, target, text, 'initial', '首次纳入管理基线')
                changed_db = True
            elif row['content_hash'] != digest:
                alerts.append({
                    'team_id': team_id,
                    'team_name': team_name,
                    'doc_key': doc_key,
                    'doc_label': DOC_SPECS[doc_key]['label'],
                    'target': target,
                    'disk_hash': digest[:12],
                })
        if changed_db:
            db.commit()
        return alerts

    def get_doc(self, team_id, doc_key, worktree=''):
        spec = self._validate_doc_key(doc_key)
        target = worktree if doc_key == 'agents' else ''
        with self.store.connect() as db:
            team = self.store.teams.get(db, team_id)
            if not team or not team['workspace']:
                raise Conflict('该 Team 尚未配置工作区。')
            content, on_disk, file_path = self._read_effective(team['workspace'], doc_key, target)
            digest = sha256_text(content)
            baseline = db.execute(
                'SELECT content_hash FROM doc_baselines WHERE team_id=? AND doc_key=? AND target=?',
                (team_id, doc_key, target),
            ).fetchone()
            external_changed = bool(on_disk and baseline and baseline['content_hash'] != digest)
            if on_disk and baseline is None:
                self._set_baseline(db, team_id, doc_key, target, digest)
                self._record_revision(db, team_id, doc_key, target, content, 'initial', '首次纳入管理基线')
                db.commit()
            revisions = [dict(row) for row in db.execute(
                '''SELECT id,source,note,content_hash,created_at,length(content) AS size
                   FROM doc_revisions WHERE team_id=? AND doc_key=? AND target=?
                   ORDER BY id DESC LIMIT 30''',
                (team_id, doc_key, target),
            )]
            extracted = _extract_shared(content) if doc_key == 'agents' else None
            has_shared_markers = bool(extracted)
            external_diff = None
            if external_changed and baseline:
                base_row = db.execute(
                    '''SELECT content FROM doc_revisions
                       WHERE team_id=? AND doc_key=? AND target=? AND content_hash=?
                       ORDER BY id DESC LIMIT 1''',
                    (team_id, doc_key, target, baseline['content_hash']),
                ).fetchone()
                if base_row is not None:
                    external_diff = _build_inline_diff(base_row['content'], content)
            return {
                'doc_key': doc_key,
                'label': spec['label'],
                'target': target,
                'path': file_path,
                'has_default': spec['has_default'],
                'is_override': on_disk if spec['has_default'] else False,
                'exists': on_disk,
                'external_changed': external_changed,
                'external_diff': external_diff,
                'has_shared_markers': has_shared_markers,
                'shared_line_count': len(extracted[2].splitlines()) if extracted else 0,
                'independent_content': _extract_independent(content) if has_shared_markers else content,
                'content': content,
                'revisions': revisions,
            }

    def get_revision(self, team_id, rev_id):
        with self.store.connect() as db:
            row = db.execute(
                'SELECT id,doc_key,target,content,content_hash,source,note,created_at FROM doc_revisions WHERE team_id=? AND id=?',
                (team_id, rev_id),
            ).fetchone()
            if not row:
                raise Conflict('历史版本不存在。')
            return dict(row)

    def accept_external(self, team_id, doc_key, worktree=''):
        self._validate_doc_key(doc_key)
        target = worktree if doc_key == 'agents' else ''
        with self.store.connect() as db:
            team = self.store.teams.get(db, team_id)
            if not team or not team['workspace']:
                raise Conflict('该 Team 尚未配置工作区。')
            content, on_disk, _ = self._read_effective(team['workspace'], doc_key, target)
            if not on_disk:
                raise Conflict('磁盘文件不存在。')
            digest = self._record_revision(db, team_id, doc_key, target, content, 'external', '采纳外部修改版本')
            self._set_baseline(db, team_id, doc_key, target, digest)
            db.commit()
            return {'accepted': True, 'hash': digest[:12]}

    def save_doc(self, team_id, doc_key, content, *, worktree='', sync_worktrees=None,
                 sync_mode='shared_if_marked', content_scope='full', auto_commit=False, commit_worktrees=None, note=''):
        spec = self._validate_doc_key(doc_key)
        if not isinstance(content, str):
            raise ValueError('文档正文必须为文本。')
        content_clean = content.replace('\r\n', '\n')
        if content_clean and not content_clean.endswith('\n'):
            content_clean += '\n'
        target = worktree if doc_key == 'agents' else ''
        with self.store.connect() as db:
            team = self.store.teams.get(db, team_id)
            if not team or not team['workspace']:
                raise Conflict('该 Team 尚未配置工作区。')
            workspace = Path(team['workspace']).resolve()
            if doc_key != 'agents':
                path = _doc_path(workspace, doc_key, '')
                # Archive any uncaptured external edit before overwriting.
                if path.is_file():
                    prev = path.read_text(encoding='utf-8', errors='replace')
                    b = db.execute('SELECT content_hash FROM doc_baselines WHERE team_id=? AND doc_key=? AND target=?',
                                   (team_id, doc_key, '')).fetchone()
                    if b and b['content_hash'] != sha256_text(prev):
                        self._record_revision(db, team_id, doc_key, '', prev, 'external', '覆盖前自动留存外部版本')
                write_file(path, content_clean.encode('utf-8'))
                digest = self._record_revision(db, team_id, doc_key, '', content_clean, 'hub', note or '管理台保存')
                self._set_baseline(db, team_id, doc_key, '', digest)
                # Bump all members' role_revision when team rules or matching role profile changed so hooks refresh.
                if doc_key == 'team_rules':
                    db.execute('UPDATE roster SET role_revision=role_revision+1 WHERE team_id=?', (team_id,))
                else:
                    db.execute(
                        "UPDATE roster SET role_revision=role_revision+1 WHERE team_id=? AND (role=? OR role LIKE ? OR role LIKE ?)",
                        (team_id, doc_key, f'{doc_key}+%', f'%+{doc_key}'),
                    )
                db.commit()
                return {'saved': True, 'synced': [str(path)], 'commits': {}}

            # AGENTS.md save + optional multi-worktree sync + optional single-file commit.
            targets = list(dict.fromkeys([target] + (list(sync_worktrees) if sync_worktrees else [])))
            self._require_git_identity_for_commit(team, targets, commit_worktrees, auto_commit=auto_commit)
            shared_info = _extract_shared(content_clean) if sync_mode == 'shared_if_marked' else None
            synced = []
            for wt_rel in targets:
                p = _doc_path(workspace, 'agents', wt_rel)
                if p.is_file():
                    prev = p.read_text(encoding='utf-8', errors='replace')
                    b = db.execute('SELECT content_hash FROM doc_baselines WHERE team_id=? AND doc_key=? AND target=?',
                                   (team_id, 'agents', wt_rel)).fetchone()
                    if b and b['content_hash'] != sha256_text(prev):
                        self._record_revision(db, team_id, 'agents', wt_rel, prev, 'external', '覆盖前自动留存外部版本')
                else:
                    prev = ''
                if content_scope == 'independent':
                    new_text = _combine_independent_and_shared(content_clean, prev)
                elif wt_rel == target:
                    new_text = content_clean
                elif shared_info and prev and _extract_shared(prev):
                    new_text = _replace_shared(prev, shared_info[2])
                else:
                    new_text = content_clean
                write_file(p, new_text.encode('utf-8'))
                digest = self._record_revision(
                    db, team_id, 'agents', wt_rel, new_text, 'hub',
                    note or ('管理台保存' if wt_rel == target else f'同步自 {target or "总工作区"}'),
                )
                self._set_baseline(db, team_id, 'agents', wt_rel, digest)
                synced.append(wt_rel)

            commits = {}
            if auto_commit:
                author_name = team['git_author_name'] or ''
                author_email = team['git_author_email'] or ''
                if not author_name or not author_email:
                    db.commit()
                    raise Conflict('文件已保存，但尚未配置 Team Git 提交身份，无法自动提交。')
                to_commit = targets if commit_worktrees is None else [w for w in commit_worktrees if w in targets]
                for wt_rel in to_commit:
                    wt_dir = (workspace / wt_rel) if wt_rel else workspace
                    commits[wt_rel or '.'] = commit_agents_file(wt_dir, author_name, author_email)
            db.commit()
            return {'saved': True, 'synced': synced, 'commits': commits}

    def reset_default(self, team_id, doc_key):
        spec = self._validate_doc_key(doc_key)
        if not spec['has_default']:
            raise Conflict('AGENTS.md 不设默认值，不支持恢复默认。')
        with self.store.connect() as db:
            team = self.store.teams.get(db, team_id)
            if not team or not team['workspace']:
                raise Conflict('该 Team 尚未配置工作区。')
            path = _doc_path(team['workspace'], doc_key, '')
            if path.is_file():
                prev = path.read_text(encoding='utf-8', errors='replace')
                self._record_revision(db, team_id, doc_key, '', prev, 'hub', '恢复默认前备份')
                path.unlink(missing_ok=True)
            default_text = _default_role_text(spec['filename'])
            digest = self._record_revision(db, team_id, doc_key, '', default_text, 'rollback', '恢复 Hub 默认模板')
            db.execute('DELETE FROM doc_baselines WHERE team_id=? AND doc_key=? AND target=?', (team_id, doc_key, ''))
            if doc_key == 'team_rules':
                db.execute('UPDATE roster SET role_revision=role_revision+1 WHERE team_id=?', (team_id,))
            else:
                db.execute(
                    "UPDATE roster SET role_revision=role_revision+1 WHERE team_id=? AND (role=? OR role LIKE ? OR role LIKE ?)",
                    (team_id, doc_key, f'{doc_key}+%', f'%+{doc_key}'),
                )
            db.commit()
            return {'reset': True, 'hash': digest[:12]}

    def rollback(self, team_id, rev_id, *, auto_commit=False):
        rev = self.get_revision(team_id, rev_id)
        return self.save_doc(
            team_id,
            rev['doc_key'],
            rev['content'],
            worktree=rev['target'],
            auto_commit=auto_commit,
            note=f"回档至历史版本 #{rev['id']}",
        )

    def _archive_external_if_needed(self, db, team_id, doc_key, target, path):
        if not path.is_file():
            return ''
        prev = path.read_text(encoding='utf-8', errors='replace')
        b = db.execute(
            'SELECT content_hash FROM doc_baselines WHERE team_id=? AND doc_key=? AND target=?',
            (team_id, doc_key, target),
        ).fetchone()
        if b and b['content_hash'] != sha256_text(prev):
            self._record_revision(db, team_id, doc_key, target, prev, 'external', '覆盖前自动留存外部版本')
        return prev

    @staticmethod
    def _require_git_identity_for_commit(team, targets, commit_worktrees=None, *, auto_commit=False):
        if commit_worktrees is None:
            need_commit = bool(auto_commit and targets)
        else:
            need_commit = any(w in targets for w in commit_worktrees)
        if not need_commit:
            return
        if not (team['git_author_name'] or '').strip() or not (team['git_author_email'] or '').strip():
            raise Conflict('尚未配置 Team Git 提交身份，已拦截本次修改。请先到「工作区」页配置 Git 身份，或取消勾选提交后再试。')

    def _commit_selected_worktrees(self, team, workspace, target_worktrees, commit_worktrees, message):
        selected = [w for w in (commit_worktrees or []) if w in target_worktrees]
        if not selected:
            return {}
        author_name = team['git_author_name'] or ''
        author_email = team['git_author_email'] or ''
        if not author_name or not author_email:
            raise Conflict('尚未配置 Team Git 提交身份，无法自动提交。')
        commits = {}
        for wt_rel in selected:
            wt_dir = (workspace / wt_rel) if wt_rel else workspace
            commits[wt_rel or '.'] = commit_agents_file(wt_dir, author_name, author_email, message=message)
        return commits

    def agents_overview(self, team_id):
        with self.store.connect() as db:
            team = self.store.teams.get(db, team_id)
            if not team or not team['workspace']:
                raise Conflict('该 Team 尚未配置工作区。')
            workspace = Path(team['workspace']).resolve()
            worktrees = discover_worktrees(workspace)

        items = []
        shared_bodies = {}
        shared_hashes = Counter()
        all_sections = []
        for wt in worktrees:
            ag_path = Path(wt['path']) / 'AGENTS.md'
            exists = ag_path.is_file()
            text = ag_path.read_text(encoding='utf-8', errors='replace') if exists else ''
            group = _classify_worktree(wt, text)
            extracted = _extract_shared(text) if exists else None
            shared_body = extracted[2] if extracted else ''
            shared_hash = sha256_text(shared_body)[:12] if extracted else ''
            sections = _extract_sections(text) if exists else []
            for sec in sections:
                if sec not in all_sections:
                    all_sections.append(sec)
            if group == 'shared' and extracted:
                shared_hashes[shared_hash] += 1
                shared_bodies[wt['relative']] = (shared_hash, shared_body)
            items.append({
                'relative': wt['relative'],
                'label': '总工作区' if wt['is_root'] else wt['relative'],
                'branch': wt['branch'],
                'is_root': wt['is_root'],
                'exists': exists,
                'group': group,
                'group_label': GROUP_LABELS[group],
                'has_shared': bool(extracted),
                'shared_hash': shared_hash,
                'sections': sections,
                'shared_status': 'na',
                'drift_diff': '',
            })

        canonical_hash = ''
        canonical_wt = ''
        canonical_shared = ''
        if shared_hashes:
            top_count = shared_hashes.most_common(1)[0][1]
            candidates = [h for h, cnt in shared_hashes.items() if cnt == top_count]
            # Prefer 'next' if it's tied at top count, else first shared worktree matching top count.
            if 'next' in shared_bodies and shared_bodies['next'][0] in candidates:
                canonical_wt = 'next'
                canonical_hash, canonical_shared = shared_bodies['next']
            else:
                for rel, (h, body) in shared_bodies.items():
                    if h in candidates:
                        canonical_wt, canonical_hash, canonical_shared = rel, h, body
                        break

        synced_count = 0
        drifted_count = 0
        for item in items:
            if item['group'] != 'shared':
                continue
            if item['shared_hash'] == canonical_hash:
                item['shared_status'] = 'synced'
                synced_count += 1
            else:
                item['shared_status'] = 'drifted'
                drifted_count += 1
                body = shared_bodies[item['relative']][1]
                diff_lines = list(difflib.unified_diff(
                    canonical_shared.splitlines(),
                    body.splitlines(),
                    fromfile=f'{canonical_wt} (基准共享块)',
                    tofile=f'{item["relative"]} (当前共享块)',
                    lineterm='',
                    n=2,
                ))
                item['drift_diff'] = '\n'.join(diff_lines[:80])
                item['drift_inline'] = _build_inline_diff(canonical_shared, body)

        return {
            'worktrees': items,
            'all_sections': all_sections,
            'shared_group': {
                'count': synced_count + drifted_count,
                'synced_count': synced_count,
                'drifted_count': drifted_count,
                'canonical_worktree': canonical_wt,
                'canonical_hash': canonical_hash,
                'canonical_shared': canonical_shared,
            },
        }

    def save_shared_block(self, team_id, shared_content, target_worktrees, commit_worktrees=None, note=''):
        if not isinstance(shared_content, str) or not shared_content.strip():
            raise ValueError('共享块正文不能为空。')
        targets = list(dict.fromkeys(target_worktrees or []))
        if not targets:
            raise ValueError('请至少选择一个目标工作树。')
        shared_clean = shared_content.replace('\r\n', '\n').strip()
        with self.store.connect() as db:
            team = self.store.teams.get(db, team_id)
            if not team or not team['workspace']:
                raise Conflict('该 Team 尚未配置工作区。')
            self._require_git_identity_for_commit(team, targets, commit_worktrees or [])
            workspace = Path(team['workspace']).resolve()
            synced = []
            for wt_rel in targets:
                p = _doc_path(workspace, 'agents', wt_rel)
                if not p.is_file():
                    raise Conflict(f'工作树「{wt_rel or "总工作区"}」不存在 AGENTS.md。')
                prev = self._archive_external_if_needed(db, team_id, 'agents', wt_rel, p)
                new_text = _replace_shared(prev, shared_clean)
                if new_text is None:
                    raise Conflict(f'工作树「{wt_rel or "总工作区"}」不含 <!-- SHARED-START --> 标记，已跳过以防覆盖独立规范。')
                write_file(p, new_text.encode('utf-8'))
                digest = self._record_revision(db, team_id, 'agents', wt_rel, new_text, 'hub', note or '共享块批量同步')
                self._set_baseline(db, team_id, 'agents', wt_rel, digest)
                synced.append(wt_rel)
            commits = self._commit_selected_worktrees(
                team, workspace, synced, commit_worktrees,
                message='docs(agents): sync shared AGENTS.md block via Team Hub',
            )
            db.commit()
            return {'saved': True, 'synced': synced, 'commits': commits}

    def find_similar_clauses(self, team_id, query, min_score=0.55, source_worktree=None):
        q = (query or '').strip()
        if len(_normalize_plain(q)) < 3:
            raise ValueError('请先选中或输入至少 3 个有效字符的约束文本。')
        threshold = max(0.25, min(0.98, float(min_score)))
        with self.store.connect() as db:
            team = self.store.teams.get(db, team_id)
            if not team or not team['workspace']:
                raise Conflict('该 Team 尚未配置工作区。')
            workspace = Path(team['workspace']).resolve()
            worktrees = discover_worktrees(workspace)

        matches = []
        for wt in worktrees:
            ag_path = Path(wt['path']) / 'AGENTS.md'
            if not ag_path.is_file():
                continue
            text = ag_path.read_text(encoding='utf-8', errors='replace')
            group = _classify_worktree(wt, text)
            units = _extract_clause_units(text)
            # Keep the highest-scoring unit per line to avoid duplicate line+sentence hits on the same line.
            best_by_line = {}
            for u in units:
                score = _clause_similarity(q, u['text'])
                if score < threshold:
                    continue
                prev = best_by_line.get(u['line_no'])
                if prev is None:
                    best_by_line[u['line_no']] = (score, u)
                else:
                    prev_score, prev_u = prev
                    # Prefer full line if scores are close (within 0.04) so removal cleanly removes the bullet line.
                    if u['kind'] == 'line' and prev_u['kind'] == 'sentence' and score >= prev_score - 0.04:
                        best_by_line[u['line_no']] = (score, u)
                    elif score > prev_score + (0.04 if prev_u['kind'] == 'line' else 0.0):
                        best_by_line[u['line_no']] = (score, u)

            for score, u in best_by_line.values():
                matches.append({
                    'id': f"{wt['relative']}:{u['start_offset']}:{u['end_offset']}",
                    'worktree': wt['relative'],
                    'worktree_label': '总工作区' if wt['is_root'] else wt['relative'],
                    'branch': wt['branch'],
                    'group': group,
                    'group_label': GROUP_LABELS[group],
                    'zone': u['zone'],
                    'line_no': u['line_no'],
                    'kind': u['kind'],
                    'start_offset': u['start_offset'],
                    'end_offset': u['end_offset'],
                    'text': u['text'],
                    'score': score,
                    'recommended': score >= 0.70,
                    'is_source': (source_worktree is not None and wt['relative'] == source_worktree),
                    'diff_segments': _diff_segments(q, u['text']),
                })

        matches.sort(key=lambda m: (-m['score'], m['worktree'], m['line_no']))
        return {'query': q, 'min_score': threshold, 'matches': matches}

    def batch_remove_clauses(self, team_id, items, commit_worktrees=None, note=''):
        if not isinstance(items, list) or not items:
            raise ValueError('请至少选择一条要移除的约束。')
        by_wt = {}
        for item in items:
            wt_rel = item.get('worktree', '') or ''
            by_wt.setdefault(wt_rel, []).append({
                'start': int(item['start_offset']),
                'end': int(item['end_offset']),
                'expected': item.get('expected_text', ''),
            })

        with self.store.connect() as db:
            team = self.store.teams.get(db, team_id)
            if not team or not team['workspace']:
                raise Conflict('该 Team 尚未配置工作区。')
            self._require_git_identity_for_commit(team, list(by_wt.keys()), commit_worktrees or [])
            workspace = Path(team['workspace']).resolve()
            updated = []
            removed_count = 0
            for wt_rel, spans in by_wt.items():
                p = _doc_path(workspace, 'agents', wt_rel)
                if not p.is_file():
                    raise Conflict(f'工作树「{wt_rel or "总工作区"}」不存在 AGENTS.md。')
                prev = self._archive_external_if_needed(db, team_id, 'agents', wt_rel, p)
                text = prev.replace('\r\n', '\n')
                spans_sorted = sorted(spans, key=lambda s: s['start'], reverse=True)
                # Validate spans do not overlap and still match expected text.
                last_start = len(text) + 1
                for sp in spans_sorted:
                    st, en = sp['start'], sp['end']
                    if st < 0 or en <= st or en > len(text) or en > last_start:
                        raise Conflict(f'工作树「{wt_rel or "总工作区"}」的目标区间无效或重叠，请重新检索后再试。')
                    if sp['expected'] and text[st:en] != sp['expected']:
                        raise Conflict(f'工作树「{wt_rel or "总工作区"}」的 AGENTS.md 内容已变化，请重新检索。')
                    last_start = st

                for sp in spans_sorted:
                    st, en = sp['start'], sp['end']
                    line_start = text.rfind('\n', 0, st) + 1
                    line_end = text.find('\n', en)
                    if line_end == -1:
                        line_end = len(text)
                    rem_line = (text[line_start:st] + text[en:line_end]).strip()
                    if not rem_line or re.match(r'^(?:[-*+]|\d+[.)])$', rem_line):
                        cut_end = line_end + 1 if line_end < len(text) else line_end
                        text = text[:line_start] + text[cut_end:]
                    else:
                        text = text[:st] + text[en:]
                    removed_count += 1

                text = re.sub(r'\n{3,}', '\n\n', text).strip() + '\n'
                write_file(p, text.encode('utf-8'))
                digest = self._record_revision(
                    db, team_id, 'agents', wt_rel, text, 'hub', note or '跨工作树批量移除约束',
                )
                self._set_baseline(db, team_id, 'agents', wt_rel, digest)
                updated.append(wt_rel)

            commits = self._commit_selected_worktrees(
                team, workspace, updated, commit_worktrees,
                message='docs(agents): remove matched constraint via Team Hub',
            )
            db.commit()
            return {'removed_count': removed_count, 'updated_worktrees': updated, 'commits': commits}

    def batch_replace_clauses(self, team_id, items, replacement, commit_worktrees=None, note=''):
        if not isinstance(items, list) or not items:
            raise ValueError('请至少选择一条要替换的约束。')
        if not isinstance(replacement, str) or not replacement.strip():
            raise ValueError('请输入替换后的新正文。')
        rep_clean = replacement.replace('\r\n', '\n').strip()
        by_wt = {}
        for item in items:
            wt_rel = item.get('worktree', '') or ''
            by_wt.setdefault(wt_rel, []).append({
                'start': int(item['start_offset']),
                'end': int(item['end_offset']),
                'expected': item.get('expected_text', ''),
            })

        with self.store.connect() as db:
            team = self.store.teams.get(db, team_id)
            if not team or not team['workspace']:
                raise Conflict('该 Team 尚未配置工作区。')
            self._require_git_identity_for_commit(team, list(by_wt.keys()), commit_worktrees or [])
            workspace = Path(team['workspace']).resolve()
            updated = []
            replaced_count = 0
            for wt_rel, spans in by_wt.items():
                p = _doc_path(workspace, 'agents', wt_rel)
                if not p.is_file():
                    raise Conflict(f'工作树「{wt_rel or "总工作区"}」不存在 AGENTS.md。')
                prev = self._archive_external_if_needed(db, team_id, 'agents', wt_rel, p)
                text = prev.replace('\r\n', '\n')
                spans_sorted = sorted(spans, key=lambda s: s['start'], reverse=True)
                last_start = len(text) + 1
                for sp in spans_sorted:
                    st, en = sp['start'], sp['end']
                    if st < 0 or en <= st or en > len(text) or en > last_start:
                        raise Conflict(f'工作树「{wt_rel or "总工作区"}」的目标区间无效或重叠，请重新检索后再试。')
                    if sp['expected'] and text[st:en] != sp['expected']:
                        raise Conflict(f'工作树「{wt_rel or "总工作区"}」的 AGENTS.md 内容已变化，请重新检索。')
                    last_start = st

                for sp in spans_sorted:
                    st, en = sp['start'], sp['end']
                    text = text[:st] + rep_clean + text[en:]
                    replaced_count += 1

                text = re.sub(r'\n{3,}', '\n\n', text).strip() + '\n'
                write_file(p, text.encode('utf-8'))
                digest = self._record_revision(
                    db, team_id, 'agents', wt_rel, text, 'hub', note or '跨工作树批量替换约束',
                )
                self._set_baseline(db, team_id, 'agents', wt_rel, digest)
                updated.append(wt_rel)

            commits = self._commit_selected_worktrees(
                team, workspace, updated, commit_worktrees,
                message='docs(agents): replace matched constraint via Team Hub',
            )
            db.commit()
            return {'replaced_count': replaced_count, 'updated_worktrees': updated, 'commits': commits}

    def preview_batch_append(self, team_id, text, worktrees, position='before_shared', section_heading=''):
        addition = (text or '').strip()
        if not addition:
            raise ValueError('请输入要添加的约束文本。')
        targets = list(dict.fromkeys(worktrees or []))
        if not targets:
            raise ValueError('请至少选择一个目标工作树。')
        with self.store.connect() as db:
            team = self.store.teams.get(db, team_id)
            if not team or not team['workspace']:
                raise Conflict('该 Team 尚未配置工作区。')
            workspace = Path(team['workspace']).resolve()
            all_wts = {wt['relative']: wt for wt in discover_worktrees(workspace)}

        previews = []
        for wt_rel in targets:
            wt = all_wts.get(wt_rel)
            if not wt:
                continue
            p = _doc_path(workspace, 'agents', wt_rel)
            exists = p.is_file()
            existing = p.read_text(encoding='utf-8', errors='replace') if exists else ''
            group = _classify_worktree(wt, existing)
            best_score = 0.0
            best_unit = None
            if exists:
                for u in _extract_clause_units(existing):
                    sc = _clause_similarity(addition, u['text'])
                    if sc > best_score:
                        best_score = sc
                        best_unit = u
            duplicate = best_score >= 0.78
            _, resolved_label = _insert_text_at_position(existing, addition, position, section_heading)
            previews.append({
                'worktree': wt_rel,
                'worktree_label': '总工作区' if wt['is_root'] else wt_rel,
                'branch': wt['branch'],
                'group': group,
                'group_label': GROUP_LABELS[group],
                'exists': exists,
                'resolved_position': resolved_label,
                'duplicate': duplicate,
                'duplicate_score': best_score if duplicate else 0.0,
                'duplicate_line': best_unit['line_no'] if (duplicate and best_unit) else 0,
                'duplicate_text': best_unit['text'] if (duplicate and best_unit) else '',
                'default_apply': not duplicate,
            })
        return {'previews': previews}

    def batch_append(self, team_id, text, worktrees, position='before_shared', section_heading='',
                     commit_worktrees=None, note=''):
        addition = (text or '').strip()
        if not addition:
            raise ValueError('请输入要添加的约束文本。')
        targets = list(dict.fromkeys(worktrees or []))
        if not targets:
            raise ValueError('请至少选择一个要写入的工作树。')
        with self.store.connect() as db:
            team = self.store.teams.get(db, team_id)
            if not team or not team['workspace']:
                raise Conflict('该 Team 尚未配置工作区。')
            self._require_git_identity_for_commit(team, targets, commit_worktrees or [])
            workspace = Path(team['workspace']).resolve()
            updated = []
            for wt_rel in targets:
                p = _doc_path(workspace, 'agents', wt_rel)
                prev = self._archive_external_if_needed(db, team_id, 'agents', wt_rel, p)
                new_text, _ = _insert_text_at_position(prev, addition, position, section_heading)
                write_file(p, new_text.encode('utf-8'))
                digest = self._record_revision(
                    db, team_id, 'agents', wt_rel, new_text, 'hub', note or '跨工作树批量添加约束',
                )
                self._set_baseline(db, team_id, 'agents', wt_rel, digest)
                updated.append(wt_rel)

            commits = self._commit_selected_worktrees(
                team, workspace, updated, commit_worktrees,
                message='docs(agents): append constraint via Team Hub',
            )
            db.commit()
            return {'updated_worktrees': updated, 'commits': commits}

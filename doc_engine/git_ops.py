"""Git identity detection, safe single-file AGENTS.md commit, and read-only .ai inspection."""

import json
import re
import subprocess
from pathlib import Path

from teams import Conflict


def detect_git_identity(workspace):
    result = {'gh': None, 'git': None}
    try:
        proc = subprocess.run(
            ['gh', 'api', 'user', '--jq', '{name: (.name // .login), login: .login, id: .id, email: .email}'],
            capture_output=True, text=True, encoding='utf-8', timeout=5,
        )
        if proc.returncode == 0 and proc.stdout.strip():
            data = json.loads(proc.stdout.strip())
            login = data.get('login') or ''
            uid = data.get('id')
            name = (data.get('name') or login).strip()
            email = (data.get('email') or (f'{uid}+{login}@users.noreply.github.com' if uid and login else '')).strip()
            if name and email:
                result['gh'] = {'name': name, 'email': email, 'login': login}
    except (OSError, ValueError, subprocess.SubprocessError):
        pass

    if workspace and Path(workspace).is_dir():
        try:
            n = subprocess.run(['git', '-C', str(workspace), 'config', 'user.name'],
                               capture_output=True, text=True, encoding='utf-8', timeout=3)
            e = subprocess.run(['git', '-C', str(workspace), 'config', 'user.email'],
                               capture_output=True, text=True, encoding='utf-8', timeout=3)
            if n.returncode == 0 and e.returncode == 0 and n.stdout.strip() and e.stdout.strip():
                result['git'] = {'name': n.stdout.strip(), 'email': e.stdout.strip()}
        except (OSError, subprocess.SubprocessError):
            pass
    return result


def _git_dir(wt_path):
    proc = subprocess.run(
        ['git', '-C', str(wt_path), 'rev-parse', '--path-format=absolute', '--git-dir'],
        capture_output=True, text=True, encoding='utf-8', timeout=4,
    )
    if proc.returncode != 0 or not proc.stdout.strip():
        return None
    return Path(proc.stdout.strip())


def commit_agents_file(wt_path, author_name, author_email, message='docs(agents): update AGENTS.md via Team Hub'):
    wt = Path(wt_path).resolve()
    gdir = _git_dir(wt)
    if not gdir:
        return {'status': 'skipped', 'reason': '非 Git 工作树'}
    in_progress = ('MERGE_HEAD', 'REBASE_HEAD', 'CHERRY_PICK_HEAD', 'REVERT_HEAD', 'rebase-merge', 'rebase-apply')
    for marker in in_progress:
        if (gdir / marker).exists():
            return {'status': 'skipped', 'reason': f'存在进行中的 Git 操作（{marker}）'}

    # Ensure AGENTS.md is tracked and modified without unmerged conflict.
    tracked = subprocess.run(['git', '-C', str(wt), 'ls-files', '--error-unmatch', 'AGENTS.md'],
                             capture_output=True, text=True, encoding='utf-8', timeout=4)
    if tracked.returncode != 0:
        return {'status': 'skipped', 'reason': 'AGENTS.md 未被 Git 跟踪'}
    unmerged = subprocess.run(['git', '-C', str(wt), 'ls-files', '--unmerged', '--', 'AGENTS.md'],
                              capture_output=True, text=True, encoding='utf-8', timeout=4)
    if unmerged.stdout.strip():
        return {'status': 'skipped', 'reason': 'AGENTS.md 存在未解决的合并冲突'}
    staged_agents = subprocess.run(['git', '-C', str(wt), 'diff', '--cached', '--name-only', '--', 'AGENTS.md'],
                                   capture_output=True, text=True, encoding='utf-8', timeout=4)
    if staged_agents.stdout.strip():
        return {'status': 'skipped', 'reason': '暂存区已有未提交的 AGENTS.md 改动，避免覆盖'}
    diff = subprocess.run(['git', '-C', str(wt), 'diff', '--name-only', '--', 'AGENTS.md'],
                          capture_output=True, text=True, encoding='utf-8', timeout=4)
    if not diff.stdout.strip():
        return {'status': 'unchanged', 'reason': 'AGENTS.md 相对 HEAD 无变更'}

    author = f'{author_name} <{author_email}>'
    commit = subprocess.run(
        ['git', '-C', str(wt), '-c', f'user.name={author_name}', '-c', f'user.email={author_email}',
         'commit', f'--author={author}', '--only', '-m', message, '--', 'AGENTS.md'],
        capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=15,
    )
    if commit.returncode != 0:
        detail = (commit.stderr or commit.stdout or 'git commit 失败').strip()
        return {'status': 'failed', 'reason': detail[:240]}
    sha = subprocess.run(['git', '-C', str(wt), 'rev-parse', '--short=7', 'HEAD'],
                         capture_output=True, text=True, encoding='utf-8', timeout=4).stdout.strip()
    return {'status': 'committed', 'commit': sha}


def _parse_kv_header(text):
    meta = {}
    title = ''
    for line in text.splitlines()[:25]:
        s = line.strip()
        if not title and s.startswith('# '):
            title = s[2:].strip()
        match = re.match(r'^([a-z_]+):\s*(.+)$', s)
        if match:
            meta[match.group(1)] = match.group(2).strip()
    return title, meta


def inspect_worktree_ai(workspace, worktree='', log_path=None):
    if not workspace:
        raise Conflict('该 Team 尚未配置工作区。')
    root = Path(workspace).resolve()
    wt = (root / worktree).resolve() if worktree else root
    if not wt.is_relative_to(root) or not wt.is_dir():
        raise Conflict('工作树路径无效或不存在。')
    ai_dir = wt / '.ai'
    current_file = ai_dir / 'CURRENT.md'
    if current_file.is_file():
        text = current_file.read_text(encoding='utf-8', errors='replace')
        title, meta = _parse_kv_header(text)
        memos = []
        in_memos = False
        for line in text.splitlines():
            s = line.strip()
            if s.lower().startswith('## open memos'):
                in_memos = True
                continue
            if in_memos:
                if s.startswith('## '):
                    break
                for m in re.findall(r'MEM-\d+', s):
                    if m not in memos:
                        memos.append(m)
        current_data = {
            'exists': True,
            'title': title,
            'meta': meta,
            'open_memos': memos,
            'content': text[:30000],
        }
    else:
        current_data = {'exists': False}

    handoffs_dir = ai_dir / 'handoffs'
    logs = []
    if handoffs_dir.is_dir():
        files = sorted(
            [p for p in handoffs_dir.rglob('*.md') if p.is_file()],
            key=lambda p: p.name,
            reverse=True,
        )
        for p in files[:10]:
            rel = p.relative_to(ai_dir).as_posix()
            try:
                raw = p.read_text(encoding='utf-8', errors='replace')
            except OSError:
                continue
            title, meta = _parse_kv_header(raw)
            logs.append({
                'path': rel,
                'name': p.name,
                'title': title or p.stem,
                'agent': meta.get('agent', ''),
                'model': meta.get('model', ''),
                'branch': meta.get('branch', p.parent.name),
                'work_commit': (meta.get('work_commit', '') or '')[:8],
                'created_at': meta.get('created_at', ''),
                'status': meta.get('status', ''),
            })

    selected_log = None
    if log_path:
        clean = str(log_path).replace('\\', '/').lstrip('/')
        target = (ai_dir / clean).resolve()
        if target.is_relative_to(ai_dir) and target.is_file() and target.suffix == '.md':
            selected_log = {
                'path': clean,
                'content': target.read_text(encoding='utf-8', errors='replace')[:30000],
            }

    return {
        'worktree': worktree,
        'current': current_data,
        'logs': logs,
        'selected_log': selected_log,
    }

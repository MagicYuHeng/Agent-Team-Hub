"""Control-plane HTTP route handlers (/control/*)."""

from codex_native import locate_thread
from docs_store import detect_git_identity, inspect_worktree_ai
from server.global_access import configure_global_access
from server.http_utils import required_text
from teams import Conflict, team_mode


def handle_control_doc(handler, body):
    team_id = body.get('team_id')
    if type(team_id) is not int:
        raise ValueError('请选择 Team。')
    with handler.server.store.connect() as db:
        doc_team = handler.server.store.teams.get(db, team_id)
    if doc_team and team_mode(doc_team) == 'peer':
        raise Conflict('无分工 Team 不管理工作区规范文档。')
    action = body.get('action')
    docs = handler.server.store.docs
    if action == 'get':
        return docs.get_doc(team_id, required_text(body, 'doc_key', 32), body.get('worktree', '') or '')
    if action == 'revision':
        rev_id = body.get('revision_id')
        if type(rev_id) is not int:
            raise ValueError('请选择历史版本。')
        return docs.get_revision(team_id, rev_id)
    if action == 'save':
        return docs.save_doc(
            team_id,
            required_text(body, 'doc_key', 32),
            body.get('content', ''),
            worktree=body.get('worktree', '') or '',
            sync_worktrees=body.get('sync_worktrees'),
            sync_mode=body.get('sync_mode', 'shared_if_marked'),
            content_scope=body.get('content_scope', 'full') or 'full',
            auto_commit=bool(body.get('auto_commit') or body.get('commit_worktrees')),
            commit_worktrees=body.get('commit_worktrees'),
            note=str(body.get('note', '') or '')[:120],
        )
    if action == 'reset':
        return docs.reset_default(team_id, required_text(body, 'doc_key', 32))
    if action == 'rollback':
        rev_id = body.get('revision_id')
        if type(rev_id) is not int:
            raise ValueError('请选择历史版本。')
        return docs.rollback(team_id, rev_id, auto_commit=bool(body.get('auto_commit')))
    if action == 'accept_external':
        return docs.accept_external(
            team_id, required_text(body, 'doc_key', 32), body.get('worktree', '') or '')
    if action == 'agents_overview':
        return docs.agents_overview(team_id)
    if action == 'save_shared':
        return docs.save_shared_block(
            team_id,
            body.get('content', ''),
            body.get('target_worktrees') or [],
            commit_worktrees=body.get('commit_worktrees') or [],
            note=str(body.get('note', '') or '')[:120],
        )
    if action == 'search_similar':
        return docs.find_similar_clauses(
            team_id,
            body.get('query', ''),
            min_score=body.get('min_score', 0.55),
            source_worktree=body.get('source_worktree'),
        )
    if action == 'batch_remove':
        return docs.batch_remove_clauses(
            team_id,
            body.get('items') or [],
            commit_worktrees=body.get('commit_worktrees') or [],
            note=str(body.get('note', '') or '')[:120],
        )
    if action == 'batch_replace':
        return docs.batch_replace_clauses(
            team_id,
            body.get('items') or [],
            body.get('replacement', ''),
            commit_worktrees=body.get('commit_worktrees') or [],
            note=str(body.get('note', '') or '')[:120],
        )
    if action == 'preview_append':
        return docs.preview_batch_append(
            team_id,
            body.get('text', ''),
            body.get('worktrees') or [],
            position=body.get('position', 'before_shared') or 'before_shared',
            section_heading=body.get('section_heading', '') or '',
        )
    if action == 'batch_append':
        return docs.batch_append(
            team_id,
            body.get('text', ''),
            body.get('worktrees') or [],
            position=body.get('position', 'before_shared') or 'before_shared',
            section_heading=body.get('section_heading', '') or '',
            commit_worktrees=body.get('commit_worktrees') or [],
            note=str(body.get('note', '') or '')[:120],
        )
    raise ValueError('未知文档操作。')


def handle_control_post(handler):
    path = handler.path
    server = handler.server
    store = server.store
    if path == '/control/pause':
        body = handler.read_json()
        member = required_text(body, 'member', 48)
        if type(body.get('paused')) is not bool:
            raise ValueError('Expected a boolean paused value.')
        found = store.set_paused(member, body['paused'])
        handler.respond(200 if found else 404, {'updated': found})
        return True
    if path == '/control/team':
        with server.members_lock:
            body = handler.read_json()
            result = store.teams.edit(body.get('action'), body)
        handler.respond(200, result)
        return True
    if path == '/control/project':
        body = handler.read_json()
        with server.members_lock:
            result = store.projects.change(body.get('team_id'), body.get('action'), server.server_port)
        handler.respond(200, result)
        return True
    if path == '/control/global-access':
        body = handler.read_json()
        with server.members_lock:
            result = configure_global_access(body.get('action'), server.server_port)
        handler.respond(200, result)
        return True
    if path == '/control/worktrees':
        body = handler.read_json()
        team_id = body.get('team_id')
        if type(team_id) is not int:
            raise ValueError('请选择 Team。')
        with store.connect() as db:
            team = store.teams.get(db, team_id)
            if not team:
                raise Conflict('该 Team 不可用。')
            worktrees = store.projects.worktree_status(db, team)
        handler.respond(200, {'worktrees': worktrees})
        return True
    if path == '/control/worktree-ai':
        body = handler.read_json()
        team_id = body.get('team_id')
        if type(team_id) is not int:
            raise ValueError('请选择 Team。')
        with store.connect() as db:
            team = store.teams.get(db, team_id)
            if not team:
                raise Conflict('该 Team 不可用。')
            workspace = team['workspace']
        result = inspect_worktree_ai(workspace, body.get('worktree', '') or '', body.get('log_path'))
        handler.respond(200, result)
        return True
    if path == '/control/message/cancel':
        body = handler.read_json()
        team_id = body.get('team_id')
        if type(team_id) is not int:
            raise ValueError('请选择 Team。')
        result = store.messages.cancel(team_id, required_text(body, 'id', 128))
        handler.respond(200, result)
        return True
    if path == '/control/message/send':
        body = handler.read_json()
        team_id = body.get('team_id')
        if type(team_id) is not int:
            raise ValueError('请选择 Team。')
        result = store.messages.send_operator(
            team_id, required_text(body, 'to', 48), required_text(body, 'text', 12000),
            mode=body.get('mode', 'auto'),
        )
        handler.respond(202, result)
        return True
    if path == '/control/git-identity/detect':
        body = handler.read_json()
        team_id = body.get('team_id')
        workspace = None
        if type(team_id) is int:
            with store.connect() as db:
                team = store.teams.get(db, team_id)
                workspace = team['workspace'] if team else None
        handler.respond(200, detect_git_identity(workspace))
        return True
    if path == '/control/doc':
        body = handler.read_json()
        handler.respond(200, handle_control_doc(handler, body))
        return True
    if path == '/control/antigravity':
        body = handler.read_json()
        with server.members_lock:
            code = store.teams.prepare_antigravity(
                body.get('team_id'), required_text(body, 'member', 48), body.get('label', ''))
        handler.respond(200, {'code': code})
        return True
    if path == '/control/acp':
        body = handler.read_json()
        with server.members_lock:
            code = store.teams.prepare_acp(
                body.get('team_id'), required_text(body, 'member', 48), body.get('label', ''))
        handler.respond(200, {'code': code})
        return True
    if path == '/control/agy-cli':
        body = handler.read_json()
        with server.members_lock:
            code = store.teams.prepare_agy_cli(
                body.get('team_id'), required_text(body, 'member', 48), body.get('label', ''))
        handler.respond(200, {'code': code})
        return True
    if path == '/control/rebind':
        member = required_text(handler.read_json(), 'member', 48)
        with server.members_lock:
            code = store.teams.prepare_binding(member)
        handler.respond(200, {'code': code})
        return True
    if path == '/control/codex-bind':
        body = handler.read_json()
        member = required_text(body, 'member', 48)
        thread, rollout_path = locate_thread(required_text(body, 'thread_id', 36))
        with server.members_lock:
            store.bind_codex(member, thread, rollout_path)
            server.members.pop(member, None)
        handler.respond(200, {'bound': True, 'thread_id': thread})
        return True
    return False

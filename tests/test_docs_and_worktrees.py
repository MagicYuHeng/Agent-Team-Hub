import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
import uuid
from contextlib import closing
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from hub import HubServer, TaskStore
from integrations.antigravity.team_relay import handle as handle_antigravity_tool, open_result
from probes.desktop_member import DesktopMember, hub_token as desktop_hub_token
from project_agent import request as agent_request

try:
    from tests._helpers import HubTestCase, TOKEN
except ImportError:
    from _helpers import HubTestCase, TOKEN

class DocsAndWorktreesTest(HubTestCase):
    def test_multi_worktree_discovery_sync_and_removal(self):
        repo = self.root / 'multi-repo'
        repo.mkdir()
        git_env = os.environ | {
            'GIT_AUTHOR_NAME': 'Test', 'GIT_AUTHOR_EMAIL': 'test@example.com',
            'GIT_COMMITTER_NAME': 'Test', 'GIT_COMMITTER_EMAIL': 'test@example.com',
        }
        subprocess.run(['git', '-C', str(repo), 'init', '-b', 'main'], check=True, capture_output=True)
        (repo / 'README.md').write_text('hello\n', encoding='utf-8')
        subprocess.run(['git', '-C', str(repo), 'add', 'README.md'], check=True, capture_output=True)
        subprocess.run(['git', '-C', str(repo), 'commit', '-m', 'init'], check=True, capture_output=True, env=git_env)
        wt_next = repo / 'next'
        subprocess.run(['git', '-C', str(repo), 'worktree', 'add', '-b', 'next', str(wt_next)],
                       check=True, capture_output=True)
        (wt_next / '.ai').mkdir()
        (wt_next / '.ai' / 'CURRENT.md').write_text('# Next\n', encoding='utf-8')

        team = self.api('/control/team', {'action': 'create', 'name': 'WTTeam', 'workspace': str(repo)})['team_id']
        listed = self.api('/control/worktrees', {'team_id': team})['worktrees']
        self.assertEqual([w['relative'] for w in listed], ['', 'next'])
        self.assertTrue(listed[1]['has_ai'])
        self.assertEqual(listed[1]['branch'], 'next')

        # Install deploys consistently across root and child worktree.
        self.api('/control/project', {'team_id': team, 'action': 'install'})
        self.assertTrue((repo / '.codex' / 'hooks.json').is_file())
        self.assertTrue((wt_next / '.codex' / 'hooks.json').is_file())
        self.assertTrue((wt_next / '.mcp.json').is_file())
        synced = self.api('/control/worktrees', {'team_id': team})['worktrees']
        self.assertTrue(all(w['deployed'] and w['up_to_date'] for w in synced))

        # Add a second worktree and sync incrementally.
        wt_voice = repo / 'voice-pipeline'
        subprocess.run(['git', '-C', str(repo), 'worktree', 'add', '-b', 'voice', str(wt_voice)],
                       check=True, capture_output=True)
        before_sync = {w['relative']: w for w in self.api('/control/worktrees', {'team_id': team})['worktrees']}
        self.assertFalse(before_sync['voice-pipeline']['deployed'])
        self.api('/control/project', {'team_id': team, 'action': 'sync'})
        after_sync = {w['relative']: w for w in self.api('/control/worktrees', {'team_id': team})['worktrees']}
        self.assertTrue(after_sync['voice-pipeline']['deployed'])
        self.assertTrue((wt_voice / '.claude' / 'settings.local.json').is_file())

        # Remove cleans all deployed worktrees cleanly.
        self.api('/control/project', {'team_id': team, 'action': 'remove'})
        for wt in (repo, wt_next, wt_voice):
            self.assertFalse((wt / '.codex' / 'hooks.json').exists())
            self.assertFalse((wt / '.mcp.json').exists())

    def test_docs_history_external_alerts_git_sync_ai_preview_and_message_controls(self):
        repo = self.root / 'docs-repo'
        repo.mkdir()
        git_env = os.environ | {
            'GIT_AUTHOR_NAME': 'Test', 'GIT_AUTHOR_EMAIL': 'test@example.com',
            'GIT_COMMITTER_NAME': 'Test', 'GIT_COMMITTER_EMAIL': 'test@example.com',
        }
        subprocess.run(['git', '-C', str(repo), 'init', '-b', 'main'], check=True, capture_output=True)
        root_agents = '# Root\n\n<!-- SHARED-START -->\nshared v1\n<!-- SHARED-END -->\n\nRoot tail\n'
        (repo / 'AGENTS.md').write_text(root_agents, encoding='utf-8')
        subprocess.run(['git', '-C', str(repo), 'add', 'AGENTS.md'], check=True, capture_output=True)
        subprocess.run(['git', '-C', str(repo), 'commit', '-m', 'init'], check=True, capture_output=True, env=git_env)
        wt_next = repo / 'next'
        subprocess.run(['git', '-C', str(repo), 'worktree', 'add', '-b', 'next', str(wt_next)],
                       check=True, capture_output=True)
        (wt_next / 'AGENTS.md').write_text('# Next\n\n<!-- SHARED-START -->\nshared v1\n<!-- SHARED-END -->\n\nNext tail\n', encoding='utf-8')
        subprocess.run(['git', '-C', str(wt_next), 'commit', '--only', '-m', 'next agents', '--', 'AGENTS.md'],
                       check=True, capture_output=True, env=git_env)
        (wt_next / '.ai' / 'handoffs' / 'next').mkdir(parents=True)
        (wt_next / '.ai' / 'CURRENT.md').write_text(
            '# Current\nbranch: next\nwork_commit: 12345678\n\n## Open memos\n- MEM-099\n', encoding='utf-8')
        (wt_next / '.ai' / 'handoffs' / 'next' / '20261125T000000Z__agent__1234567__sample.md').write_text(
            '# Sample Log\nagent: antigravity\nmodel: gemini-4-neon\nwork_commit: 12345678\n', encoding='utf-8')

        team = self.api('/control/team', {'action': 'create', 'name': 'DocsTeam', 'workspace': str(repo)})['team_id']
        worker = self.member(team, 'worker1', 'claude', '测试职责', role='worker', model='deepseek-v4-flash', worktree='')
        cc_bind = self.desktop(worker, session_id=str(uuid.uuid4()))

        # Root worktree displays clear unbounded label in roster & whoami.
        me = self.api('/desktop/whoami', cc_bind)
        self.assertEqual(me['worktree'], '项目内不限（注意避免多线写入冲突）')
        roster_me = self.api('/desktop/roster', cc_bind)['members'][0]
        self.assertEqual(roster_me['worktree'], '项目内不限（注意避免多线写入冲突）')

        # Deploy hooks -> writes .git/info/exclude so Hub files are ignored cleanly.
        self.api('/control/project', {'team_id': team, 'action': 'install'})
        exclude_text = (repo / '.git' / 'info' / 'exclude').read_text(encoding='utf-8')
        self.assertIn('# BEGIN Team Hub managed', exclude_text)
        status_next = subprocess.run(['git', '-C', str(wt_next), 'status', '--short'],
                                     capture_output=True, text=True, check=True).stdout
        self.assertNotIn('.codex/', status_next)
        self.assertNotIn('.mcp.json', status_next)

        # Operator test message & cancel queued message.
        sent = self.api('/control/message/send', {'team_id': team, 'to': 'worker1', 'text': '测试连通'}, 202)
        self.api('/control/message/cancel', {'team_id': team, 'id': sent['id']})
        self.api('/control/message/cancel', {'team_id': team, 'id': sent['id']}, 409)
        self.assertIsNone(self.api('/desktop/poll', cc_bind)['delivery'])

        # .ai/ read-only inspection returns CURRENT summary & recent handoff log.
        ai_info = self.api('/control/worktree-ai', {'team_id': team, 'worktree': 'next'})
        self.assertEqual(ai_info['current']['meta']['work_commit'], '12345678')
        self.assertEqual(ai_info['current']['open_memos'], ['MEM-099'])
        self.assertEqual(len(ai_info['logs']), 1)
        log_detail = self.api('/control/worktree-ai', {
            'team_id': team, 'worktree': 'next', 'log_path': ai_info['logs'][0]['path']})
        self.assertIn('# Sample Log', log_detail['selected_log']['content'])

        # Set Team git identity & baseline AGENTS.md via /control.
        self.api('/control/team', {
            'action': 'git_identity', 'team_id': team,
            'git_author_name': 'Test Author', 'git_author_email': 'author@example.com',
        })
        self.assertEqual(self.api('/control')['alerts'], [])

        # External modification to next/AGENTS.md triggers alert in /control!
        (wt_next / 'AGENTS.md').write_text(
            '# Next\n\n<!-- SHARED-START -->\nexternal edit\n<!-- SHARED-END -->\n\nNext tail\n', encoding='utf-8')
        alerts = self.api('/control')['alerts']
        self.assertEqual(len(alerts), 1)
        self.assertEqual((alerts[0]['team_name'], alerts[0]['doc_key'], alerts[0]['target']),
                         ('DocsTeam', 'agents', 'next'))
        ext_doc = self.api('/control/doc', {'team_id': team, 'action': 'get', 'doc_key': 'agents', 'worktree': 'next'})
        self.assertTrue(ext_doc['external_changed'])
        self.assertIsNotNone(ext_doc['external_diff'])
        self.assertGreaterEqual(ext_doc['external_diff']['added'], 1)
        self.assertGreaterEqual(ext_doc['external_diff']['removed'], 1)
        # Accept external change clears alert and saves external revision.
        self.api('/control/doc', {'team_id': team, 'action': 'accept_external', 'doc_key': 'agents', 'worktree': 'next'})
        self.assertEqual(self.api('/control')['alerts'], [])

        # Save AGENTS.md with shared-section multi-worktree sync + safe single-file git commit!
        (wt_next / 'unrelated.txt').write_text('dirty file must not be committed\n', encoding='utf-8')
        saved = self.api('/control/doc', {
            'team_id': team, 'action': 'save', 'doc_key': 'agents', 'worktree': '',
            'content': '# Root\n\n<!-- SHARED-START -->\nshared v2\n<!-- SHARED-END -->\n\nRoot tail\n',
            'sync_worktrees': ['next'], 'auto_commit': True, 'commit_worktrees': ['', 'next'],
        })
        self.assertEqual(saved['commits']['.']['status'], 'committed')
        self.assertEqual(saved['commits']['next']['status'], 'committed')
        # Verify next/AGENTS.md kept its local header & tail while updating shared block!
        next_text = (wt_next / 'AGENTS.md').read_text(encoding='utf-8')
        self.assertIn('# Next', next_text)
        self.assertIn('shared v2', next_text)
        self.assertIn('Next tail', next_text)
        # Verify unrelated.txt was NOT included in the AGENTS.md commit.
        committed_files = subprocess.run(['git', '-C', str(wt_next), 'show', '--name-only', '--format=', 'HEAD'],
                                         capture_output=True, text=True, check=True).stdout.strip().splitlines()
        self.assertEqual(committed_files, ['AGENTS.md'])

        # Role profile edit, history rollback, and reset to default (AGENTS.md rejects reset).
        self.api('/control/doc', {'team_id': team, 'action': 'reset', 'doc_key': 'agents'}, 409)
        self.api('/control/doc', {'team_id': team, 'action': 'save', 'doc_key': 'worker', 'content': 'custom worker v1'})
        self.api('/control/doc', {'team_id': team, 'action': 'save', 'doc_key': 'worker', 'content': 'custom worker v2'})
        doc_state = self.api('/control/doc', {'team_id': team, 'action': 'get', 'doc_key': 'worker'})
        self.assertTrue(doc_state['is_override'])
        self.assertGreaterEqual(len(doc_state['revisions']), 2)
        first_rev_id = doc_state['revisions'][-1]['id']
        self.api('/control/doc', {'team_id': team, 'action': 'rollback', 'revision_id': first_rev_id})
        self.assertEqual(self.api('/control/doc', {'team_id': team, 'action': 'get', 'doc_key': 'worker'})['content'].strip(),
                         'custom worker v1')
        self.api('/control/doc', {'team_id': team, 'action': 'reset', 'doc_key': 'worker'})
        reset_state = self.api('/control/doc', {'team_id': team, 'action': 'get', 'doc_key': 'worker'})
        self.assertFalse(reset_state['is_override'])
        self.assertIn('分工说明：Worker', reset_state['content'])

        # Removing project hooks also cleans .git/info/exclude.
        self.api('/control/project', {'team_id': team, 'action': 'remove'})
        self.assertNotIn('# BEGIN Team Hub managed', (repo / '.git' / 'info' / 'exclude').read_text(encoding='utf-8'))

    def test_agents_grouping_shared_block_similar_search_and_batch_ops(self):
        repo = self.root / 'agents-group-repo'
        repo.mkdir()
        git = lambda *args, cwd=repo: subprocess.run(
            ['git', '-C', str(cwd), *args], capture_output=True, text=True, check=True)
        git('init', '-b', 'main')
        git('config', 'user.name', 'Test User')
        git('config', 'user.email', 'test@example.com')
        (repo / 'AGENTS.md').write_text('# Container Root\n\n仅负责工作树路由。\n', encoding='utf-8')
        git('add', 'AGENTS.md')
        git('commit', '-m', 'init root')

        wt_next = repo / 'next'
        wt_stable = repo / 'stable'
        wt_gw = repo / 'gateway'
        git('worktree', 'add', '-b', 'next', str(wt_next))
        git('worktree', 'add', '-b', 'stable', str(wt_stable))
        git('worktree', 'add', '-b', 'gateway', str(wt_gw))

        (wt_next / 'AGENTS.md').write_text(
            '# Next\n\n- 本工作树密钥存放于./local/(位置A)，禁止提交。\n\n'
            '<!-- SHARED-START -->\n## 协作与交接\n\n- 共享规则 v1。\n<!-- SHARED-END -->\n\nNext tail\n',
            encoding='utf-8',
        )
        (wt_stable / 'AGENTS.md').write_text(
            '# Stable\n\n- 本工作树密钥存放于./local/(位置B)，禁止提交。\n'
            '- 本工作树密钥存放于./local/(位置B)，禁止提交。\n\n'
            '<!-- SHARED-START -->\n## 协作与交接\n\n- 共享规则 旧版。\n<!-- SHARED-END -->\n\nStable tail\n',
            encoding='utf-8',
        )
        (wt_gw / 'AGENTS.md').write_text('# Gateway\n\n仅用于本地假网关测试。\n', encoding='utf-8')
        for wt in (wt_next, wt_stable, wt_gw):
            git('add', 'AGENTS.md', cwd=wt)
            git('commit', '-m', 'init agents', cwd=wt)

        team = self.api('/control/team', {'action': 'create', 'name': 'GroupTeam', 'workspace': str(repo)})['team_id']
        self.api('/control/team', {
            'action': 'git_identity', 'team_id': team,
            'git_author_name': 'Test Author', 'git_author_email': 'author@example.com',
        })

        # 1. Overview classifies root / shared / standalone and detects drift.
        ov = self.api('/control/doc', {'team_id': team, 'action': 'agents_overview'})
        by_rel = {w['relative']: w for w in ov['worktrees']}
        self.assertEqual(by_rel['']['group'], 'root')
        self.assertEqual(by_rel['next']['group'], 'shared')
        self.assertEqual(by_rel['stable']['group'], 'shared')
        self.assertEqual(by_rel['gateway']['group'], 'standalone')
        self.assertEqual(ov['shared_group']['canonical_worktree'], 'next')
        self.assertEqual(ov['shared_group']['drifted_count'], 1)
        self.assertIn('共享规则 旧版', by_rel['stable']['drift_diff'])
        self.assertEqual(by_rel['stable']['drift_inline']['added'], 1)
        self.assertEqual(by_rel['stable']['drift_inline']['removed'], 1)
        self.assertTrue(any(r['type'] == 'del' and any(s['type'] == 'emph' for s in r['segments'])
                            for r in by_rel['stable']['drift_inline']['rows']))

        # 2. Save shared block to next+stable, selectively committing ONLY next; refuse standalone gateway.
        self.api('/control/doc', {
            'team_id': team, 'action': 'save_shared',
            'content': '## 协作与交接\n\n- 共享规则 v2 统一版。',
            'target_worktrees': ['gateway'],
        }, 409)
        next_head_before = git('rev-parse', 'HEAD', cwd=wt_next).stdout.strip()
        stable_head_before = git('rev-parse', 'HEAD', cwd=wt_stable).stdout.strip()
        saved = self.api('/control/doc', {
            'team_id': team, 'action': 'save_shared',
            'content': '## 协作与交接\n\n- 共享规则 v2 统一版。',
            'target_worktrees': ['next', 'stable'],
            'commit_worktrees': ['next'],
        })
        self.assertIn('next', saved['commits'])
        self.assertEqual(saved['commits']['next']['status'], 'committed')
        self.assertNotIn('stable', saved['commits'])
        self.assertNotEqual(git('rev-parse', 'HEAD', cwd=wt_next).stdout.strip(), next_head_before)
        self.assertEqual(git('rev-parse', 'HEAD', cwd=wt_stable).stdout.strip(), stable_head_before)
        # Headers & tails preserved in both worktrees.
        self.assertIn('Stable tail', (wt_stable / 'AGENTS.md').read_text(encoding='utf-8'))
        self.assertIn('共享规则 v2 统一版', (wt_stable / 'AGENTS.md').read_text(encoding='utf-8'))
        self.assertEqual(self.api('/control/doc', {'team_id': team, 'action': 'agents_overview'})['shared_group']['drifted_count'], 0)

        # 3. Local dual-track similar search finds (位置B) & duplicate line from (位置A) query.
        found = self.api('/control/doc', {
            'team_id': team, 'action': 'search_similar',
            'query': '本工作树密钥存放于./local/(位置A)，禁止提交。',
            'min_score': 0.55,
            'source_worktree': 'next',
        })
        matched_wts = [m['worktree'] for m in found['matches']]
        self.assertEqual(matched_wts.count('next'), 1)
        self.assertEqual(matched_wts.count('stable'), 2)
        self.assertNotIn('gateway', matched_wts)
        self.assertTrue(all(m['recommended'] for m in found['matches']))
        stable_match = next(m for m in found['matches'] if m['worktree'] == 'stable')
        self.assertTrue(any(seg['type'] == 'diff' for seg in stable_match['diff_segments']))

        # 4. Batch remove matched constraints and commit ONLY stable.
        rem = self.api('/control/doc', {
            'team_id': team, 'action': 'batch_remove',
            'items': [{
                'worktree': m['worktree'],
                'start_offset': m['start_offset'],
                'end_offset': m['end_offset'],
                'expected_text': m['text'],
            } for m in found['matches']],
            'commit_worktrees': ['stable'],
        })
        self.assertEqual(rem['removed_count'], 3)
        self.assertEqual(rem['commits']['stable']['status'], 'committed')
        self.assertNotIn('next', rem['commits'])
        self.assertNotIn('位置B', (wt_stable / 'AGENTS.md').read_text(encoding='utf-8'))
        self.assertNotIn('位置A', (wt_next / 'AGENTS.md').read_text(encoding='utf-8'))
        # Next had uncommitted removal -> commit it cleanly via git to test clean append commit next.
        git('commit', '--only', '-m', 'clean next', '--', 'AGENTS.md', cwd=wt_next)

        # 5. Batch append preview deduplication & section insertion with selective commit.
        dup_preview = self.api('/control/doc', {
            'team_id': team, 'action': 'preview_append',
            'text': '- 共享规则 v2 统一版。',
            'worktrees': ['next', 'stable'],
            'position': 'section',
            'section_heading': '协作与交接',
        })
        self.assertTrue(all(p['duplicate'] and not p['default_apply'] for p in dup_preview['previews']))

        new_clause = '- 新增全组验收前必须运行回归测试。'
        ok_preview = self.api('/control/doc', {
            'team_id': team, 'action': 'preview_append',
            'text': new_clause,
            'worktrees': ['next', 'stable'],
            'position': 'section',
            'section_heading': '协作与交接',
        })
        self.assertTrue(all((not p['duplicate']) and p['default_apply'] for p in ok_preview['previews']))
        appended = self.api('/control/doc', {
            'team_id': team, 'action': 'batch_append',
            'text': new_clause,
            'worktrees': ['next', 'stable'],
            'position': 'section',
            'section_heading': '协作与交接',
            'commit_worktrees': ['next'],
        })
        self.assertEqual(appended['commits']['next']['status'], 'committed')
        self.assertNotIn('stable', appended['commits'])
        self.assertIn(new_clause, (wt_next / 'AGENTS.md').read_text(encoding='utf-8'))
        self.assertIn(new_clause, (wt_stable / 'AGENTS.md').read_text(encoding='utf-8'))

        # 6. Independent-only read & save preserves shared block untouched without exposing SHARED markers.
        next_doc = self.api('/control/doc', {'team_id': team, 'action': 'get', 'doc_key': 'agents', 'worktree': 'next'})
        self.assertTrue(next_doc['has_shared_markers'])
        self.assertNotIn('<!-- SHARED-START -->', next_doc['independent_content'])
        self.assertIn('# Next', next_doc['independent_content'])
        self.assertIn('Next tail', next_doc['independent_content'])
        self.api('/control/doc', {
            'team_id': team, 'action': 'save', 'doc_key': 'agents', 'worktree': 'next',
            'content': '# Next 精简独立块\n\n仅编辑独立内容。\n',
            'content_scope': 'independent',
        })
        rebuilt = (wt_next / 'AGENTS.md').read_text(encoding='utf-8')
        self.assertIn('# Next 精简独立块', rebuilt)
        self.assertIn('共享规则 v2 统一版', rebuilt)
        self.assertIn(new_clause, rebuilt)

        # 7. Batch replace matched clause across worktrees with selective single-file commit.
        found_rep = self.api('/control/doc', {
            'team_id': team, 'action': 'search_similar',
            'query': new_clause, 'min_score': 0.75,
        })
        self.assertEqual(len(found_rep['matches']), 2)
        rep_clause = '- 已升级为统一回归门禁检查。'
        rep_res = self.api('/control/doc', {
            'team_id': team, 'action': 'batch_replace',
            'items': [{
                'worktree': m['worktree'],
                'start_offset': m['start_offset'],
                'end_offset': m['end_offset'],
                'expected_text': m['text'],
            } for m in found_rep['matches']],
            'replacement': rep_clause,
            'commit_worktrees': ['stable'],
        })
        self.assertEqual(rep_res['replaced_count'], 2)
        self.assertEqual(rep_res['commits']['stable']['status'], 'committed')
        self.assertNotIn('next', rep_res['commits'])
        self.assertIn(rep_clause, (wt_next / 'AGENTS.md').read_text(encoding='utf-8'))
        self.assertIn(rep_clause, (wt_stable / 'AGENTS.md').read_text(encoding='utf-8'))
        self.assertNotIn(new_clause, (wt_stable / 'AGENTS.md').read_text(encoding='utf-8'))

        # 8. Clear git identity -> any write+commit must be blocked BEFORE touching disk (zero phantom external alert!).
        self.api('/control/team', {
            'action': 'git_identity', 'team_id': team,
            'git_author_name': '', 'git_author_email': '',
        })
        self.assertEqual(self.api('/control')['alerts'], [])
        before_next = (wt_next / 'AGENTS.md').read_text(encoding='utf-8')
        before_stable = (wt_stable / 'AGENTS.md').read_text(encoding='utf-8')
        self.api('/control/doc', {
            'team_id': team, 'action': 'save', 'doc_key': 'agents', 'worktree': 'next',
            'content': '# Blocked save\n', 'auto_commit': True, 'commit_worktrees': ['next'],
        }, 409)
        self.api('/control/doc', {
            'team_id': team, 'action': 'save_shared',
            'content': '## Blocked shared\n', 'target_worktrees': ['next', 'stable'], 'commit_worktrees': ['next'],
        }, 409)
        found_blk = self.api('/control/doc', {
            'team_id': team, 'action': 'search_similar', 'query': rep_clause, 'min_score': 0.75,
        })
        blk_items = [{
            'worktree': m['worktree'], 'start_offset': m['start_offset'],
            'end_offset': m['end_offset'], 'expected_text': m['text'],
        } for m in found_blk['matches']]
        self.api('/control/doc', {
            'team_id': team, 'action': 'batch_remove', 'items': blk_items, 'commit_worktrees': ['next'],
        }, 409)
        self.api('/control/doc', {
            'team_id': team, 'action': 'batch_replace', 'items': blk_items,
            'replacement': '- blocked replacement', 'commit_worktrees': ['stable'],
        }, 409)
        self.api('/control/doc', {
            'team_id': team, 'action': 'batch_append', 'text': '- blocked append',
            'worktrees': ['next'], 'position': 'end', 'commit_worktrees': ['next'],
        }, 409)
        self.assertEqual((wt_next / 'AGENTS.md').read_text(encoding='utf-8'), before_next)
        self.assertEqual((wt_stable / 'AGENTS.md').read_text(encoding='utf-8'), before_stable)
        self.assertEqual(self.api('/control')['alerts'], [])

    def test_console_static_module_whitelist_and_security_headers(self):
        from urllib.error import HTTPError
        from urllib.request import Request, urlopen
        from hub import CONSOLE_FILES

        expected_csp = "default-src 'self'; base-uri 'none'; frame-ancestors 'none'; form-action 'none'"
        base_url = f'http://127.0.0.1:{self.server.server_port}'
        # Positive: every whitelisted static route is publicly readable with strict headers.
        for path, (_fname, mime) in CONSOLE_FILES.items():
            req = Request(f'{base_url}{path}', method='GET')
            with urlopen(req, timeout=4) as res:
                self.assertEqual(res.status, 200, path)
                self.assertEqual(res.headers.get('Content-Type'), f'{mime}; charset=utf-8', path)
                self.assertEqual(res.headers.get('Cache-Control'), 'no-store', path)
                self.assertEqual(res.headers.get('X-Content-Type-Options'), 'nosniff', path)
                self.assertEqual(res.headers.get('Content-Security-Policy'), expected_csp, path)
                body = res.read()
                self.assertGreater(len(body), 0, path)

        # Negative: console/package.json and unknown paths must never be served publicly.
        for bad_path in ('/manage/package.json', '/manage/modules/unknown.js', '/manage/../hub.py'):
            req = Request(f'{base_url}{bad_path}', method='GET')
            with self.assertRaises(HTTPError) as ctx:
                urlopen(req, timeout=4)
            self.assertIn(ctx.exception.code, (401, 404), bad_path)

        # Unauthenticated API endpoints still require Bearer auth.
        for api_path, method in (('/control', 'GET'), ('/control/team', 'POST')):
            req = Request(f'{base_url}{api_path}', data=(b'{}' if method == 'POST' else None), method=method)
            with self.assertRaises(HTTPError) as ctx:
                urlopen(req, timeout=4)
            self.assertEqual(ctx.exception.code, 401, api_path)

if __name__ == '__main__':
    unittest.main()

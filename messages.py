"""Durable per-member inboxes and roster change receipts."""

import sqlite3
import uuid

from teams import Conflict


def render_message(sender, body, changed=False):
    return f'【来自 {sender}】\n' + ('当前团队组织有变化\n\n' if changed else '') + body


class Messages:
    def __init__(self, store):
        self.store = store
        with store.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS team_messages (
                    id TEXT PRIMARY KEY, team_id INTEGER NOT NULL,
                    sender TEXT NOT NULL, sender_name TEXT NOT NULL, recipient TEXT NOT NULL,
                    body TEXT NOT NULL, notice_revision INTEGER, delivered TEXT
                );
                CREATE INDEX IF NOT EXISTS recipient_inbox ON team_messages(recipient,delivered);
            """)

    def send(self, team_id, sender, recipient_name, body, request_id=None):
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            sender_row = db.execute("""SELECT COALESCE(r.alias,r.member) FROM roster r
                                       JOIN teams t ON r.team_id=t.id
                                       WHERE t.archived=0 AND r.team_id=? AND r.member=?""",
                                    (team_id, sender)).fetchone()
            recipient = self.store.teams.lookup(db, team_id, recipient_name)
            if not sender_row or not recipient:
                raise Conflict('请使用当前 Team 的成员名。')
            message_id = request_id or str(uuid.uuid4())
            try:
                db.execute('''INSERT INTO team_messages(id,team_id,sender,sender_name,recipient,body)
                              VALUES (?,?,?,?,?,?)''',
                           (message_id, team_id, sender, sender_row[0], recipient, body))
            except sqlite3.IntegrityError:
                previous = db.execute('SELECT team_id,sender,recipient,body FROM team_messages WHERE id=?',
                                      (message_id,)).fetchone()
                if tuple(previous) != (team_id, sender, recipient, body):
                    raise Conflict('消息重试与原请求内容不一致。')
        return {'status': 'queued', 'task_id': message_id}

    def next(self, team_id, member):
        with self.store.connect() as db:
            return self.next_in_connection(db, team_id, member)

    @staticmethod
    def next_in_connection(db, team_id, member):
        row = db.execute("""
            SELECT m.id,m.body,COALESCE(s.alias,m.sender_name) AS sender_name,t.revision,r.seen_revision
            FROM team_messages m JOIN teams t ON t.id=m.team_id
            JOIN roster r ON r.team_id=m.team_id AND r.member=m.recipient
            JOIN member_controls c ON c.name=m.recipient
            LEFT JOIN roster s ON s.team_id=m.team_id AND s.member=m.sender
            WHERE m.team_id=? AND m.recipient=? AND m.delivered IS NULL AND t.archived=0
            AND c.paused=0
            ORDER BY m.rowid LIMIT 1
        """, (team_id, member)).fetchone()
        if not row:
            return None
        return {'id': row['id'], 'revision': row['revision'],
                'prompt': render_message(row['sender_name'], row['body'], row['revision'] > row['seen_revision'])}

    def received(self, team_id, member, message_id, revision, state='accepted'):
        with self.store.connect() as db:
            return self.received_in_connection(db, team_id, member, message_id, revision, state)

    @staticmethod
    def received_in_connection(db, team_id, member, message_id, revision, state):
        if type(revision) is not int or revision < 0:
            return False
        row = db.execute("""UPDATE team_messages SET delivered=?
                            WHERE id=? AND team_id=? AND recipient=? AND (delivered IS NULL OR delivered='submitting')
                            AND ?<=(SELECT revision FROM teams WHERE id=?)
                            RETURNING id""", (state, message_id, team_id, member, revision, team_id)).fetchone()
        if row and state == 'accepted':
            db.execute('UPDATE team_messages SET notice_revision=? WHERE id=?', (revision, message_id))
            db.execute("""UPDATE roster SET seen_revision=MAX(seen_revision,?) WHERE team_id=? AND member=?""",
                       (revision, team_id, member))
        return row is not None or db.execute("""SELECT 1 FROM team_messages WHERE id=? AND team_id=?
                            AND recipient=? AND delivered=?""", (message_id, team_id, member, state)).fetchone() is not None

    def reserve_codex(self, team_id, member):
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            delivery = self.next_in_connection(db, team_id, member)
            if delivery:
                db.execute("UPDATE team_messages SET delivered='submitting' WHERE id=?", (delivery['id'],))
            return delivery

    def retry_codex(self, message_id):
        with self.store.connect() as db:
            db.execute("UPDATE team_messages SET delivered=NULL WHERE id=? AND delivered='submitting'", (message_id,))

    def cancel(self, team_id, message_id):
        with self.store.connect() as db:
            row = db.execute(
                """UPDATE team_messages SET delivered='canceled'
                   WHERE team_id=? AND id=? AND (delivered IS NULL OR delivered='submitting')
                   RETURNING id""",
                (team_id, message_id),
            ).fetchone()
            if not row:
                raise Conflict('仅可取消尚未送达的消息。')
        return {'canceled': True, 'id': message_id}

    def send_operator(self, team_id, recipient_name, body):
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            team = self.store.teams.get(db, team_id)
            if not team:
                raise Conflict('该 Team 不可用。')
            recipient = self.store.teams.lookup(db, team_id, recipient_name)
            if not recipient:
                raise Conflict('请选择当前 Team 中的成员。')
            message_id = str(uuid.uuid4())
            db.execute(
                '''INSERT INTO team_messages(id,team_id,sender,sender_name,recipient,body)
                   VALUES (?,?,?,?,?,?)''',
                (message_id, team_id, 'operator', '管理台（操作者）', recipient, body),
            )
        return {'status': 'queued', 'id': message_id}

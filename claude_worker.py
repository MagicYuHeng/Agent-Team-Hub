import json
import os
import queue
import shutil
import subprocess
import threading
import time
import uuid
from pathlib import Path


class WorkerError(RuntimeError):
    pass


def claude_environment():
    environment = os.environ.copy()
    for name in ('TEAM_HUB_TOKEN', 'CLAUDECODE'):
        environment.pop(name, None)

    # Restricted mode skips user settings. Forward the active provider as one
    # bundle so a credential and a base URL from different sources cannot mix.
    routing_keys = ('ANTHROPIC_BASE_URL', 'ANTHROPIC_AUTH_TOKEN', 'ANTHROPIC_API_KEY')
    if any(environment.get(key) for key in routing_keys):
        return environment

    config_dir = Path(environment.get('CLAUDE_CONFIG_DIR') or Path.home() / '.claude')
    try:
        settings = json.loads((config_dir / 'settings.json').read_text(encoding='utf-8-sig'))
    except FileNotFoundError:
        return environment
    except (OSError, ValueError) as error:
        raise WorkerError('Claude user settings could not be read.') from error

    provider = settings.get('env', {}) if isinstance(settings, dict) else None
    if not isinstance(provider, dict):
        raise WorkerError('Claude user settings must contain a valid env object.')
    if any(provider.get(key) for key in routing_keys):
        for key, value in provider.items():
            if key.startswith('ANTHROPIC_') and isinstance(value, str) and value:
                environment[key] = value
    return environment


def claude_executable():
    path = shutil.which('claude')
    if path and Path(path).suffix.lower() in {'.cmd', '.bat'}:
        # The Windows npm shim delegates to this executable. Own the actual child.
        path = Path(path).parent / 'node_modules/@anthropic-ai/claude-code/bin/claude.exe'
        if not path.is_file():
            path = None
    if not path:
        raise WorkerError('Claude Code executable was not found on PATH.')
    return str(path)


class ClaudeWorker:
    def __init__(self, workspace, timeout=120):
        self.workspace = workspace
        self.timeout = timeout
        self.session_id = str(uuid.uuid4())
        self.process = None
        self.events = queue.Queue()
        self.stopped = threading.Event()
        self.lock = threading.Lock()

    def _start(self):
        command = [
            claude_executable(), '--print', '--input-format', 'stream-json',
            '--output-format', 'stream-json', '--verbose', '--session-id', self.session_id,
            '--tools', 'Read', '--allowedTools', 'Read', '--permission-mode', 'dontAsk',
            '--restricted', '--strict-mcp-config', '--no-session-persistence',
        ]
        self.process = subprocess.Popen(
            command, cwd=self.workspace, env=claude_environment(), stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
            encoding='utf-8', errors='replace',
        )
        threading.Thread(target=self._read_results, args=(self.process,), daemon=True).start()

    def _read_results(self, process):
        try:
            for line in process.stdout:
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                if isinstance(event, dict) and event.get('type') == 'result':
                    self.events.put(event)
        finally:
            process.stdout.close()
            self.events.put(None)

    def run(self, instruction):
        with self.lock:
            if self.stopped.is_set():
                raise WorkerError('The Claude session stopped. Restart the Hub to start a new session.')
            if self.process is None:
                self._start()
            process = self.process

        payload = {'type': 'user', 'message': {'role': 'user', 'content': instruction}}
        started = time.monotonic()
        try:
            process.stdin.write(json.dumps(payload) + '\n')
            process.stdin.flush()
            event = self.events.get(timeout=max(0, self.timeout - (time.monotonic() - started)))
        except (BrokenPipeError, OSError, ValueError, queue.Empty) as error:
            self.close()
            raise WorkerError('Claude did not return a result within the active session.') from error
        if event is None or event.get('session_id') != self.session_id:
            self.close()
            raise WorkerError('Claude exited or returned an unexpected session ID.')
        result = event.get('result')
        if event.get('is_error') or event.get('subtype') != 'success' or not isinstance(result, str):
            if isinstance(result, str) and '/login' in result:
                raise WorkerError('Claude requires authentication. Check the active CC Switch provider or Hub environment.')
            raise WorkerError('Claude returned an execution error. Check the CLI runtime and provider configuration.')
        return self.session_id, result

    def close(self):
        with self.lock:
            self.stopped.set()
            process = self.process
            if process is None:
                return
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
            process.stdin.close()

import datetime
import hashlib
import hmac
import json
import os
import pathlib
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse
from team_relay import TOOLS as SCOPED_TOOLS, handle as handle_scoped_tool

BASE_DIR = pathlib.Path(__file__).parent
STATE_DIR = pathlib.Path(os.environ.get("RELAY_STATE_DIR") or BASE_DIR)
HTML_PATH = BASE_DIR / "dist" / "app.html"
STATE_PATH = STATE_DIR / "relay_state.json"
CONFIG_PATH = BASE_DIR / "relay_config.json"
LOG_PATH = STATE_DIR / "mcp_server.log"

DEFAULT_CONFIG = {
    "hub_url": "http://127.0.0.1:58767",
    "captain_chat_label": "Chat A",
    "http_host": "127.0.0.1",
    "http_port": 58766,
}

STATE_LOCK = threading.Lock()
STDOUT_LOCK = threading.Lock()
BIND_LEASE_SECONDS = 30.0


def log(msg: str) -> None:
    ts = datetime.datetime.now().isoformat()
    with LOG_PATH.open("a", encoding="utf-8") as f:
        f.write(f"[{ts}] [pid={os.getpid()}] {msg}\n")


def load_config() -> dict:
    cfg = dict(DEFAULT_CONFIG)
    if CONFIG_PATH.exists():
        try:
            raw = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                cfg.update(raw)
        except Exception as e:
            log(f"Failed to read {CONFIG_PATH}: {e}")
    return cfg


def get_hub_url() -> str:
    env_url = os.environ.get("TEAM_HUB_URL", "").strip()
    if env_url:
        return env_url.rstrip("/")
    return str(load_config().get("hub_url", DEFAULT_CONFIG["hub_url"])).strip().rstrip("/")


def get_hub_token() -> str:
    return os.environ.get("TEAM_HUB_TOKEN", "").strip()


def load_state() -> dict:
    base = {
        "queue": [],
        "results": {},
        "events": [],
        "pending_dispatches": {},
        "dispatches": {},
        "delivered_messages": {},
        "acked_messages": {},
        "binding": {},
    }
    if not STATE_PATH.exists():
        return base
    try:
        loaded = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        if isinstance(loaded, dict):
            base.update(loaded)
        return base
    except Exception:
        return base


def save_state(state: dict) -> None:
    tmp = STATE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(STATE_PATH)


def append_event(st: dict, kind: str, detail: dict) -> None:
    st.setdefault("events", []).append(
        {"ts": datetime.datetime.now().isoformat(), "pid": os.getpid(), "kind": kind, **detail}
    )


def hub_http_json(
    method: str,
    path: str,
    payload: dict | None = None,
    timeout: float = 8.0,
) -> tuple[int, dict]:
    url = f"{get_hub_url()}{path}"
    headers: dict[str, str] = {}
    token = get_hub_token()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    data = None
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json; charset=utf-8"

    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8")
            return resp.status, json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", errors="replace")
        try:
            body = json.loads(raw) if raw else {"error": str(e)}
        except Exception:
            body = {"error": raw or str(e)}
        return e.code, body


def build_hub_result_prompt(
    message_id: str,
    task_id: str,
    sender: str,
    status: str,
    session_id: str | None,
    result_text: str,
) -> str:
    return (
        f"[Team Hub 结果投递]\n"
        f"- message_id: {message_id}\n"
        f"- task_id: {task_id}\n"
        f"- from: {sender}\n"
        f"- status: {status}\n"
        f"- session_id: {session_id if session_id else 'null'}\n"
        f"- result:\n{result_text}\n\n"
        f"请向用户汇报上述任务结果，并立即调用工具 "
        f'submit_relay_result(message_id="{message_id}", task_id="{task_id}") '
        f"向 Hub 确认本条结果已处理（仅用于确认，不要重复派单）。"
    )


def format_result_text(raw_result) -> str:
    if raw_result is None:
        return ""
    if isinstance(raw_result, str):
        return raw_result
    return json.dumps(raw_result, ensure_ascii=False, indent=2)


def enqueue_hub_result(body: dict) -> tuple[int, dict]:
    message_id = str(body.get("message_id", "")).strip()
    task_id = str(body.get("task_id", "")).strip()
    sender = str(body.get("from", "unknown")).strip() or "unknown"
    status = str(body.get("status", "completed")).strip() or "completed"
    session_id = body.get("session_id")
    raw_result = body.get("result", "")
    result_text = format_result_text(raw_result)
    if not message_id or not task_id:
        return 400, {"error": "message_id and task_id are required"}

    now = datetime.datetime.now().isoformat()
    with STATE_LOCK:
        st = load_state()
        if (
            message_id in st.get("acked_messages", {})
            or message_id in st.get("delivered_messages", {})
            or any(q.get("message_id") == message_id for q in st.get("queue", []))
        ):
            append_event(
                st,
                "hub_result_duplicate_ignored",
                {"message_id": message_id, "task_id": task_id},
            )
            save_state(st)
            return 200, {
                "status": "duplicate_ignored",
                "message_id": message_id,
                "task_id": task_id,
            }

        item = {
            "kind": "hub_result",
            "request_id": message_id,
            "message_id": message_id,
            "task_id": task_id,
            "from": sender,
            "status": status,
            "session_id": session_id,
            "result": raw_result,
            "prompt": build_hub_result_prompt(
                message_id, task_id, sender, status, session_id, result_text
            ),
            "enqueued_at": now,
        }
        st.setdefault("queue", []).append(item)
        append_event(
            st,
            "hub_result_enqueued",
            {"message_id": message_id, "task_id": task_id, "from": sender},
        )
        save_state(st)
    log(f"HUB_RESULT_ENQUEUED message_id={message_id} task_id={task_id} from={sender}")
    return 202, {"status": "enqueued", "message_id": message_id, "task_id": task_id}


def enqueue_secret_test(body: dict) -> tuple[int, dict]:
    request_id = body.get("request_id") or ("REQ-" + uuid.uuid4().hex[:8].upper())
    prompt = body.get("prompt") or (
        f"请只回复：本次请求ID {request_id} + 上一轮暗号。"
        f'并调用工具 submit_relay_result 提交 request_id="{request_id}" 和你记住的上一轮暗号。'
    )
    item = {
        "kind": "secret_test",
        "request_id": request_id,
        "prompt": prompt,
        "enqueued_at": datetime.datetime.now().isoformat(),
    }
    with STATE_LOCK:
        st = load_state()
        st.setdefault("queue", []).append(item)
        append_event(st, "enqueued", {"request_id": request_id})
        save_state(st)
    log(f"ENQUEUED request_id={request_id}")
    return 202, {"status": "enqueued", **item}


class RelayHTTPHandler(BaseHTTPRequestHandler):
    def _send_json(self, code: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _authorized(self) -> bool:
        expected = get_hub_token()
        auth = self.headers.get("Authorization", "")
        if not expected or not auth.startswith("Bearer "):
            self._send_json(401, {"error": "unauthorized"})
            return False
        if not hmac.compare_digest(auth[7:].strip(), expected):
            self._send_json(401, {"error": "unauthorized"})
            return False
        return True

    def log_message(self, format, *args):
        log("HTTP " + (format % args))

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == "/health":
            self._send_json(200, {"status": "ok"})
            return
        if not self._authorized():
            return
        if parsed.path == "/result":
            req_id = (parse_qs(parsed.query).get("request_id") or [""])[0]
            with STATE_LOCK:
                st = load_state()
                res = st.get("results", {}).get(req_id) or st.get("acked_messages", {}).get(req_id)
            if not res:
                self._send_json(200, {"status": "pending", "request_id": req_id})
            else:
                self._send_json(200, res)
            return
        if parsed.path == "/events":
            with STATE_LOCK:
                st = load_state()
            self._send_json(200, {"events": st.get("events", [])})
            return
        self._send_json(404, {"error": "not found"})

    def do_POST(self):
        if not self._authorized():
            return
        parsed = urlparse(self.path)
        if parsed.path not in ("/enqueue", "/deliver"):
            self._send_json(404, {"error": "not found"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self._send_json(400, {"error": "invalid content-length"})
            return
        if length < 0 or length > 64 * 1024:
            self._send_json(413, {"error": "payload too large"})
            return
        try:
            body = json.loads(self.rfile.read(length).decode("utf-8")) if length else {}
        except Exception as e:
            self._send_json(400, {"error": f"invalid json: {e}"})
            return

        if parsed.path == "/deliver" or ("message_id" in body and "task_id" in body):
            code, resp = enqueue_hub_result(body)
        else:
            code, resp = enqueue_secret_test(body)
        self._send_json(code, resp)


class ReuseThreadingHTTPServer(ThreadingHTTPServer):
    allow_reuse_address = True


def start_http_server() -> None:
    cfg = load_config()
    host = "127.0.0.1"
    port = int(os.environ.get("RELAY_HTTP_PORT", cfg.get("http_port", DEFAULT_CONFIG["http_port"])))
    try:
        srv = ReuseThreadingHTTPServer((host, port), RelayHTTPHandler)
        log(f"Legacy HTTP listening on {host}:{port}")
        srv.serve_forever()
    except OSError as e:
        log(f"HTTP bind skipped ({e}); sharing {STATE_PATH}")


APP_URI = "ui://aux-relay/app.html"

TOOLS = [
    {
        "name": "open_aux_relay",
        "title": "Aux Relay",
        "description": "Open the Aux Relay side pane panel.",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
        "_meta": {
            "ui": {"resourceUri": APP_URI},
            "com.google.antigravity/ui": {
                "entrypoints": [{"type": "aux_pane", "title": "Aux Relay"}]
            },
        },
    },
    {
        "name": "team_dispatch",
        "title": "Team Dispatch",
        "description": (
            "Dispatch an instruction to a teammate (e.g. 'claude') via the local Team Hub. "
            "Returns task_id and status='queued' immediately so the current turn can end."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "to": {"type": "string", "description": "Target agent name, e.g. 'claude'"},
                "instruction": {"type": "string", "description": "Instruction for the target agent"},
            },
            "required": ["to", "instruction"],
            "additionalProperties": False,
        },
    },
    {
        "name": "poll_pending_message",
        "title": "Poll Pending Message",
        "description": "App-only tool to poll local queue and Hub /inbox for the bound Captain chat.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "instance_id": {"type": "string", "description": "Unique aux_pane webview instance id"},
                "bind": {"type": "boolean", "description": "Explicitly bind this instance as Captain chat"},
                "failed_message_id": {
                    "type": "string",
                    "description": "Clear local delivery mark for a message_id whose app.sendMessage() failed",
                },
            },
            "additionalProperties": False,
        },
        "_meta": {"ui": {"visibility": ["app"]}},
    },
    {
        "name": "submit_relay_result",
        "title": "Submit Relay Result",
        "description": (
            "Acknowledge a processed Hub result (message_id + task_id) via POST /inbox/ack, "
            "or submit the recalled secret for a legacy secret test (request_id + secret)."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "message_id": {"type": "string", "description": "The message_id of the Hub result"},
                "task_id": {"type": "string", "description": "The task_id of the Hub result"},
                "request_id": {"type": "string", "description": "Legacy secret test request_id"},
                "secret": {"type": "string", "description": "Legacy secret test recalled secret"},
            },
        },
    },
] + SCOPED_TOOLS

next(tool for tool in TOOLS if tool["name"] == "team_dispatch")["inputSchema"]["properties"]["route"] = {
    "type": "string", "description": "Binding argument issued in this original chat by its Team sidebar"
}

def send_rpc(msg: dict) -> None:
    line = json.dumps(msg, ensure_ascii=False)
    with STDOUT_LOCK:
        sys.stdout.write(line + "\n")
        sys.stdout.flush()


def rpc_tool_result(req_id, text: str, structured: dict, is_error: bool = False) -> None:
    result = {
        "content": [{"type": "text", "text": text}],
        "structuredContent": structured,
    }
    if is_error:
        result["isError"] = True
    send_rpc({"jsonrpc": "2.0", "id": req_id, "result": result})


def handle_team_dispatch(req_id, args: dict) -> None:
    to_agent = str(args.get("to", "")).strip()
    instruction = str(args.get("instruction", "")).strip()
    if not to_agent or not instruction:
        rpc_tool_result(
            req_id,
            "Both 'to' and 'instruction' are required.",
            {"status": "error", "error": "missing_arguments"},
            is_error=True,
        )
        return

    route = args.get("route")
    identity = f"{to_agent}\n{instruction}" if route is None else json.dumps([route, to_agent, instruction])
    dispatch_key = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:16]
    with STATE_LOCK:
        st = load_state()
        pending = st.setdefault("pending_dispatches", {})
        if dispatch_key in pending:
            request_id = pending[dispatch_key]["request_id"]
            reused = True
        else:
            request_id = "REQ-" + uuid.uuid4().hex[:12].upper()
            pending[dispatch_key] = {
                "request_id": request_id,
                "to": to_agent,
                "created_at": datetime.datetime.now().isoformat(),
            }
            reused = False
        save_state(st)

    payload = {"request_id": request_id, "to": to_agent, "instruction": instruction}
    path = "/tasks"
    if route is not None:
        path = "/antigravity/dispatch"
        payload["route"] = route
    last_error = None
    resp_code = 0
    resp_body = {}

    for attempt in range(3):
        try:
            resp_code, resp_body = hub_http_json("POST", path, payload, timeout=8.0)
            if 200 <= resp_code < 300 and resp_body.get("task_id"):
                last_error = None
                break
            last_error = f"HTTP {resp_code}: {json.dumps(resp_body, ensure_ascii=False)}"
            if 400 <= resp_code < 500 and resp_code != 429:
                break
        except Exception as e:
            last_error = str(e)
        if attempt < 2:
            time.sleep(0.25)

    if last_error is not None:
        with STATE_LOCK:
            st = load_state()
            append_event(
                st,
                "dispatch_failed",
                {"request_id": request_id, "to": to_agent, "reused": reused, "error": last_error},
            )
            save_state(st)
        log(f"DISPATCH_FAILED request_id={request_id} to={to_agent} err={last_error}")
        rpc_tool_result(
            req_id,
            f"Hub dispatch failed for request_id={request_id}: {last_error}",
            {
                "status": "error",
                "request_id": request_id,
                "reused_request_id": reused,
                "to": to_agent,
                "hub_url": get_hub_url(),
                "error": last_error,
            },
            is_error=True,
        )
        return

    task_id = str(resp_body.get("task_id"))
    hub_status = str(resp_body.get("status") or "queued")
    now = datetime.datetime.now().isoformat()
    with STATE_LOCK:
        st = load_state()
        st.setdefault("pending_dispatches", {}).pop(dispatch_key, None)
        st.setdefault("dispatches", {})[task_id] = {
            "request_id": request_id,
            "task_id": task_id,
            "to": to_agent,
            "status": hub_status,
            "dispatched_at": now,
        }
        append_event(
            st,
            "dispatched",
            {"request_id": request_id, "task_id": task_id, "to": to_agent, "status": hub_status},
        )
        save_state(st)

    log(f"DISPATCHED request_id={request_id} task_id={task_id} to={to_agent} status={hub_status}")
    sc = {
        "request_id": request_id,
        "task_id": task_id,
        "status": hub_status,
        "to": to_agent,
        "reused_request_id": reused,
    }
    rpc_tool_result(
        req_id,
        f"Dispatched to {to_agent}. task_id={task_id}, status={hub_status}, request_id={request_id}",
        sc,
    )


def check_and_update_binding(st: dict, instance_id: str | None, force_bind: bool, now_ts: float) -> tuple[bool, str | None]:
    binding = st.setdefault("binding", {})
    cur_id = binding.get("instance_id")
    cur_seen = float(binding.get("last_seen_ts") or 0.0)

    if not instance_id:
        if cur_id:
            return False, "bound_to_other" if (now_ts - cur_seen <= BIND_LEASE_SECONDS) else "lease_expired"
        return True, None

    if force_bind or not cur_id:
        binding["instance_id"] = instance_id
        binding["last_seen_ts"] = now_ts
        return True, None

    if cur_id == instance_id:
        if now_ts - cur_seen <= BIND_LEASE_SECONDS:
            binding["last_seen_ts"] = now_ts
            return True, None
        # Lease expired: require explicit user re-bind rather than silent renewal
        return False, "lease_expired"

    # Never allow another chat instance to auto-takeover even if cur_seen expired
    return False, "bound_to_other"


def handle_poll_pending_message(req_id, args: dict) -> None:
    instance_id = str(args.get("instance_id", "")).strip() or None
    force_bind = bool(args.get("bind", False))
    failed_message_id = str(args.get("failed_message_id", "")).strip() or None
    now_ts = time.time()
    cfg = load_config()
    chat_label = str(cfg.get("captain_chat_label", "Chat A"))

    item = None
    bound = False
    unbind_reason = None

    with STATE_LOCK:
        st = load_state()
        bound, unbind_reason = check_and_update_binding(st, instance_id, force_bind, now_ts)

        if bound and failed_message_id:
            st.setdefault("delivered_messages", {}).pop(failed_message_id, None)
            append_event(
                st,
                "hub_delivery_retry_armed",
                {"message_id": failed_message_id, "instance_id": instance_id},
            )

        if bound:
            q = st.get("queue", [])
            if q:
                item = q.pop(0)
                if item.get("kind") == "hub_result" and item.get("message_id"):
                    st.setdefault("delivered_messages", {})[item["message_id"]] = {
                        "message_id": item["message_id"],
                        "task_id": item.get("task_id"),
                        "instance_id": instance_id,
                        "delivered_at": datetime.datetime.now().isoformat(),
                    }
                append_event(
                    st,
                    "dequeued_by_app",
                    {
                        "request_id": item.get("request_id"),
                        "message_id": item.get("message_id"),
                        "task_id": item.get("task_id"),
                        "instance_id": instance_id,
                    },
                )
        active_bound_id = st.get("binding", {}).get("instance_id")
        save_state(st)

    if not bound:
        sc = {
            "hasMessage": False,
            "bound": False,
            "reason": unbind_reason,
            "boundInstanceId": active_bound_id,
            "chatLabel": chat_label,
        }
        rpc_tool_result(req_id, json.dumps(sc, ensure_ascii=False), sc)
        return

    # If local queue was empty, pull from Hub GET /inbox
    if item is None:
        try:
            code, inbox_body = hub_http_json("GET", "/inbox", timeout=5.0)
            if 200 <= code < 300:
                msg = inbox_body.get("message")
                if isinstance(msg, dict) and msg.get("message_id") and msg.get("task_id"):
                    m_id = str(msg["message_id"])
                    t_id = str(msg["task_id"])
                    sender = str(msg.get("from", "claude"))
                    status = str(msg.get("status", "completed"))
                    session_id = msg.get("session_id")
                    result_text = format_result_text(msg.get("result", ""))

                    with STATE_LOCK:
                        st = load_state()
                        prev_deliv = st.get("delivered_messages", {}).get(m_id)
                        already_delivered_same_instance = (
                            prev_deliv is not None and prev_deliv.get("instance_id") == instance_id
                        )
                        if not already_delivered_same_instance:
                            st.setdefault("delivered_messages", {})[m_id] = {
                                "message_id": m_id,
                                "task_id": t_id,
                                "instance_id": instance_id,
                                "delivered_at": datetime.datetime.now().isoformat(),
                            }
                            append_event(
                                st,
                                "inbox_pulled",
                                {
                                    "message_id": m_id,
                                    "task_id": t_id,
                                    "from": sender,
                                    "status": status,
                                    "session_id": session_id,
                                    "instance_id": instance_id,
                                },
                            )
                            save_state(st)
                            item = {
                                "kind": "hub_result",
                                "request_id": m_id,
                                "message_id": m_id,
                                "task_id": t_id,
                                "prompt": build_hub_result_prompt(
                                    m_id, t_id, sender, status, session_id, result_text
                                ),
                            }
        except Exception as e:
            log(f"INBOX_POLL_ERROR err={e}")

    if item:
        log(f"DEQUEUED request_id={item.get('request_id')} message_id={item.get('message_id')}")
        sc = {
            "hasMessage": True,
            "bound": True,
            "chatLabel": chat_label,
            "request_id": item.get("request_id"),
            "message_id": item.get("message_id"),
            "task_id": item.get("task_id"),
            "prompt": item["prompt"],
        }
    else:
        sc = {"hasMessage": False, "bound": True, "chatLabel": chat_label}

    rpc_tool_result(req_id, json.dumps(sc, ensure_ascii=False), sc)


def handle_submit_relay_result(req_id, args: dict) -> None:
    message_id = str(args.get("message_id", "")).strip()
    task_id = str(args.get("task_id", "")).strip()
    r_id = str(args.get("request_id", "")).strip()
    secret = str(args.get("secret", "")).strip()
    now = datetime.datetime.now().isoformat()

    if message_id or (task_id and not secret):
        ack_payload = {
            "message_id": message_id,
            "task_id": task_id,
        }
        hub_ack_ok = False
        hub_ack_code = None
        hub_ack_error = None
        resp_body = {}
        try:
            hub_ack_code, resp_body = hub_http_json("POST", "/inbox/ack", ack_payload, timeout=5.0)
            if 200 <= hub_ack_code < 300 and resp_body.get("acknowledged") is True:
                hub_ack_ok = True
            else:
                hub_ack_error = f"HTTP {hub_ack_code}: {json.dumps(resp_body, ensure_ascii=False)}"
        except Exception as e:
            hub_ack_error = str(e)

        if not hub_ack_ok:
            with STATE_LOCK:
                st = load_state()
                append_event(
                    st,
                    "hub_ack_failed",
                    {
                        "message_id": message_id,
                        "task_id": task_id,
                        "hub_ack_code": hub_ack_code,
                        "error": hub_ack_error,
                    },
                )
                save_state(st)
            log(f"ACK_FAILED message_id={message_id} task_id={task_id} err={hub_ack_error}")
            rpc_tool_result(
                req_id,
                f"Failed to acknowledge message_id={message_id} at Hub /inbox/ack: {hub_ack_error}",
                {
                    "status": "ack_failed",
                    "acknowledged": False,
                    "message_id": message_id,
                    "task_id": task_id,
                    "hub_ack_code": hub_ack_code,
                    "error": hub_ack_error,
                },
                is_error=True,
            )
            return

        ack_obj = {
            "status": "acknowledged",
            "acknowledged": True,
            "message_id": message_id,
            "task_id": task_id,
            "hub_ack_code": hub_ack_code,
            "acked_at": now,
        }
        with STATE_LOCK:
            st = load_state()
            key = message_id or task_id
            st.setdefault("acked_messages", {})[key] = ack_obj
            if message_id:
                st.setdefault("delivered_messages", {}).pop(message_id, None)
            append_event(st, "hub_result_acknowledged", ack_obj)
            save_state(st)
        log(f"ACK_SUBMITTED message_id={message_id} task_id={task_id}")
        rpc_tool_result(
            req_id,
            f"Acknowledged message_id={message_id}, task_id={task_id}.",
            ack_obj,
        )
        return

    res_obj = {
        "status": "completed",
        "request_id": r_id,
        "secret": secret,
        "received_at": now,
    }
    with STATE_LOCK:
        st = load_state()
        st.setdefault("results", {})[r_id] = res_obj
        append_event(st, "result_submitted", {"request_id": r_id, "secret": secret})
        save_state(st)
    log(f"RESULT_SUBMITTED request_id={r_id} secret={secret}")
    rpc_tool_result(req_id, f"Recorded result for {r_id}.", res_obj)


def handle_request(req: dict) -> None:
    req_id = req.get("id")
    method = req.get("method")
    params = req.get("params") or {}

    if method == "initialize":
        proto = params.get("protocolVersion") or "2024-11-05"
        send_rpc(
            {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "protocolVersion": proto,
                    "capabilities": {"tools": {}, "resources": {}},
                    "serverInfo": {"name": "mcp-aux-relay", "version": "0.6.0"},
                },
            }
        )
        return

    if method in ("notifications/initialized",):
        return

    if method == "ping":
        send_rpc({"jsonrpc": "2.0", "id": req_id, "result": {}})
        return

    if method == "resources/list":
        send_rpc(
            {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "resources": [
                        {
                            "uri": APP_URI,
                            "name": "Aux Relay UI",
                            "mimeType": "text/html;profile=mcp-app",
                            "_meta": {"ui": {"prefersBorder": False}},
                        }
                    ]
                },
            }
        )
        return

    if method == "resources/read":
        uri = params.get("uri")
        if uri == APP_URI and HTML_PATH.exists():
            html = HTML_PATH.read_text(encoding="utf-8-sig")
            send_rpc(
                {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "result": {
                        "contents": [
                            {
                                "uri": APP_URI,
                                "mimeType": "text/html;profile=mcp-app",
                                "text": html,
                                "_meta": {"ui": {"prefersBorder": False}},
                            }
                        ]
                    },
                }
            )
        else:
            send_rpc({"jsonrpc": "2.0", "id": req_id, "error": {"code": -32602, "message": f"Unknown resource: {uri}"}})
        return

    if method == "tools/list":
        send_rpc({"jsonrpc": "2.0", "id": req_id, "result": {"tools": TOOLS}})
        return

    if method == "tools/call":
        name = params.get("name")
        args = params.get("arguments") or {}
        if handle_scoped_tool(name, args, req_id, hub_http_json, rpc_tool_result, meta=params.get("_meta")):
            return

        if name == "open_aux_relay":
            from team_relay import open_result
            send_rpc({"jsonrpc": "2.0", "id": req_id, "result": open_result(params.get("_meta"), hub_http_json)})
            return
        if name == "team_dispatch":
            handle_team_dispatch(req_id, args)
            return
        if name == "poll_pending_message":
            handle_poll_pending_message(req_id, args)
            return
        if name == "submit_relay_result":
            handle_submit_relay_result(req_id, args)
            return

        send_rpc({"jsonrpc": "2.0", "id": req_id, "error": {"code": -32601, "message": f"Unknown tool: {name}"}})
        return

    if req_id is not None:
        send_rpc({"jsonrpc": "2.0", "id": req_id, "error": {"code": -32601, "message": f"Method not found: {method}"}})


def main():
    sys.stdin.reconfigure(encoding="utf-8")
    sys.stdout.reconfigure(encoding="utf-8")
    log("MCP server starting")
    if os.environ.get("RELAY_ENABLE_LEGACY_HTTP", "").strip() == "1":
        t = threading.Thread(target=start_http_server, daemon=True)
        t.start()

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
            handle_request(req)
        except Exception as e:
            log(f"Error handling line: {e}")


if __name__ == "__main__":
    main()

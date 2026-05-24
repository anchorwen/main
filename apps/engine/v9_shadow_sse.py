import json
from collections.abc import Generator
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlparse


class SessionStreamQueryError(ValueError):
    pass


class SessionStreamResponseStartError(RuntimeError):
    pass


__all__ = [
    "render_sse_message",
    "render_session_sse_event",
    "parse_sse_messages",
    "iter_sse_messages",
    "iter_sse_messages_from_chunks",
    "summarize_session_sse_events",
    "SessionSSEClientBuffer",
    "consume_session_sse",
    "stream_session_sse",
    "parse_bool_query_param",
    "build_session_stream_args_from_query",
    "build_session_stream_plan_from_query",
    "run_shadow_session_sse_server",
]


def render_sse_message(event_name: str, data: dict) -> str:
    return f"event: {event_name}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


def render_session_sse_event(event: dict) -> str:
    return render_sse_message(event["event"], event)


def parse_sse_messages(stream_text: str) -> list[dict]:
    return list(iter_sse_messages(stream_text.splitlines()))


def iter_sse_messages(lines) -> Generator[dict, None, None]:
    event_name = None
    data_lines: list[str] = []

    def emit_message() -> dict | None:
        if event_name is None and not data_lines:
            return None
        if event_name is None:
            raise ValueError("SSE message missing event line")
        payload = json.loads("\n".join(data_lines))
        return {
            "event": event_name,
            "data": payload,
        }

    for raw_line in lines:
        line = raw_line.rstrip("\r\n")
        if line == "":
            message = emit_message()
            if message is not None:
                yield message
            event_name = None
            data_lines = []
            continue
        if line.startswith(":"):
            continue
        if line.startswith("event: "):
            event_name = line.removeprefix("event: ")
        elif line.startswith("data: "):
            data_lines.append(line.removeprefix("data: "))

    message = emit_message()
    if message is not None:
        yield message


def _split_complete_sse_messages(buffer: str) -> tuple[list[str], str]:
    normalized = buffer.replace("\r\n", "\n")
    raw_messages = []
    consumed_length = 0

    while True:
        boundary = normalized.find("\n\n", consumed_length)
        if boundary == -1:
            break
        raw_message = normalized[consumed_length:boundary]
        consumed_length = boundary + 2
        if raw_message == "":
            continue
        raw_messages.append(raw_message)

    return raw_messages, normalized[consumed_length:]


def _can_parse_terminal_sse_buffer(buffer: str) -> bool:
    lines = buffer.splitlines()
    return any(line.startswith("event: ") for line in lines) and any(
        line.startswith("data: ") for line in lines
    )


def iter_sse_messages_from_chunks(chunks) -> Generator[dict, None, None]:
    buffer = ""

    for chunk in chunks:
        buffer += chunk
        raw_messages, buffer = _split_complete_sse_messages(buffer)
        for raw_message in raw_messages:
            yield from iter_sse_messages(raw_message.splitlines())

    if _can_parse_terminal_sse_buffer(buffer):
        yield from iter_sse_messages(buffer.splitlines())


def summarize_session_sse_events(events: list[dict]) -> dict:
    progress = [event for event in events if event["event"].endswith(".progress")]
    completed = [event for event in events if event["event"].endswith(".completed")]
    errors = [event for event in events if event["event"].endswith(".error")]
    return {
        "events": events,
        "progress": progress,
        "completed": completed,
        "errors": errors,
        "final_completed": completed[-1] if completed else None,
        "final_error": errors[-1] if errors else None,
        "latest_progress": progress[-1] if progress else None,
        "status": "error"
        if errors
        else "completed"
        if completed
        else "streaming"
        if progress
        else "idle",
        "error_message": errors[-1]["data"]["data"]["message"] if errors else None,
        "ok": bool(completed) and not bool(errors),
    }


@dataclass
class SessionSSEClientBuffer:
    buffer: str = ""
    messages: list[dict] = field(default_factory=list)

    @property
    def state(self) -> dict:
        return summarize_session_sse_events(self.messages)

    def feed(self, chunk: str) -> list[dict]:
        produced = []
        self.buffer += chunk
        raw_messages, self.buffer = _split_complete_sse_messages(self.buffer)
        for raw_message in raw_messages:
            for message in iter_sse_messages(raw_message.splitlines()):
                produced.append(message)
                self.messages.append(message)
        return produced

    def finish(self) -> list[dict]:
        if not self.buffer:
            return []
        if not _can_parse_terminal_sse_buffer(self.buffer):
            self.buffer = ""
            return []
        produced = list(iter_sse_messages(self.buffer.splitlines()))
        self.messages.extend(produced)
        self.buffer = ""
        return produced


def consume_session_sse(lines) -> dict:
    return summarize_session_sse_events(list(iter_sse_messages(lines)))


def stream_session_sse(session_manager_cls, args, stream_plan=None):
    manager = session_manager_cls(stream_plan=stream_plan)
    for event in manager.stream_run(args):
        yield render_session_sse_event(event)


def safe_write_sse_message(handler, message: str) -> bool:
    try:
        handler.wfile.write(message.encode("utf-8"))
        handler.wfile.flush()
    except (BrokenPipeError, ConnectionResetError):
        return False
    return True


def start_sse_response(handler) -> None:
    try:
        handler.send_response(200)
        handler.send_header("Content-Type", "text/event-stream; charset=utf-8")
        handler.send_header("Cache-Control", "no-cache")
        handler.send_header("Connection", "close")
        handler.end_headers()
    except (BrokenPipeError, ConnectionResetError):
        raise SessionStreamResponseStartError(
            "Client disconnected before SSE response started"
        ) from None


def parse_bool_query_param(value: str | None, default: bool = False) -> bool:
    if value is None:
        return default
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise SessionStreamQueryError(f"Invalid boolean query value: {value}")


def build_session_stream_args_from_query(query_params: dict[str, list[str]]):
    feature_file = query_params.get("feature_file", [None])[0]
    feature_batch_file = query_params.get("feature_batch_file", [None])[0]
    feature_dir = query_params.get("feature_dir", [None])[0]
    feature_inputs = [feature_file, feature_batch_file, feature_dir]
    if sum(value is not None for value in feature_inputs) > 1:
        raise SessionStreamQueryError(
            "Use only one of --feature-file, --feature-batch-file, or --feature-dir."
        )
    return type(
        "SessionStreamArgs",
        (),
        {
            "scenario_flag": query_params.get("scenario", [None])[0],
            "scenario_positional": None,
            "feature_file": feature_file,
            "feature_batch_file": feature_batch_file,
            "feature_dir": feature_dir,
        },
    )()


def build_session_stream_plan_from_query(
    query_params: dict[str, list[str]], session_stream_plan_cls
):
    event_name_prefix = query_params.get("event_prefix", ["session"])[0] or "session"
    if any(char.isspace() for char in event_name_prefix):
        raise SessionStreamQueryError("event_prefix must not contain whitespace")
    if "." in event_name_prefix:
        raise SessionStreamQueryError("event_prefix must not contain dots")
    return session_stream_plan_cls(
        include_meta=parse_bool_query_param(
            query_params.get("include_meta", [None])[0], default=True
        ),
        include_stats=parse_bool_query_param(
            query_params.get("include_stats", [None])[0], default=False
        ),
        event_name_prefix=event_name_prefix,
    )


def run_shadow_session_sse_server(
    session_manager_cls,
    session_stream_plan_cls,
    host: str = "127.0.0.1",
    port: int = 8765,
) -> HTTPServer:
    class ShadowSessionSSEHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            parsed = urlparse(self.path)
            if parsed.path != "/engine/v9-shadow/stream":
                self.send_response(404)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.end_headers()
                self.wfile.write(b"Not Found")
                return

            query_params = parse_qs(parsed.query, keep_blank_values=False)
            try:
                args = build_session_stream_args_from_query(query_params)
                stream_plan = build_session_stream_plan_from_query(
                    query_params, session_stream_plan_cls
                )
            except SessionStreamQueryError as exc:
                try:
                    stream_plan = build_session_stream_plan_from_query(
                        query_params, session_stream_plan_cls
                    )
                except SessionStreamQueryError:
                    stream_plan = session_stream_plan_cls()
                try:
                    start_sse_response(self)
                except SessionStreamResponseStartError:
                    return
                payload = {
                    "event": f"{stream_plan.event_name_prefix}.error",
                    "step": "session_run_failed",
                    "data": {
                        "error_type": type(exc).__name__,
                        "message": str(exc),
                    },
                }
                if not safe_write_sse_message(self, render_session_sse_event(payload)):
                    return
                return

            try:
                start_sse_response(self)
            except SessionStreamResponseStartError:
                return

            for message in stream_session_sse(session_manager_cls, args, stream_plan=stream_plan):
                if not safe_write_sse_message(self, message):
                    return

        def log_message(self, format, *args):
            return

    return HTTPServer((host, port), ShadowSessionSSEHandler)

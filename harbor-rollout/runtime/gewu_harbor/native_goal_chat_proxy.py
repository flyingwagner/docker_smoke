"""Repair the gateway Responses stream for Chat-Completions-only models.

Codex 0.158 speaks Responses. Self-hosted models may only support Chat
Completions, so the gateway exposes a compatibility route selected with
``use_chat_completions_api=true``. That route can return reasoning deltas
without a complete reasoning item, which Codex does not persist.

This proxy keeps the request path unchanged, enables the explicit gateway
compatibility route, buffers the upstream result, and emits a canonical
Responses item lifecycle. In particular, Chat ``reasoning_content`` is exposed
as a normal ``reasoning`` item with ``reasoning_text`` content so Codex writes
it to the native session.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
import json
from pathlib import Path
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Iterable


_HOP_BY_HOP = {
    "connection",
    "content-length",
    "host",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
}
_REASONING_TYPES = {"reasoning_text", "text", "output_text", "summary_text"}


def prepare_upstream_body(body: bytes, max_output_tokens: int | None = None):
    """Preserve the Codex request and select the gateway compatibility route."""
    payload = json.loads(body)
    if not isinstance(payload, dict):
        raise ValueError("Responses request body must be a JSON object")
    payload["use_chat_completions_api"] = True
    reasoning = payload.get("reasoning")
    if isinstance(reasoning, dict) and isinstance(reasoning.get("effort"), str):
        # The gateway's Chat compatibility path forwards this object to
        # SGLang. SGLang accepts an effort string but rejects summary fields.
        payload["reasoning"] = {"effort": reasoning["effort"]}
    if max_output_tokens is not None:
        current = payload.get("max_output_tokens")
        if current is None:
            payload["max_output_tokens"] = max_output_tokens
        elif isinstance(current, bool) or not isinstance(current, int) or current < 16:
            raise ValueError("max_output_tokens must be an integer >= 16")
        elif current < max_output_tokens:
            payload["max_output_tokens"] = max_output_tokens
    return json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode()


def upstream_target(base_url: str, incoming_path: str) -> str:
    incoming = urllib.parse.urlsplit(incoming_path)
    base = urllib.parse.urlsplit(base_url)
    path = incoming.path
    if path.startswith("/v1"):
        path = path[3:] or "/"
    target_path = base.path.rstrip("/") + (path if path.startswith("/") else "/" + path)
    return urllib.parse.urlunsplit(
        (base.scheme, base.netloc, target_path, incoming.query, "")
    )


def _text_parts(parts: Any, accepted_types: set[str]) -> list[str]:
    if not isinstance(parts, list):
        return []
    texts = []
    for part in parts:
        if not isinstance(part, dict):
            continue
        text = part.get("text")
        if part.get("type") in accepted_types and isinstance(text, str):
            texts.append(text)
    return texts


def normalize_response(payload: dict[str, Any]) -> dict[str, Any]:
    """Turn gateway reasoning output into Responses reasoning_text content."""
    response = deepcopy(payload)
    output = response.get("output")
    if not isinstance(output, list):
        output = []
    normalized = []
    for raw_item in output:
        if not isinstance(raw_item, dict):
            continue
        item = deepcopy(raw_item)
        if item.get("type") == "reasoning":
            texts = _text_parts(item.get("content"), _REASONING_TYPES)
            if not texts:
                texts = _text_parts(item.get("summary"), {"summary_text", "text"})
            item["content"] = (
                [{"type": "reasoning_text", "text": "".join(texts)}] if texts else []
            )
            item["summary"] = item.get("summary") if isinstance(item.get("summary"), list) else []
            item.pop("role", None)
        normalized.append(item)
    response["output"] = normalized
    return response


def _event(
    event_type: str,
    sequence_number: int,
    model: str,
    **values: Any,
) -> bytes:
    payload = {
        "type": event_type,
        "sequence_number": sequence_number,
        "model": model,
        **values,
    }
    return ("data: " + json.dumps(payload, ensure_ascii=False) + "\n\n").encode()


def canonical_events(response: dict[str, Any]) -> Iterable[bytes]:
    """Emit the complete Responses event lifecycle expected by Codex."""
    response = deepcopy(response)
    model = response.get("model") or ""
    queued = deepcopy(response)
    queued["output"] = []
    queued["status"] = "queued"
    in_progress = deepcopy(queued)
    in_progress["status"] = "in_progress"
    sequence = 0
    yield _event("response.created", sequence, model, response=queued)
    sequence += 1
    yield _event("response.in_progress", sequence, model, response=in_progress)
    sequence += 1

    for output_index, final_item in enumerate(response.get("output", [])):
        item_type = final_item.get("type")
        added = deepcopy(final_item)
        if item_type == "reasoning":
            added["content"] = []
            added["summary"] = []
        elif item_type == "message":
            added["content"] = []
            added["status"] = "in_progress"
        elif item_type == "function_call":
            added["arguments"] = ""
            added["status"] = "in_progress"
        elif item_type == "custom_tool_call":
            added["input"] = ""
            added["status"] = "in_progress"
        yield _event(
            "response.output_item.added",
            sequence,
            model,
            output_index=output_index,
            item=added,
        )
        sequence += 1

        if item_type == "reasoning":
            item_id = final_item.get("id")
            for content_index, part in enumerate(final_item.get("content", [])):
                text = part.get("text", "") if isinstance(part, dict) else ""
                yield _event(
                    "response.reasoning_text.delta",
                    sequence,
                    model,
                    content_index=content_index,
                    item_id=item_id,
                    output_index=output_index,
                    delta=text,
                )
                sequence += 1
                yield _event(
                    "response.reasoning_text.done",
                    sequence,
                    model,
                    content_index=content_index,
                    item_id=item_id,
                    output_index=output_index,
                    text=text,
                )
                sequence += 1
        elif item_type == "message":
            item_id = final_item.get("id")
            for content_index, part in enumerate(final_item.get("content", [])):
                if not isinstance(part, dict) or part.get("type") != "output_text":
                    continue
                text = part.get("text", "")
                yield _event(
                    "response.content_part.added",
                    sequence,
                    model,
                    item_id=item_id,
                    output_index=output_index,
                    content_index=content_index,
                    part={"type": "output_text", "text": "", "annotations": []},
                )
                sequence += 1
                yield _event(
                    "response.output_text.delta",
                    sequence,
                    model,
                    item_id=item_id,
                    output_index=output_index,
                    content_index=content_index,
                    delta=text,
                    logprobs=[],
                )
                sequence += 1
                yield _event(
                    "response.output_text.done",
                    sequence,
                    model,
                    item_id=item_id,
                    output_index=output_index,
                    content_index=content_index,
                    text=text,
                    logprobs=[],
                )
                sequence += 1
                yield _event(
                    "response.content_part.done",
                    sequence,
                    model,
                    item_id=item_id,
                    output_index=output_index,
                    content_index=content_index,
                    part=part,
                )
                sequence += 1
        elif item_type == "function_call":
            item_id = final_item.get("id")
            arguments = final_item.get("arguments", "{}")
            yield _event(
                "response.function_call_arguments.delta",
                sequence,
                model,
                output_index=output_index,
                item_id=item_id,
                delta=arguments,
            )
            sequence += 1
            yield _event(
                "response.function_call_arguments.done",
                sequence,
                model,
                output_index=output_index,
                item_id=item_id,
                arguments=arguments,
            )
            sequence += 1
        elif item_type == "custom_tool_call":
            item_id = final_item.get("id")
            tool_input = final_item.get("input", "")
            yield _event(
                "response.custom_tool_call_input.delta",
                sequence,
                model,
                output_index=output_index,
                item_id=item_id,
                delta=tool_input,
            )
            sequence += 1
            yield _event(
                "response.custom_tool_call_input.done",
                sequence,
                model,
                output_index=output_index,
                item_id=item_id,
                input=tool_input,
            )
            sequence += 1

        yield _event(
            "response.output_item.done",
            sequence,
            model,
            output_index=output_index,
            item=final_item,
        )
        sequence += 1

    completed = deepcopy(response)
    completed["status"] = "completed"
    yield _event("response.completed", sequence, model, response=completed)


def _sse_payloads(lines: Iterable[bytes]) -> Iterable[dict[str, Any]]:
    data_lines = []
    for raw in lines:
        line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
        if not line:
            if data_lines:
                data = "\n".join(data_lines)
                data_lines = []
                if data == "[DONE]":
                    return
                try:
                    payload = json.loads(data)
                except ValueError:
                    continue
                if isinstance(payload, dict):
                    yield payload
            continue
        if line.startswith("data:"):
            data_lines.append(line[5:].lstrip())
    if data_lines:
        data = "\n".join(data_lines)
        if data != "[DONE]":
            try:
                payload = json.loads(data)
            except ValueError:
                payload = None
            if isinstance(payload, dict):
                yield payload


def collect_response(lines: Iterable[bytes], on_progress=None) -> dict[str, Any]:
    reasoning: dict[str, list[str]] = {}
    reasoning_order: list[str] = []
    completed = None
    for event in _sse_payloads(lines):
        if on_progress is not None:
            on_progress()
        event_type = event.get("type")
        if event_type in {
            "response.reasoning_summary_text.delta",
            "response.reasoning_text.delta",
        }:
            item_id = event.get("item_id")
            delta = event.get("delta")
            if isinstance(item_id, str) and isinstance(delta, str):
                if item_id not in reasoning:
                    reasoning[item_id] = []
                    reasoning_order.append(item_id)
                reasoning[item_id].append(delta)
        elif event_type == "response.failed":
            response = event.get("response")
            if isinstance(response, dict):
                raise RuntimeError(response.get("error") or "Upstream response failed")
            raise RuntimeError("Upstream response failed")
        elif event_type == "error":
            raise RuntimeError(event.get("error") or event.get("message") or "Upstream stream error")
        elif event_type == "response.completed":
            response = event.get("response")
            if isinstance(response, dict):
                completed = response
    if completed is None:
        raise RuntimeError("Upstream stream ended without response.completed")
    normalized = normalize_response(completed)
    reasoning_items = {
        item.get("id"): item
        for item in normalized.get("output", [])
        if isinstance(item, dict) and item.get("type") == "reasoning"
    }
    for item_id in reasoning_order:
        item = reasoning_items.get(item_id)
        if item is None:
            item = {"id": item_id, "type": "reasoning", "summary": [], "content": []}
            normalized.setdefault("output", []).insert(0, item)
            reasoning_items[item_id] = item
        text = "".join(reasoning[item_id])
        current = _text_parts(item.get("content"), {"reasoning_text", "text"})
        if text and not current:
            item["content"] = [{"type": "reasoning_text", "text": text}]
    return normalized


class ProxyHandler(BaseHTTPRequestHandler):
    server: "ProxyServer"

    def log_message(self, format, *args):
        print(format % args, flush=True)

    def _send_json(self, status: int, payload: Any) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        if not urllib.parse.urlsplit(self.path).path.rstrip("/").endswith("/responses"):
            self.send_error(404)
            return
        length = int(self.headers.get("Content-Length", "0"))
        try:
            body = self.rfile.read(length)
            request_payload = json.loads(body)
            upstream_body = prepare_upstream_body(
                body, self.server.max_output_tokens
            )
        except (ValueError, json.JSONDecodeError) as error:
            self.send_error(400, f"invalid Responses request: {error}")
            return

        headers = {
            key: value
            for key, value in self.headers.items()
            if key.lower() not in _HOP_BY_HOP
        }
        headers["content-type"] = "application/json"
        headers["accept"] = "text/event-stream"
        target = upstream_target(self.server.upstream_base_url, self.path)
        request = urllib.request.Request(
            target, data=upstream_body, headers=headers, method="POST"
        )
        try:
            upstream = urllib.request.urlopen(request, timeout=self.server.timeout)
        except urllib.error.HTTPError as error:
            payload = error.read()
            self.send_response(error.code)
            self.send_header(
                "content-type", error.headers.get("content-type", "application/json")
            )
            self.send_header("content-length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return
        except urllib.error.URLError as error:
            self.send_error(502, f"upstream request failed: {error.reason}")
            return

        try:
            if not request_payload.get("stream", False):
                payload = json.load(upstream)
                try:
                    normalized = normalize_response(payload)
                except Exception as error:
                    self._send_json(
                        502,
                        {"error": {"message": type(error).__name__ + ": " + str(error)}},
                    )
                    return
                self._send_json(200, normalized)
                return

            self.send_response(200)
            self.send_header("content-type", "text/event-stream")
            self.send_header("cache-control", "no-cache")
            self.send_header("connection", "close")
            self.end_headers()
            self.wfile.write(b": upstream-response\n\n")
            self.wfile.flush()
            last_progress = time.monotonic()

            def progress():
                nonlocal last_progress
                now = time.monotonic()
                if now - last_progress >= 5:
                    self.wfile.write(b": upstream-response-active\n\n")
                    self.wfile.flush()
                    last_progress = now

            try:
                normalized = collect_response(upstream, on_progress=progress)
                for event in canonical_events(normalized):
                    self.wfile.write(event)
                    self.wfile.flush()
            except Exception as error:
                response_id = "resp_" + str(int(time.time() * 1000))
                failed = {
                    "id": response_id,
                    "object": "response",
                    "model": request_payload.get("model", ""),
                    "status": "failed",
                    "output": [],
                    "error": {
                        "code": "upstream_chat_compat_failed",
                        "message": type(error).__name__ + ": " + str(error),
                    },
                }
                self.wfile.write(
                    _event(
                        "response.failed",
                        0,
                        request_payload.get("model", ""),
                        response=failed,
                    )
                )
                self.wfile.flush()
        finally:
            upstream.close()

class ProxyServer(ThreadingHTTPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(
        self,
        address,
        upstream_base_url,
        max_output_tokens=None,
        timeout=900,
    ):
        super().__init__(address, ProxyHandler)
        self.upstream_base_url = upstream_base_url
        self.max_output_tokens = max_output_tokens
        self.timeout = timeout


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--listen", required=True)
    parser.add_argument("--upstream-base-url", required=True)
    parser.add_argument("--max-output-tokens", type=int)
    parser.add_argument("--ready-file", required=True)
    parser.add_argument("--timeout", type=float, default=900)
    args = parser.parse_args()
    if args.max_output_tokens is not None and args.max_output_tokens < 16:
        raise SystemExit("--max-output-tokens must be >= 16")
    host, port_text = args.listen.rsplit(":", 1)
    server = ProxyServer(
        (host, int(port_text)),
        args.upstream_base_url,
        args.max_output_tokens,
        args.timeout,
    )
    ready = Path(args.ready_file)
    ready.write_text(json.dumps({"port": int(port_text)}, ensure_ascii=False))
    try:
        server.serve_forever()
    finally:
        ready.unlink(missing_ok=True)
        server.server_close()


if __name__ == "__main__":
    main()

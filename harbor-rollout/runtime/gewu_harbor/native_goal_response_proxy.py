#!/usr/bin/env python3
"""Inject a validated Responses API output limit for Codex 0.158.0."""
from __future__ import annotations

import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import random
import time
import urllib.error
import urllib.parse
import urllib.request


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


def inject_max_output_tokens(body: bytes, max_output_tokens: int) -> bytes:
    """Add the top-level Responses field without changing other parameters."""
    payload = json.loads(body)
    if not isinstance(payload, dict):
        raise ValueError("Responses request body must be a JSON object")
    current = payload.get("max_output_tokens")
    if current is None:
        payload["max_output_tokens"] = max_output_tokens
    elif isinstance(current, bool) or not isinstance(current, int) or current < 16:
        raise ValueError("max_output_tokens must be an integer >= 16")
    elif current < max_output_tokens:
        payload["max_output_tokens"] = max_output_tokens
    return json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode()


def upstream_target(base_url: str, incoming_path: str) -> str:
    """Map the local /v1 prefix onto the upstream provider base path."""
    incoming = urllib.parse.urlsplit(incoming_path)
    base = urllib.parse.urlsplit(base_url)
    path = incoming.path
    if path.startswith("/v1"):
        path = path[3:] or "/"
    target_path = base.path.rstrip("/") + (path if path.startswith("/") else "/" + path)
    return urllib.parse.urlunsplit(
        (base.scheme, base.netloc, target_path, incoming.query, "")
    )


class ProxyHandler(BaseHTTPRequestHandler):
    server: "ProxyServer"

    def log_message(self, format, *args):
        print(format % args, flush=True)

    def _retry_delay(self, response, retry_number):
        """Return a bounded delay for the next 429 attempt.

        Prefer the provider's Retry-After value. If it is absent or malformed,
        use exponential backoff with a small jitter so concurrent agents do
        not retry in lockstep.
        """
        retry_after = response.headers.get("Retry-After")
        try:
            delay = max(0.0, float(retry_after))
        except (TypeError, ValueError):
            delay = min(
                self.server.retry_max_delay,
                self.server.retry_base_delay * (2 ** (retry_number - 1)),
            )
            if delay:
                delay += random.uniform(0.0, min(delay * 0.25, 1.0))
        return min(delay, self.server.retry_max_delay)

    def _handle(self):
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length) if length else b""
        request_path = urllib.parse.urlsplit(self.path).path
        is_responses = request_path.rstrip("/").endswith("/responses")
        if is_responses and self.command == "POST":
            try:
                body = inject_max_output_tokens(body, self.server.max_output_tokens)
            except (ValueError, json.JSONDecodeError) as error:
                self.send_error(400, f"invalid Responses request: {error}")
                return

        headers = {
            key: value
            for key, value in self.headers.items()
            if key.lower() not in _HOP_BY_HOP
        }
        target = upstream_target(self.server.upstream_base_url, self.path)
        request = urllib.request.Request(
            target,
            data=body if self.command in {"POST", "PUT", "PATCH"} else None,
            headers=headers,
            method=self.command,
        )
        retry_number = 0
        while True:
            try:
                response = urllib.request.urlopen(request, timeout=self.server.timeout)
                break
            except urllib.error.HTTPError as error:
                if error.code != 429 or (
                    self.server.max_429_retries >= 0
                    and retry_number >= self.server.max_429_retries
                ):
                    response = error
                    break
                next_retry = retry_number + 1
                delay = self._retry_delay(error, next_retry)
                self.log_message(
                    "upstream 429; retry %d/%s in %.2fs",
                    next_retry,
                    "infinite" if self.server.max_429_retries < 0 else self.server.max_429_retries,
                    delay,
                )
                error.close()
                if delay:
                    time.sleep(delay)
                retry_number = next_retry
            except urllib.error.URLError as error:
                self.send_error(502, f"upstream request failed: {error.reason}")
                return

        self.send_response(response.status)
        for key, value in response.headers.items():
            if key.lower() not in _HOP_BY_HOP:
                self.send_header(key, value)
        self.end_headers()
        while True:
            chunk = response.read(64 * 1024)
            if not chunk:
                break
            self.wfile.write(chunk)
            self.wfile.flush()
        response.close()

    do_GET = _handle
    do_POST = _handle
    do_PUT = _handle
    do_PATCH = _handle


class ProxyServer(ThreadingHTTPServer):
    allow_reuse_address = True

    def __init__(
        self,
        address,
        upstream_base_url,
        max_output_tokens,
        timeout,
        max_429_retries=5,
        retry_base_delay=1.0,
        retry_max_delay=30.0,
    ):
        super().__init__(address, ProxyHandler)
        self.upstream_base_url = upstream_base_url
        self.max_output_tokens = max_output_tokens
        self.timeout = timeout
        if max_429_retries < -1 or max_429_retries > 5:
            raise ValueError("max_429_retries must be -1 (infinite) or between 0 and 5")
        if retry_base_delay < 0 or retry_max_delay < 0:
            raise ValueError("retry delays must be non-negative")
        self.max_429_retries = max_429_retries
        self.retry_base_delay = retry_base_delay
        self.retry_max_delay = retry_max_delay


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--listen", required=True)
    parser.add_argument("--upstream-base-url", required=True)
    parser.add_argument("--max-output-tokens", required=True, type=int)
    parser.add_argument("--ready-file", required=True)
    parser.add_argument("--timeout", type=float, default=900)
    parser.add_argument(
        "--max-429-retries",
        type=int,
        default=-1,
        help="429 reattempts; -1 means unlimited (default: -1)",
    )
    parser.add_argument("--retry-base-delay", type=float, default=1.0)
    parser.add_argument("--retry-max-delay", type=float, default=30.0)
    args = parser.parse_args()
    if args.max_output_tokens < 16:
        raise SystemExit("--max-output-tokens must be >= 16")
    host, port_text = args.listen.rsplit(":", 1)
    server = ProxyServer(
        (host, int(port_text)),
        args.upstream_base_url,
        args.max_output_tokens,
        args.timeout,
        args.max_429_retries,
        args.retry_base_delay,
        args.retry_max_delay,
    )
    ready = Path(args.ready_file)
    ready.write_text(json.dumps({"port": int(port_text), "max_output_tokens": args.max_output_tokens}))
    try:
        server.serve_forever()
    finally:
        ready.unlink(missing_ok=True)
        server.server_close()


if __name__ == "__main__":
    main()

"""Stdio client for native Codex Goals; App Server owns continuation."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import queue
import subprocess
import sys
import threading
import time


class GoalClient:
    def __init__(self, request):
        self.request = request
        self.messages = queue.Queue()
        self.next_id = 0
        self.goal = None
        self.thread_id = None
        self.active_turns = set()
        self.turns = 0
        self.started = time.monotonic()
        self.last_completed = None
        self.state_path = Path(os.environ["CODEX_HOME"]).parent / "goal-state.json"
        self.process = subprocess.Popen(
            ["codex", "app-server", "--enable", "goals", "--enable", "unified_exec",
             *request.get("cli_flags", [])],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=sys.stderr,
            text=True, bufsize=1,
        )
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self):
        for line in self.process.stdout:
            try:
                self.messages.put(json.loads(line))
            except ValueError:
                continue
        self.messages.put(None)

    def record(self, halt_reason=None):
        result = {
            "thread_id": self.thread_id, "goal": self.goal,
            "completed_turns": self.turns, "active_turns": sorted(self.active_turns),
            "native_auto_continuations": max(0, self.turns - 1),
            "halt_reason": halt_reason,
        }
        temporary = self.state_path.with_suffix(".new")
        temporary.write_text(json.dumps(result, indent=2))
        temporary.replace(self.state_path)

    def send(self, method, params=None, notification=False):
        message = {"method": method, "params": params or {}}
        if not notification:
            self.next_id += 1
            message["id"] = self.next_id
        self.process.stdin.write(json.dumps(message) + "\n")
        self.process.stdin.flush()
        return message.get("id")

    def receive(self, timeout=1):
        message = self.messages.get(timeout=timeout)
        if message is None:
            raise RuntimeError(f"Codex App Server exited unexpectedly ({self.process.poll()})")
        print(json.dumps(message), flush=True)
        method = message.get("method")
        params = message.get("params", {})
        if method == "thread/goal/updated":
            self.goal = params["goal"]
            self.record()
        elif method == "turn/started":
            self.active_turns.add(params["turn"]["id"])
            self.record()
        elif method == "turn/completed":
            turn = params["turn"]
            self.active_turns.discard(turn["id"])
            self.turns += 1
            self.last_completed = time.monotonic()
            self.record()
            if turn.get("status") == "failed":
                raise RuntimeError("Native Goal turn failed; see Codex event log")
        elif method in ("item/tool/requestUserInput", "item/commandExecution/requestApproval",
                        "item/fileChange/requestApproval"):
            raise RuntimeError("Interactive input is unavailable in this benchmark")
        return message

    def rpc(self, method, params=None):
        request_id = self.send(method, params)
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            try:
                message = self.receive(min(1, deadline - time.monotonic()))
            except queue.Empty:
                continue
            if message.get("id") == request_id:
                if "error" in message:
                    raise RuntimeError(f"App Server RPC failed: {method}: {message['error']}")
                return message["result"]
        raise RuntimeError(f"App Server RPC timed out: {method}")

    def run(self):
        workdir = self.request.get("workdir", "/app")
        problem_path = str(Path(workdir) / "readme.md")
        solution_path = str(Path(workdir) / "solution.md")
        self.rpc("initialize", {
            "clientInfo": {"name": "harbor_native_goal", "version": "1.0"},
            "capabilities": {"experimentalApi": True},
        })
        self.send("initialized", notification=True)
        result = self.rpc("thread/start", {
            "model": self.request["model"], "cwd": workdir,
            "approvalPolicy": "never", "sandbox": "danger-full-access",
            "experimentalRawEvents": True,
        })
        self.thread_id = result["thread"]["id"]
        self.goal = self.rpc("thread/goal/set", {
            "threadId": self.thread_id, "status": "active",
            "objective": f"Resolve the exact research problem in {problem_path} "
                         f"and save a rigorous self-contained answer in {solution_path}.",
        })["goal"]
        self.record()
        goal_instruction = (
            "\n\nA native Codex Goal has already been created for this task. "
            "Inspect it with get_goal when needed; do not create a duplicate. "
            "Keep working across automatic continuation turns until the objective is achieved. "
            "Use update_goal(status=complete) only after the requested result is established "
            f"and saved in {solution_path}. Follow native Goal rules for blockers. "
            "Save useful partial work promptly. The client safety cap is "
            f"{self.request['max_turns']} turns; Harbor also enforces the task time limit. "

        )
        if self.request.get("lkm_enabled"):
            goal_instruction += (
                " The bohrium-lkm skill is available for useful literature searches; "
                "read its SKILL.md before using it, and never print credentials."
            )
        self.rpc("turn/start", {
            "threadId": self.thread_id,
            "input": [{"type": "text", "text": self.request["instruction"] + goal_instruction}],
        })
        while True:
            try:
                self.receive(.5)
            except queue.Empty:
                pass
            if not self.active_turns and self.last_completed is not None:
                if self.goal and self.goal["status"] != "active":
                    self.record("native_goal_" + self.goal["status"])
                    return
                if time.monotonic() - self.last_completed > 10:
                    self.record("native_dispatcher_idle_with_active_goal")
                    return
            if self.turns >= self.request["max_turns"]:
                for turn_id in tuple(self.active_turns):
                    self.rpc("turn/interrupt", {"threadId": self.thread_id, "turnId": turn_id})
                self.record("client_turn_cap")
                return
            timeout = self.request.get("timeout_sec", 0)
            if timeout and time.monotonic() - self.started > timeout:
                raise TimeoutError("Native Goal client safety timeout")

    def close(self):
        self.process.terminate()
        try:
            self.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--request", required=True)
    args = parser.parse_args()
    client = GoalClient(json.loads(Path(args.request).read_text()))
    try:
        client.run()
    except Exception as error:
        client.record(type(error).__name__)
        print(f"Native Goal driver failed: {error}", file=sys.stderr, flush=True)
        return 1
    finally:
        client.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())

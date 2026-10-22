"""Raw-only Responses reasoning policy for Gewu Goal adapters."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, override

from harbor.agents.installed.codex import Codex
from harbor.models.trajectories import Trajectory


_RAW_REASONING_TYPES = {"reasoning_text", "text"}


def _extract_text_parts(parts: Any, accepted_types: set[str]) -> list[str]:
    if not isinstance(parts, list):
        return []
    texts: list[str] = []
    for part in parts:
        if isinstance(part, str):
            texts.append(part)
        elif isinstance(part, dict):
            text = part.get("text")
            if part.get("type") in accepted_types and isinstance(text, str):
                texts.append(text)
    return texts


class RawReasoningCodex(Codex):
    """Build Harbor's ATIF trajectory with plaintext raw reasoning only.

    Harbor 0.23 reads only ``reasoning.summary``. Responses-compatible
    providers may instead return plaintext chain of thought in
    ``reasoning.content``. This adapter lets Harbor build its normal trajectory,
    clears summary-derived reasoning, then fills ``reasoning_content`` from raw
    content and records its source.
    """

    @staticmethod
    def _extract_reasoning(
        payload: dict[str, Any],
    ) -> list[str]:
        return _extract_text_parts(payload.get("content"), _RAW_REASONING_TYPES)

    @staticmethod
    def _session_file(session_dir: Path) -> Path | None:
        session_files = list(session_dir.glob("*.jsonl"))
        return max(session_files) if session_files else None

    def _reasoning_by_api_call(
        self,
        session_dir: Path,
    ) -> dict[str, list[str]]:
        session_file = self._session_file(session_dir)
        if session_file is None:
            return {}

        raw_events: list[dict[str, Any]] = []
        with session_file.open() as handle:
            for line in handle:
                stripped = line.strip()
                if not stripped:
                    continue
                try:
                    event = json.loads(stripped)
                except json.JSONDecodeError:
                    continue
                if isinstance(event, dict):
                    raw_events.append(event)

        reasoning_by_call: dict[str, list[str]] = {}
        pending_raw: list[str] = []
        api_call_index = 1
        current_api_call_id = f"api_call_{api_call_index}"
        saw_model_output = False

        def attach_pending() -> None:
            nonlocal pending_raw
            if not pending_raw:
                return
            reasoning_by_call.setdefault(current_api_call_id, []).extend(pending_raw)
            pending_raw = []

        for event in raw_events:
            event_type = event.get("type")
            payload = event.get("payload", {})
            if not isinstance(payload, dict):
                continue

            if event_type == "event_msg":
                if payload.get("type") == "token_count" and saw_model_output:
                    api_call_index += 1
                    current_api_call_id = f"api_call_{api_call_index}"
                    saw_model_output = False
                continue

            if event_type != "response_item":
                continue

            payload_type = payload.get("type")
            if payload_type == "reasoning":
                pending_raw.extend(self._extract_reasoning(payload))
                continue

            if payload_type in {"function_call_output", "custom_tool_call_output"}:
                pending_raw = []
                continue

            if payload_type in {"function_call", "custom_tool_call"}:
                if not payload.get("call_id"):
                    continue
                attach_pending()
                saw_model_output = True
                continue

            if payload_type in {"message", "web_search_call"}:
                if payload_type == "message" and payload.get("role") != "assistant":
                    pending_raw = []
                    continue
                attach_pending()
                saw_model_output = True

        return reasoning_by_call

    @staticmethod
    def _join_parts(parts: list[str]) -> str | None:
        text = "\n".join(part for part in parts if part)
        return text or None

    def _add_reasoning_source(
        self,
        trajectory: Trajectory,
        session_dir: Path,
    ) -> None:
        reasoning_by_call = self._reasoning_by_api_call(session_dir)
        for step in trajectory.steps:
            if step.source != "agent":
                continue
            extra = dict(step.extra or {})
            extra.pop("reasoning_source", None)
            extra.pop("reasoning_summary", None)
            step.reasoning_content = None

            api_call_id = extra.get("api_call_id")
            if isinstance(api_call_id, str):
                raw_text = self._join_parts(reasoning_by_call.get(api_call_id, []))
            else:
                raw_text = None
            if raw_text:
                step.reasoning_content = raw_text
                extra["reasoning_source"] = "raw_content"
            step.extra = extra

    @override
    def _convert_events_to_trajectory(self, session_dir: Path) -> Trajectory | None:
        trajectory = super()._convert_events_to_trajectory(session_dir)
        if trajectory is not None:
            self._add_reasoning_source(trajectory, session_dir)
        return trajectory


__all__ = ["RawReasoningCodex"]

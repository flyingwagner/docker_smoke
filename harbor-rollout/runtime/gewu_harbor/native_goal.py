"""Model-independent Harbor adapter for the native Codex Goal driver.

Model routing and metadata belong in Codex's native ``config`` and the optional
``model_catalog``. The adapter does not infer either from a model name.
"""
from __future__ import annotations

import json
from pathlib import Path, PurePosixPath
import re
import shlex

from .model_catalog import load_model_catalog
from .raw_reasoning import RawReasoningCodex


def _integer(value, name, *, minimum=1, maximum=None):
    if isinstance(value, bool) or not (
        isinstance(value, int)
        or isinstance(value, str) and re.fullmatch(r"[0-9]+", value)
    ):
        raise ValueError(f"{name} must be an integer")
    number = int(value)
    if number < minimum or maximum is not None and number > maximum:
        bound = f"between {minimum} and {maximum}" if maximum else f">= {minimum}"
        raise ValueError(f"{name} must be {bound}")
    return number


def _response_proxy(value):
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError("response_proxy must be an object or null")
    if set(value) - {"max_output_tokens", "port", "upstream_api"}:
        raise ValueError(
            "response_proxy supports only max_output_tokens, port, and upstream_api"
        )
    upstream_api = value.get("upstream_api", "responses")
    if upstream_api not in {"responses", "chat_completions"}:
        raise ValueError(
            "response_proxy.upstream_api must be responses or chat_completions"
        )
    max_output_tokens = value.get("max_output_tokens")
    if upstream_api == "responses" and max_output_tokens is None:
        raise ValueError("response_proxy requires an explicit max_output_tokens")
    if max_output_tokens is not None:
        max_output_tokens = _integer(
            max_output_tokens,
            "response_proxy.max_output_tokens",
            minimum=16,
        )
    return {
        "upstream_api": upstream_api,
        "max_output_tokens": max_output_tokens,
        "port": _integer(
            value.get("port", 18789), "response_proxy.port", maximum=65535
        ),
    }


class NativeGoal(RawReasoningCodex):
    """Run Codex Goals with explicit, model-independent connection settings.

    The optional response proxy preserves the existing output-limit transport
    helper. It is disabled unless configured and never selects a token limit.
    Goal session capture remains unchanged. Harbor builds the standard ATIF
    trajectory, with raw provider reasoning required by
    ``RawReasoningCodex``. Summary-only reasoning is not used.
    """

    _REMOTE_CODEX_HOME = PurePosixPath("/logs/agent/codex-home")
    _REMOTE_PROXY_SCRIPT = "/logs/agent/codex-response-proxy.py"
    _REMOTE_PROXY_PID = "/logs/agent/codex-response-proxy.pid"
    _REMOTE_PROXY_READY = "/logs/agent/codex-response-proxy.ready"
    _REMOTE_PROXY_LOG = "/logs/agent/codex-response-proxy.log"

    @classmethod
    def preflight(cls, kwargs=None, env=None):
        options = dict(kwargs or {})
        _integer(options.pop("max_turns", 300), "max_turns")
        _integer(options.pop("total_timeout_sec", 43200), "total_timeout_sec")
        load_model_catalog(options.pop("model_catalog", None))
        _response_proxy(options.pop("response_proxy", None))
        super().preflight(kwargs=options, env=env)

    def __init__(self, *args, max_turns=300, total_timeout_sec=43200,
                 model_catalog=None, response_proxy=None, **kwargs):
        self.max_turns = _integer(max_turns, "max_turns")
        self.total_timeout_sec = _integer(total_timeout_sec, "total_timeout_sec")
        self.model_catalog = load_model_catalog(model_catalog)
        self.response_proxy = _response_proxy(response_proxy)
        self._proxy_base_url = None
        super().__init__(*args, **kwargs)
        if not isinstance(self.model_name, str) or not self.model_name.strip():
            raise ValueError("Model name is required")
        self._selected_provider(self._base_config)

    @staticmethod
    def _selected_provider(config):
        """Return an explicitly configured provider, leaving built-ins to Codex."""
        providers = config.get("model_providers", {})
        if not isinstance(providers, dict):
            raise ValueError("config.model_providers must be a table")
        selected = config.get("model_provider")
        if selected is not None and (not isinstance(selected, str) or not selected):
            raise ValueError("config.model_provider must be a nonempty string")
        if selected not in providers:
            return None
        provider = providers[selected]
        if not isinstance(provider, dict):
            raise ValueError("The selected model provider must be a table")
        key = provider.get("env_key")
        if key is not None and (
            not isinstance(key, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key)
        ):
            raise ValueError("The selected provider env_key must be an environment name")
        url = provider.get("base_url")
        if url is not None and (not isinstance(url, str) or not url.strip()):
            raise ValueError("The selected provider base_url must be a nonempty string")
        return provider

    def _build_effective_config(self, openai_base_url=None):
        # Apply Harbor's MCP merge without injecting an unrelated top-level
        # OpenAI endpoint into a selected custom provider.
        config = super()._build_effective_config(None)
        provider = self._selected_provider(config)
        if openai_base_url:
            if provider is None:
                config["openai_base_url"] = openai_base_url
            else:
                provider["base_url"] = openai_base_url.rstrip("/")
                if "openai_base_url" in config:
                    config["openai_base_url"] = openai_base_url
        if self._proxy_base_url:
            if provider is not None:
                provider["base_url"] = self._proxy_base_url
            config["openai_base_url"] = self._proxy_base_url
        if self.model_catalog is not None:
            config["model_catalog_json"] = str(self._REMOTE_CODEX_HOME / "models.json")
        return config

    async def _upload_effective_config(self, environment, config, remote_path):
        if self.model_catalog is not None:
            catalog_path = self.logs_dir / "native-goal-models.json"
            catalog_path.write_text(json.dumps(self.model_catalog, ensure_ascii=False, indent=2))
            await self._upload_agent_owned_file(
                environment, catalog_path, config["model_catalog_json"]
            )
        await super()._upload_effective_config(environment, config, remote_path)

    def _configured_base_url(self, access):
        """Keep an ambient default-provider endpoint away from explicit routing."""
        provider = self._selected_provider(self._base_config)
        if provider is None:
            return access.configured_base_url
        for name in ("OPENAI_BASE_URL", "OPENAI_API_BASE"):
            if self.extra_env.get(name):
                return self.extra_env[name]
        if provider.get("base_url"):
            return None
        if (
            provider.get("env_key") == "OPENAI_API_KEY"
            or provider.get("requires_openai_auth")
        ):
            return access.configured_base_url
        return None

    def _uses_openai_auth(self, config):
        provider = self._selected_provider(config)
        return provider is None or (
            provider.get("env_key") == "OPENAI_API_KEY"
            or bool(provider.get("requires_openai_auth"))
        )

    def _run_env(self, access, config):
        env = dict(self.extra_env)
        provider = self._selected_provider(config)
        if provider is None:
            env.update(access.env)
        env["CODEX_HOME"] = str(self._REMOTE_CODEX_HOME)
        key_name = provider.get("env_key") if provider is not None else "OPENAI_API_KEY"
        if key_name:
            key = self._get_env(key_name)
            if key is None and provider is None:
                key = access.api_key
            if provider is not None and not key:
                raise ValueError(
                    f"The configured provider requires environment variable {key_name}"
                )
            if key is not None:
                env[key_name] = key
        if (
            provider is not None
            and provider.get("requires_openai_auth")
            and access.api_key is not None
        ):
            env["OPENAI_API_KEY"] = access.api_key
        endpoint = self._configured_base_url(access)
        if endpoint:
            env["OPENAI_BASE_URL"] = endpoint
        return env

    async def _start_proxy(self, environment, access, config, env):
        if self.response_proxy is None:
            return
        provider = self._selected_provider(config)
        upstream = (
            provider.get("base_url") if provider is not None
            else config.get("openai_base_url") or access.base_url
        )
        if not upstream:
            raise ValueError("response_proxy requires a configured upstream base URL")
        await self.exec_as_root(environment, command="mkdir -p /logs/agent")
        upstream_api = self.response_proxy["upstream_api"]
        helper_name = (
            "native_goal_chat_proxy.py"
            if upstream_api == "chat_completions"
            else "native_goal_response_proxy.py"
        )
        helper = Path(__file__).with_name(helper_name)
        await self._upload_agent_owned_file(environment, helper, self._REMOTE_PROXY_SCRIPT)
        port = self.response_proxy["port"]
        max_output_tokens = self.response_proxy["max_output_tokens"]
        max_output_arg = (
            f"--max-output-tokens {max_output_tokens} "
            if max_output_tokens is not None
            else ""
        )
        command = (
            f"rm -f {self._REMOTE_PROXY_PID} {self._REMOTE_PROXY_READY}; "
            f"nohup python3 {self._REMOTE_PROXY_SCRIPT} "
            f"--listen 127.0.0.1:{port} "
            f"--upstream-base-url {shlex.quote(upstream)} "
            f"{max_output_arg}"
            f"--ready-file {self._REMOTE_PROXY_READY} "
            f">{self._REMOTE_PROXY_LOG} 2>&1 </dev/null & "
            f"echo $! > {self._REMOTE_PROXY_PID}; "
            "for i in $(seq 1 50); do "
            f"if test -f {self._REMOTE_PROXY_READY}; then exit 0; fi; "
            "sleep 0.1; "
            "done; "
            f"cat {self._REMOTE_PROXY_LOG}; exit 1"
        )
        await self.exec_as_agent(environment, command=command, env=env)
        self._proxy_base_url = f"http://127.0.0.1:{port}/v1"

    async def _stop_proxy(self, environment):
        try:
            await self.exec_as_agent(
                environment,
                command=(
                    f"if test -f {self._REMOTE_PROXY_PID}; then "
                    f"kill $(cat {self._REMOTE_PROXY_PID}) 2>/dev/null || true; "
                    "fi; "
                    f"rm -f {self._REMOTE_PROXY_PID} {self._REMOTE_PROXY_READY} "
                    f"{self._REMOTE_PROXY_SCRIPT}"
                ),
            )
        finally:
            self._proxy_base_url = None

    async def run(self, instruction, environment, context):
        home = str(self._REMOTE_CODEX_HOME)
        access = self.model_connection
        endpoint = self._configured_base_url(access)
        config = self._build_effective_config(endpoint)
        provider = self._selected_provider(config)
        if provider is not None and not provider.get("base_url"):
            raise ValueError("The selected custom provider requires a base_url")
        env = self._run_env(access, config)
        try:
            await self._start_proxy(environment, access, config, env)
            await self.exec_as_agent(environment, command=f"mkdir -p {home}", env=env)
            config = self._build_effective_config(endpoint)
            await self._upload_effective_config(environment, config, home + "/config.toml")
            if self._uses_openai_auth(config):
                await self.exec_as_agent(
                    environment,
                    command=("umask 077; python3 -c " + shlex.quote(
                        "import json,os; from pathlib import Path; "
                        "Path(os.environ['CODEX_HOME'],'auth.json').write_text("
                        "json.dumps({'OPENAI_API_KEY':os.environ.get('OPENAI_API_KEY','')}))"
                    )),
                    env=env,
                )
            driver = Path(__file__).with_name("native_goal_driver.py")
            await self._upload_agent_owned_file(
                environment, driver, "/logs/agent/native-goal-driver.py"
            )
            request = {
                "model": self.model_name,
                "instruction": instruction,
                "max_turns": self.max_turns,
                "timeout_sec": self.total_timeout_sec,
                "lkm_enabled": False,
                "cli_flags": shlex.split(self.build_cli_flags()),
            }
            request_path = self.logs_dir / "native-goal-request.json"
            request_path.write_text(json.dumps(request, indent=2))
            await self._upload_agent_owned_file(
                environment, request_path, "/logs/agent/native-goal-request.json"
            )
            result = await self.exec_as_agent(
                environment,
                command=(
                    "set -o pipefail; python3 /logs/agent/native-goal-driver.py "
                    "--request /logs/agent/native-goal-request.json "
                    "2>&1 </dev/null | tee /logs/agent/codex.txt"
                ),
                env=env,
            )
            if result.return_code:
                raise RuntimeError("Native Goal driver failed; inspect codex.txt")
        finally:
            try:
                await self.exec_as_agent(
                    environment,
                    command=(
                        f"rm -f {home}/auth.json; "
                        f"if [ -d {home}/sessions ]; then "
                        f"cp -R {home}/sessions /logs/agent/sessions; fi"
                    ),
                    env={"CODEX_HOME": home},
                )
            finally:
                if self.response_proxy is not None:
                    await self._stop_proxy(environment)

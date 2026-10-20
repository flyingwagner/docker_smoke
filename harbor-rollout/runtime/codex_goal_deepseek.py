"""Native Goal adapter for the Harbor BenchmarkJob rollout runtime."""
import json
from pathlib import Path, PurePosixPath
import shlex

from harbor.agents.installed.codex import Codex


class CodexGoal(Codex):
    _REMOTE_CODEX_HOME = PurePosixPath('/logs/agent/codex-home')
    _REMOTE_PROXY_SCRIPT = '/logs/agent/codex-response-proxy.py'
    _REMOTE_PROXY_PID = '/logs/agent/codex-response-proxy.pid'
    _REMOTE_PROXY_READY = '/logs/agent/codex-response-proxy.ready'
    _REMOTE_PROXY_LOG = '/logs/agent/codex-response-proxy.log'
    _MODEL_ALIASES = {
        'deepseek-flash',
        'deepseek-v4.1-flash',
        'self-deepseek-v4.1-flash-1017',
    }

    @classmethod
    def preflight(cls, kwargs=None, env=None):
        options = dict(kwargs or {})
        if int(options.pop('max_turns', 300)) < 1:
            raise ValueError('max_turns must be positive')
        if int(options.pop('total_timeout_sec', 43200)) < 1:
            raise ValueError('total_timeout_sec must be positive')
        super().preflight(kwargs=options, env=env)

    def __init__(self, *args, max_turns=300, total_timeout_sec=43200, **kwargs):
        self.max_turns = int(max_turns)
        self.total_timeout_sec = int(total_timeout_sec)
        self._proxy_base_url = None
        super().__init__(*args, **kwargs)

    def _build_effective_config(self, openai_base_url=None):
        if self.model_name not in self._MODEL_ALIASES:
            raise ValueError(f'Unsupported rollout model: {self.model_name}')
        config = super()._build_effective_config(openai_base_url)
        url = config.pop('openai_base_url', None)
        if not url:
            raise ValueError('OPENAI_BASE_URL is required')
        config['model_provider'] = 'deepseek'
        config.setdefault('model_providers', {})['deepseek'] = {
            'name': 'DeepSeek', 'base_url': url.rstrip('/'),
            'wire_api': 'responses', 'env_key': 'OPENAI_API_KEY',
            'supports_websockets': False,
        }
        config['model_catalog_json'] = str(self._REMOTE_CODEX_HOME / 'models.json')
        config['model_context_window'] = 1000000
        if self._proxy_base_url:
            config['openai_base_url'] = self._proxy_base_url
            config['model_providers']['deepseek']['base_url'] = self._proxy_base_url.rstrip('/')
        return config

    async def _start_proxy(self, environment, access):
        if not access.configured_base_url:
            raise ValueError('OPENAI_BASE_URL is required')
        await self._upload_agent_owned_file(
            environment, Path(__file__).with_name('codex_response_proxy.py'),
            self._REMOTE_PROXY_SCRIPT,
        )
        port = 18789
        command = (
            f'rm -f {self._REMOTE_PROXY_PID} {self._REMOTE_PROXY_READY}; '
            f'nohup python3 {self._REMOTE_PROXY_SCRIPT} '
            f'--listen 127.0.0.1:{port} '
            f'--upstream-base-url {shlex.quote(access.configured_base_url)} '
            f'--ready-file {self._REMOTE_PROXY_READY} '
            f'>{self._REMOTE_PROXY_LOG} 2>&1 </dev/null & '
            f'echo $! > {self._REMOTE_PROXY_PID}; '
            'for i in $(seq 1 50); do '
            f'if test -f {self._REMOTE_PROXY_READY}; then exit 0; fi; '
            'sleep 0.1; '
            'done; '
            f'cat {self._REMOTE_PROXY_LOG}; exit 1'
        )
        await self.exec_as_agent(environment, command=command)
        self._proxy_base_url = f'http://127.0.0.1:{port}/v1'

    async def _stop_proxy(self, environment):
        try:
            await self.exec_as_agent(
                environment,
                command=(
                    f'if test -f {self._REMOTE_PROXY_PID}; then '
                    f'kill $(cat {self._REMOTE_PROXY_PID}) 2>/dev/null || true; '
                    'fi; '
                    f'rm -f {self._REMOTE_PROXY_PID} {self._REMOTE_PROXY_READY} '
                    f'{self._REMOTE_PROXY_SCRIPT}'
                ),
            )
        finally:
            self._proxy_base_url = None

    async def _upload_effective_config(self, environment, config, remote_path):
        catalog = json.loads(
            Path(__file__).with_name('codex-deepseek-flash-models.json').read_text()
        )
        for model in catalog.get('models', []):
            model['slug'] = self.model_name
            model['display_name'] = self.model_name
        catalog_path = self.logs_dir / 'rollout-models.json'
        catalog_path.write_text(json.dumps(catalog))
        await self._upload_agent_owned_file(
            environment, catalog_path,
            config['model_catalog_json'],
        )
        await super()._upload_effective_config(environment, config, remote_path)

    async def run(self, instruction, environment, context):
        home = str(self._REMOTE_CODEX_HOME)
        access = self.model_connection
        env = {'CODEX_HOME': home, 'OPENAI_API_KEY': access.api_key or ''}
        if access.configured_base_url:
            env['OPENAI_BASE_URL'] = access.configured_base_url
        try:
            await self._start_proxy(environment, access)
            await self.exec_as_agent(environment, command=f'mkdir -p {home}', env=env)
            config = self._build_effective_config(access.configured_base_url)
            await self._upload_effective_config(environment, config, home + '/config.toml')
            # The provider's env_key authenticates Codex directly. Keeping the
            # credential in the environment avoids persisting auth.json in the
            # platform's output volume, including when the Job is interrupted.
            driver = Path(__file__).with_name('codex_goal_driver.py')
            await self._upload_agent_owned_file(
                environment, driver, '/logs/agent/native-goal-driver.py')
            request = {
                'model': self.model_name, 'instruction': instruction,
                'workdir': '/app',
                'max_turns': self.max_turns, 'timeout_sec': self.total_timeout_sec,
                'lkm_enabled': False, 'cli_flags': shlex.split(self.build_cli_flags()),
            }
            request_path = self.logs_dir / 'native-goal-request.json'
            request_path.write_text(json.dumps(request, indent=2))
            await self._upload_agent_owned_file(
                environment, request_path, '/logs/agent/native-goal-request.json')
            result = await self.exec_as_agent(
                environment,
                command=('set -o pipefail; python3 /logs/agent/native-goal-driver.py '
                         '--request /logs/agent/native-goal-request.json '
                         '2>&1 </dev/null | tee /logs/agent/codex.txt'),
                env=env,
            )
            if result.return_code:
                raise RuntimeError('Native Goal driver failed; inspect the protected event log')
        finally:
            # Preserve trajectories, configuration and Goal state; remove the credential.
            await self.exec_as_agent(
                environment,
                command=(f'rm -f {home}/auth.json; '
                         f'if [ -d {home}/sessions ]; then '
                         f'cp -R {home}/sessions /logs/agent/sessions; fi'),
                env={'CODEX_HOME': home},
            )
            await self._stop_proxy(environment)

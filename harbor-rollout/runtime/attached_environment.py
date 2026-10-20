"""Harbor environment backed by the current BenchmarkJob container.

The BenchmarkJob is the isolation boundary. Harbor never asks this backend to
create Docker or Kubernetes children; its file and command operations are
mapped into a private directory inside the already-running Job.
"""

import asyncio
from contextlib import contextmanager
import json
import os
import re
import shutil
import signal
from pathlib import Path

from harbor.environments.base import BaseEnvironment, ExecResult
from harbor.environments.capabilities import EnvironmentCapabilities


def redact(text):
    for name in ("PLATFORM_ENDPOINT_KEY", "MATRIXLLMROUTER_API_KEY", "OPENAI_API_KEY"):
        value = os.environ.get(name, "")
        for secret in (value, value.strip()):
            if secret:
                text = text.replace(secret, "[REDACTED]")
    return re.sub(r"\bsk-[A-Za-z0-9_-]{8,}", "[REDACTED]", text)


class AttachedJobEnvironment(BaseEnvironment):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        output = Path(os.environ["PLATFORM_OUTPUT_PATH"]).resolve()
        self.root = output / "sandboxes" / self.session_id
        self.workdir = self.root / "app"

    @staticmethod
    def type():
        return "platform-attached-harbor-rollout"

    @property
    def capabilities(self):
        return EnvironmentCapabilities()

    def _validate_definition(self):
        # The outer BenchmarkJob image is the task environment. Harbor's task
        # Dockerfile/image fields are metadata only in this backend.
        return None

    def path(self, value):
        value = str(value)
        if value == str(self.root) or value.startswith(str(self.root) + "/"):
            return Path(value)
        for prefix in ("/logs", "/tests", "/solution", "/app", "/harbor", "/installed-agent"):
            if value == prefix or value.startswith(prefix + "/"):
                return self.root / value.lstrip("/")
        return Path(value)

    def command(self, value):
        # The image supplies /app, /logs, /tests, etc. as symlinks through the
        # active trial anchor. Keep task instructions and shell commands exact.
        return value

    @contextmanager
    def with_default_user(self, user):
        # The platform owns the Unix identity. A task's logical agent user must
        # not cause Harbor to chown files to the image's unrelated UID 1000.
        self.record("user", requested_user=user, actual_uid=os.getuid())
        with super().with_default_user(os.getuid()):
            yield

    def record(self, operation, **fields):
        self.root.mkdir(parents=True, exist_ok=True)
        with (self.root / "environment-operations.jsonl").open("a") as stream:
            stream.write(redact(json.dumps({"operation": operation, **fields})) + "\n")

    async def start(self, force_build=False):
        if force_build:
            raise ValueError("The existing platform image must be reused")
        for relative in ("app", "home", "tests", "solution", "harbor", "installed-agent",
                         "logs/agent", "logs/verifier", "logs/artifacts"):
            (self.root / relative).mkdir(parents=True, exist_ok=True)
        anchor = Path("/tmp/harbor-active")
        if anchor.is_symlink():
            if anchor.resolve() != self.root.resolve():
                raise RuntimeError("/tmp/harbor-active already belongs to a different trial")
        elif anchor.exists():
            raise RuntimeError("/tmp/harbor-active must be a symlink owned by this trial")
        else:
            anchor.symlink_to(self.root, target_is_directory=True)
        self.record("start", uid=os.getuid(), image=self.task_env_config.docker_image)
        await self._upload_environment_dir_after_start()
        # A normal Harbor Docker environment copies readme.md through its
        # Dockerfile. This attached backend does not build that Dockerfile, so
        # reproduce the task-visible file explicitly while keeping metadata and
        # repair records out of the solver workspace.
        readme = self.environment_dir / "readme.md"
        if readme.is_file():
            await self.upload_file(readme, "/app/readme.md")

    async def stop(self, delete=True):
        # Keep evidence in the Job output; the outer Job owns container teardown.
        self.record("stop", output_retained=True)

    async def upload_file(self, source_path, target_path):
        source, target = Path(source_path), self.path(target_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        if source.resolve() != target.resolve():
            shutil.copy2(source, target)
        self.record("upload_file", source=str(source), target=str(target))

    async def upload_dir(self, source_dir, target_dir):
        source, target = Path(source_dir), self.path(target_dir)
        target.mkdir(parents=True, exist_ok=True)
        if source.resolve() != target.resolve():
            shutil.copytree(source, target, dirs_exist_ok=True, symlinks=True)
        self.record("upload_dir", source=str(source), target=str(target))

    async def download_file(self, source_path, target_path):
        source, target = self.path(source_path), Path(target_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        if source.resolve() != target.resolve():
            shutil.copy2(source, target)
        self.record("download_file", source=str(source), target=str(target))

    async def download_dir(self, source_dir, target_dir):
        source, target = self.path(source_dir), Path(target_dir)
        target.mkdir(parents=True, exist_ok=True)
        if source.resolve() != target.resolve():
            shutil.copytree(source, target, dirs_exist_ok=True, symlinks=True)
        self.record("download_dir", source=str(source), target=str(target))

    async def exec(self, command, cwd=None, env=None, timeout_sec=None, user=None):
        # No privilege escalation: even Harbor's file chmod operations labelled
        # 'root' run as the existing Job UID and fail if privileges are required.
        if user not in (None, "root", "agent", os.getuid(), str(os.getuid())):
            raise ValueError("The attached platform backend cannot switch users")
        mapped = self.command(command)
        environment = os.environ.copy()
        environment.update(self._merge_env(env) or {})
        for name in ("CODEX_HOME", "JUDGE_PROBLEM", "JUDGE_SOLUTION", "JUDGE_OUT", "JUDGE_SCRIPT"):
            if name in environment:
                environment[name] = str(self.path(environment[name]))
        environment.update({
            "HOME": str(self.root / "home"),
            "USER": str(os.getuid()),
        })
        for name, value in {
            "JUDGE_PROBLEM": str(self.workdir / "readme.md"),
            "JUDGE_SOLUTION": str(self.workdir / "solution.md"),
            "JUDGE_OUT": str(self.root / "logs/verifier"),
            "JUDGE_SCRIPT": str(self.root / "tests/judge_llm.py"),
        }.items():
            environment.setdefault(name, value)
        self.record("exec", command=mapped, requested_user=user, actual_uid=os.getuid())
        process = await asyncio.create_subprocess_exec(
            "bash", "-o", "pipefail", "-c", mapped,
            cwd=self.path(cwd) if cwd else self.workdir,
            env=environment,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout_sec)
        except (asyncio.CancelledError, asyncio.TimeoutError):
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            await process.wait()
            raise
        stdout, stderr = redact(stdout.decode(errors="replace")), redact(stderr.decode(errors="replace"))
        callback = self._output_callback()
        if callback:
            if stdout:
                await callback(stdout, "stdout")
            if stderr:
                await callback(stderr, "stderr")
        return ExecResult(stdout=stdout, stderr=stderr, return_code=process.returncode)

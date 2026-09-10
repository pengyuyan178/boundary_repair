"""Bounded subprocess execution; no shell expansion and no repository-controlled executables."""
import os
from pathlib import Path
import signal
import subprocess
import tempfile
from contextlib import ExitStack
from dataclasses import dataclass
from typing import Mapping

from boundary_repair.domain.errors import BudgetExceeded, ExternalServiceError


@dataclass(frozen=True, slots=True)
class ProcessResult:
    """Raw bounded process output; sanitize before exposing errors to users."""
    returncode: int
    stdout: bytes
    stderr: bytes


def run_process(
    arguments: list[str], *, timeout: float, cwd: Path | None = None,
    input_data: bytes | None = None, max_output: int = 16000000,
    environment: Mapping[str, str] | None = None, output_file: Path | None = None,
) -> ProcessResult:
    """Execute one command, kill its group on deadline, and cap captured output after completion.

    Temporary files avoid pipe deadlocks and unbounded RAM. OS/container quotas are still needed
    for adversarial disk output. Windows uses taskkill for process descendants on timeout.
    """
    if timeout <= 0:
        raise BudgetExceeded('process_deadline')
    env = dict(environment) if environment is not None else {k: v for k, v in os.environ.items()
           if k in {'PATH', 'SYSTEMROOT', 'WINDIR', 'TEMP', 'TMP', 'HOME', 'USERPROFILE', 'PATHEXT',
                    'DOCKER_HOST', 'DOCKER_CONTEXT', 'DOCKER_CONFIG', 'DOCKER_TLS_VERIFY', 'DOCKER_CERT_PATH'}}
    env.update({'GIT_TERMINAL_PROMPT': '0', 'GIT_CONFIG_NOSYSTEM': '1', 'GIT_CONFIG_GLOBAL': os.devnull,
                'GIT_NO_REPLACE_OBJECTS': '1', 'LC_ALL': 'C.UTF-8'})
    with ExitStack() as stack:
        output = stack.enter_context(output_file.open("xb") if output_file else tempfile.TemporaryFile())
        error = stack.enter_context(tempfile.TemporaryFile())
        try:
            process = subprocess.Popen(arguments, cwd=cwd, stdin=subprocess.PIPE if input_data is not None else subprocess.DEVNULL,
                                       stdout=output, stderr=error, env=env, shell=False,
                                       start_new_session=os.name != 'nt')
        except OSError as exc:
            raise ExternalServiceError('process_start_failed') from exc
        try:
            process.communicate(input_data, timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            if os.name == 'nt':
                subprocess.run(['taskkill', '/PID', str(process.pid), '/T', '/F'], capture_output=True, timeout=10)
            else:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            process.kill()
            process.wait()
            raise BudgetExceeded('process_timeout') from exc
        if output.tell() > max_output or error.tell() > max_output:
            raise ExternalServiceError('process_output_limit')
        output.seek(0)
        error.seek(0)
        return ProcessResult(process.returncode, b"" if output_file else output.read(), error.read())

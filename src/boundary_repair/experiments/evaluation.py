"""Separate official Docker grading. No generation module imports this file or sees test results."""
from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
from typing import Protocol

from boundary_repair.adapters.process import run_process
from boundary_repair.adapters.storage import file_sha256, safe_component, write_json
from boundary_repair.config import ExperimentConfig
from boundary_repair.domain.errors import ConfigurationError, ExternalServiceError, ValidationError
from boundary_repair.kernel.codec import strict_json


@dataclass(frozen=True, slots=True)
class EvaluationRequest:
    """Only this process receives the raw benchmark/test data and the frozen prediction artifact."""
    dataset: Path
    predictions: Path
    batch_directory: Path
    evaluation_id: str
    harness_python: Path
    harness_revision: str
    image_manifest: Path


@dataclass(frozen=True, slots=True)
class EvaluationReport:
    """Grade counts are distinct from submissions and infrastructure failures."""
    evaluation_id: str
    submitted: int
    graded: int
    resolved: int
    infrastructure_errors: int
    selected: int = 0


class EvaluatorPort(Protocol):
    """Independent grader with no generator handle or repair callback."""

    def evaluate(self, request: EvaluationRequest) -> EvaluationReport:
        """Evaluate immutable predictions; persist reports only, never regenerate candidates."""
        ...


def validate_predictions(path: Path) -> tuple[dict[str, str], ...]:
    """Accept the official three-string schema and unique safe ids; reject empty patches."""
    rows, seen = [], set()
    for line in path.read_text(encoding='utf-8').splitlines():
        if not line.strip():
            continue
        row = strict_json(line)
        if set(row) != {'instance_id', 'model_name_or_path', 'model_patch'} or any(not isinstance(v, str) or not v.strip() for v in row.values()):
            raise ValidationError('invalid_prediction_schema')
        identifier = safe_component(row['instance_id'])
        if identifier in seen:
            raise ValidationError('duplicate_prediction_id')
        seen.add(identifier)
        rows.append(row)
    if not rows:
        raise ConfigurationError('no_predictions_to_evaluate')
    return tuple(rows)


def browser_audit(log: str) -> dict:
    """Require complete Chrome and Firefox runs using the existing Chart.js evaluator protocol."""
    clean = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", log).replace("\r", "\n")
    start_marker, end_marker = ">>>>> Start Test Output", ">>>>> End Test Output"
    boundaries = start_marker in clean and end_marker in clean
    if boundaries:
        clean = clean.split(start_marker, 1)[1].split(end_marker, 1)[0]
    executions = {}
    for browser, executed, total in re.findall(
        r"(Chrome|Firefox) [^\n]*?Executed (\d+) of (\d+)", clean
    ):
        executions[browser] = {"executed": int(executed), "total": int(total)}
    disconnects = [line for line in clean.splitlines() if "disconnected" in line.lower()]
    completed = boundaries and not disconnects and all(
        executions.get(browser, {}).get("total", 0) > 0
        and executions[browser]["executed"] == executions[browser]["total"]
        for browser in ("Chrome", "Firefox")
    )
    return {"both_browsers_completed": completed, "executions": executions,
            "runtime_disconnects": disconnects}


def evaluation_integrity(directory: Path, identifier: str, payload: dict) -> dict:
    """Accept a grade only with test boundaries, a compatible exit status and completed browsers."""
    path = directory / 'test_output.txt'
    if not path.is_file():
        return {'complete': False, 'reason': 'missing_test_output'}
    log = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", path.read_text(encoding='utf-8'))
    codes = re.findall(r'^>>>>> Test Exit Code: (\d+)\s*$', log, re.MULTILINE)
    start, end = log.find('>>>>> Start Test Output'), log.find('>>>>> End Test Output')
    complete = (start >= 0 and end > start and len(codes) == 1
                and not payload.get('infra_failure', False))
    code = int(codes[0]) if len(codes) == 1 else None
    complete = complete and code in {0, 1} and (payload.get('resolved') is False or code == 0)
    browser = browser_audit(log) if identifier.startswith('chartjs__Chart.js-') else None
    if browser is not None:
        complete = complete and browser['both_browsers_completed']
    return {'complete': complete, 'test_exit_code': code, 'browser_audit': browser,
            'reason': None if complete else 'incomplete_or_inconsistent_test_execution',
            'test_output_sha256': file_sha256(path)}


def parse_case_reports(root: Path, identifiers: tuple[str, ...]) -> dict[str, bool]:
    """Read per-instance grades only after validating their actual test execution logs."""
    results: dict[str, bool] = {}
    seen = set()
    for path in sorted(root.rglob('report.json')):
        data = strict_json(path.read_text(encoding='utf-8'), maximum=16000000)
        for identifier in identifiers:
            payload = data.get(identifier)
            if not isinstance(payload, dict):
                continue
            value = payload.get('resolved')
            if type(value) is not bool:
                continue
            if identifier in seen:
                raise ValidationError('duplicate_evaluation_case_report')
            seen.add(identifier)
            integrity = evaluation_integrity(path.parent, identifier, payload)
            write_json(path.parent / 'execution_integrity.json', integrity)
            if integrity['complete']:
                results[identifier] = value
    return results


@dataclass(frozen=True, slots=True)
class OfficialDockerEvaluator:
    """Adapter for the supported official legacy module CLI; verify its installed version/help first.

    This implementation is contract-tested without Docker. A server-side smoke run remains
    required. Digest checks verify declared aliases before evaluation, not adversarial races
    in a concurrently modified Docker daemon.
    """

    def evaluate(self, request: EvaluationRequest) -> EvaluationReport:
        """A8: validate artifact/version/image bindings, run harness once, then parse actual grades."""
        if os.environ.get('BOUNDARY_GENERATION_ROLE') == 'isolated_worker':
            raise ConfigurationError('evaluation_forbidden_in_generation_worker')
        rows = validate_predictions(request.predictions)
        ids = tuple(row['instance_id'] for row in rows)
        batch_manifest = strict_json((request.batch_directory / 'manifest.json').read_text(encoding='utf-8'))
        if batch_manifest.get('not_benchmark_evidence') or batch_manifest.get('execution_mode') == 'fixture':
            raise ConfigurationError('fixture_batches_cannot_be_officially_evaluated')
        selected = batch_manifest.get('selected_instances', [])
        if not isinstance(selected, list) or not set(ids) <= set(selected):
            raise ValidationError('prediction_not_in_selected_batch')
        expected_dataset = batch_manifest.get('evaluation_dataset_sha256', batch_manifest.get('dataset_sha256'))
        if expected_dataset != file_sha256(request.dataset):
            raise ValidationError('dataset_changed_since_generation')
        if batch_manifest.get('protocol') == 'isolated-generation-v1':
            frozen = strict_json((request.batch_directory / 'generation_frozen.json').read_text(encoding='utf-8'))
            if frozen['protocol'] != batch_manifest['protocol'] or frozen['attempted'] != len(selected):
                raise ValidationError('complete_isolated_generation_required')
            for name, key in (('manifest.json', 'manifest_sha256'), ('results.jsonl', 'results_sha256'),
                              ('predictions.jsonl', 'predictions_sha256')):
                if file_sha256(request.batch_directory / name) != frozen[key]:
                    raise ValidationError('frozen_generation_artifact_changed')
            if file_sha256(request.image_manifest) != frozen['image_manifest_sha256']:
                raise ValidationError('frozen_image_manifest_changed')
            frozen_rows = validate_predictions(request.batch_directory / 'predictions.jsonl')
            if any(row not in frozen_rows for row in rows):
                raise ValidationError('prediction_not_in_frozen_generation')
        manifest = strict_json(request.image_manifest.read_text(encoding='utf-8'))
        options = manifest.get('evaluation', {})
        if not isinstance(options, dict) or set(options) - {'namespace', 'instance_image_tag', 'max_workers', 'timeout_seconds'}:
            raise ConfigurationError('invalid_evaluation_options')
        workers, timeout = options.get('max_workers', 1), options.get('timeout_seconds', 3600)
        if type(workers) is not int or not 1 <= workers <= 32 or type(timeout) is not int or not 1 <= timeout <= 14400:
            raise ConfigurationError('invalid_evaluation_budget')
        python = str(request.harness_python)
        metadata = run_process([python, '-c',
            'import importlib.metadata,json,swebench;print(json.dumps({"version":importlib.metadata.version("swebench"),"file":swebench.__file__}))'], timeout=30)
        if metadata.returncode:
            raise ConfigurationError('selected_interpreter_cannot_import_swebench')
        installed = strict_json(metadata.stdout.decode())
        expected = request.harness_revision
        if expected.startswith('git:'):
            commit = expected[4:]
            if not re.fullmatch(r'[0-9a-f]{40}', commit):
                raise ConfigurationError('full_harness_git_commit_required')
            git = run_process(['git', '-C', str(Path(installed['file']).parent), 'rev-parse', 'HEAD'], timeout=30)
            if git.returncode or git.stdout.decode().strip() != commit:
                raise ConfigurationError('harness_git_revision_mismatch')
        elif installed['version'] != expected.removeprefix('version:'):
            raise ConfigurationError('harness_package_version_mismatch')
        help_result = run_process([python, '-m', 'swebench.harness.run_evaluation', '--help'], timeout=30)
        help_text = help_result.stdout.decode(errors='replace')
        flags = ['--dataset_name', '--predictions_path', '--run_id', '--max_workers', '--timeout']
        if help_result.returncode or any(flag not in help_text for flag in flags):
            raise ConfigurationError('installed_harness_cli_not_supported')
        image_bindings = []
        for identifier in ids:
            item = manifest.get('instances', {}).get(identifier)
            if not isinstance(item, dict):
                raise ConfigurationError('evaluation_instance_image_missing')
            image, alias = item.get('image'), item.get('harness_image')
            if not isinstance(image, str) or not re.fullmatch(r'[A-Za-z0-9._:/-]+@sha256:[0-9a-f]{64}', image) or not isinstance(alias, str) or not alias or alias.startswith('-'):
                raise ConfigurationError('pinned_image_and_harness_alias_required')
            hashes = []
            for name in (image, alias):
                check = run_process(['docker', 'image', 'inspect', '--format={{.Id}}', name], timeout=30)
                if check.returncode:
                    raise ConfigurationError('evaluation_image_not_present')
                hashes.append(check.stdout.decode().strip())
            if hashes[0] != hashes[1] or not hashes[0].startswith('sha256:'):
                raise ConfigurationError('harness_alias_not_bound_to_pinned_image')
            image_bindings.append({'instance_id': identifier, 'digest': image, 'alias': alias, 'local_image_id': hashes[0]})
        evaluation_id = safe_component(request.evaluation_id)
        root = request.batch_directory / 'evaluation' / evaluation_id
        root.mkdir(parents=True, exist_ok=False)
        harness_dataset = request.dataset
        if request.dataset.suffix.lower() == '.json':
            raw_dataset = json.loads(request.dataset.read_text(encoding='utf-8-sig'))
            if isinstance(raw_dataset, dict):
                if any(not isinstance(row, dict) or row.get('instance_id') != key
                       for key, row in raw_dataset.items()):
                    raise ValidationError('dataset_mapping_id_mismatch')
                harness_dataset = root / 'dataset.json'
                write_json(harness_dataset, list(raw_dataset.values()))
        prediction_hash = file_sha256(request.predictions)
        # Unique cwd + evaluation id + content hash prevent accidental cache reuse.
        run_id = safe_component(evaluation_id[:96] + '-' + prediction_hash[:16])
        arguments = [python, '-m', 'swebench.harness.run_evaluation', '--dataset_name', str(harness_dataset),
                     '--predictions_path', str(request.predictions), '--run_id', run_id,
                     '--max_workers', str(workers), '--timeout', str(timeout)]
        for key in ('namespace', 'instance_image_tag'):
            if key in options:
                if '--' + key not in help_text or not isinstance(options[key], str) or not options[key]:
                    raise ConfigurationError('unsupported_harness_image_option')
                arguments.extend(['--' + key, options[key]])
        write_json(root / 'request.json', {'arguments': arguments, 'prediction_sha256': prediction_hash,
                   'dataset_sha256': file_sha256(request.dataset),
                   'harness_dataset_sha256': file_sha256(harness_dataset), 'installed_harness': installed,
                   'image_bindings': image_bindings, 'runtime_image_race_protection': False})
        # Bounded outer deadline accommodates per-instance execution and image setup. No retries.
        result = run_process(arguments, cwd=root, timeout=(timeout + 600) * len(ids) + 300,
                             max_output=64000000, output_file=root / 'harness.stdout.log')
        (root / 'harness.stderr.log').write_bytes(result.stderr)
        if file_sha256(request.predictions) != prediction_hash:
            raise ValidationError('predictions_modified_during_evaluation')
        grades = parse_case_reports(root, ids)
        for identifier in ids:
            write_json(request.batch_directory / 'cases' / safe_component(identifier) / 'result_data' /
                       ('evaluation_' + evaluation_id + '.json'),
                       {'instance_id': identifier, 'resolved': grades.get(identifier),
                        'status': 'graded' if identifier in grades else 'infrastructure_or_missing_report',
                        'evaluation_id': evaluation_id})
        report = EvaluationReport(evaluation_id, len(rows), len(grades), sum(grades.values()), len(rows)-len(grades), len(selected))
        write_json(root / 'evaluation_summary.json', {'report': report, 'harness_returncode': result.returncode,
                   'prediction_sha256': prediction_hash, 'denominator_is_selected_not_just_graded': True})
        return report


def run_evaluation(config: ExperimentConfig, batch_directory: Path, evaluation_id: str,
                   evaluator: EvaluatorPort) -> EvaluationReport:
    """Require explicit server/Docker/version configuration and an existing generated batch."""
    if config.target != 'server' or config.isolation != 'docker':
        raise ConfigurationError('official_evaluation_requires_server_docker')
    if not config.harness_python or not config.harness_revision or not config.image_manifest:
        raise ConfigurationError('harness_python_revision_and_manifest_required')
    if not config.harness_python.is_file() or not config.image_manifest.is_file():
        raise ConfigurationError('harness_or_manifest_not_found')
    batch_directory = batch_directory.resolve()
    if not batch_directory.is_relative_to(config.results_root.resolve()):
        raise ConfigurationError('evaluation_batch_outside_results_root')
    predictions = batch_directory / 'predictions.jsonl'
    if not predictions.is_file():
        raise ConfigurationError('predictions_not_found')
    return evaluator.evaluate(EvaluationRequest(config.dataset, predictions, batch_directory,
        safe_component(evaluation_id), config.harness_python, config.harness_revision, config.image_manifest))

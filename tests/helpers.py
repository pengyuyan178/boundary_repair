"""Synthetic inputs shared by tests; no real task, gold patch, or provider credentials."""
from dataclasses import replace
import json
import os
from pathlib import Path
import shutil
import subprocess

from boundary_repair.config import IntegrationSettings, load_config
from boundary_repair.domain.runtime import RunContext, SearchPolicy, BudgetLedger, BudgetLimits
from boundary_repair.domain.task import RepositorySnapshot, TaskInput
from boundary_repair.kernel.files import tree_digest
from boundary_repair.kernel.terms import literal as L, symbol as S, term_json
from boundary_repair.domain.specification import Term

ROOT = Path(__file__).resolve().parents[1]
SOURCE = '// 中文 🐈\nfunction shouldShow(active, hidden) { return active || hidden; }\nmodule.exports = {shouldShow};\n'
ISSUE = ('shouldShow is a pure Boolean entry function with Boolean active and hidden arguments.\n'
         'When active=true and hidden=false, return true.\n'
         'When active=true and hidden=true, return false.\n'
         'When active=false and hidden=false, keep returning false.\n'
         'When active=false and hidden=true, return false.\n')


def context():
    return RunContext('synthetic', 'demo__ui-1', 42, SearchPolicy(), BudgetLedger(BudgetLimits()))


def parser_module():
    candidates = [os.environ.get('BOUNDARY_TEST_TYPESCRIPT', ''), str(ROOT / 'node_modules/typescript'),
                  '/usr/local/slides_js/node_modules/typescript',
                  '/opt/nvm/versions/node/v22.16.0/lib/node_modules/typescript']
    for raw in candidates:
        if raw and (Path(raw) / 'package.json').is_file():
            return Path(raw)
    return None


def config(root, parser=True):
    result = load_config(ROOT / 'configs/local.json', root)
    settings = IntegrationSettings(parser_module=parser_module(), parser_mode='typescript' if parser else 'text',
                                   workspace_mode='git_archive')
    return replace(result, integration=settings)


def evidence():
    cases = [(True, False, True), (True, True, False), (False, False, False), (False, True, False)]
    lines = ISSUE.splitlines()[1:]
    sources, claims = [], []
    for i, (active, hidden, expected) in enumerate(cases):
        sources.append({'source_id': f's{i}', 'kind': 'issue_text', 'locator': lines[i]})
        guard = Term('and', (Term('eq', (S('active'), L(active))), Term('eq', (S('hidden'), L(hidden)))))
        claims.append({'claim_id': f'c{i}', 'kind': 'frame' if i == 2 else 'requirement', 'source_ids': [f's{i}'],
                       'description': lines[i], 'targets': [{'entity_id': 'shouldShow', 'property_name': 'return',
                                                            'context': term_json(guard)}],
                       'relation': term_json(Term('eq', (S('return'), L(expected))))})
    return {'sources': sources, 'claims': claims, 'choice_groups': []}


def write_fixture(root):
    path = root / 'responses.json'
    path.write_text(json.dumps({'responses': {'evidence.v1': evidence()}}), encoding='utf-8')
    return path


def snapshot(root):
    code = root / 'source'
    code.mkdir()
    (code / 'ui.js').write_text(SOURCE, encoding='utf-8')
    return RepositorySnapshot(code, 'a' * 40, tree_digest(code))


def task(commit='a' * 40):
    return TaskInput('demo__ui-1', 'demo/ui', commit, ISSUE)


def git_repo(root):
    repo = root / 'original'
    repo.mkdir()
    (repo / 'ui.js').write_text(SOURCE, encoding='utf-8')
    (repo / 'tests').mkdir()
    (repo / 'tests/gold.txt').write_text('should never enter snapshot', encoding='utf-8')
    (repo / '.env').write_text('SECRET=not_a_real_secret', encoding='utf-8')
    env = os.environ.copy()
    env.update({'GIT_AUTHOR_NAME': 'Test', 'GIT_AUTHOR_EMAIL': 'test@example.invalid',
                'GIT_COMMITTER_NAME': 'Test', 'GIT_COMMITTER_EMAIL': 'test@example.invalid'})
    for args in (['init', '-q'], ['add', '.'], ['commit', '-qm', 'synthetic base']):
        subprocess.run(['git', '-C', str(repo), *args], env=env, check=True, capture_output=True)
    commit = subprocess.check_output(['git', '-C', str(repo), 'rev-parse', 'HEAD']).decode().strip()
    return repo, commit

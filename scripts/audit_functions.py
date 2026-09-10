"""Generate a source-level function inventory and attach measured coverage, without claiming correctness."""
from __future__ import annotations

import argparse
import ast
import json
from pathlib import Path
from typing import Iterator


def functions(nodes: list[ast.stmt], prefix: str = '', protocol: bool = False) -> Iterator[tuple[str, ast.FunctionDef | ast.AsyncFunctionDef, bool]]:
    """Yield qualified definitions, including nested helpers, and identify deliberate Protocol declarations."""
    for node in nodes:
        if isinstance(node, ast.ClassDef):
            is_protocol = any(ast.unparse(base).endswith('Protocol') for base in node.bases)
            yield from functions(node.body, prefix + node.name + '.', is_protocol)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            yield prefix + node.name, node, protocol
            yield from functions(node.body, prefix + node.name + '.', False)


def build_inventory(root: Path, coverage: dict[str, object]) -> list[dict[str, object]]:
    """Match exact source definitions to coverage function records; body coverage is not a behavior oracle."""
    rows = []
    for path in sorted((root / 'src' / 'boundary_repair').rglob('*.py')):
        relative = path.relative_to(root).as_posix()
        source = path.read_text(encoding='utf-8')
        tree = ast.parse(source)
        record = coverage.get('files', {}).get(relative, {})
        for name, node, protocol in functions(tree.body):
            measured = record.get('functions', {}).get(name, {})
            status = 'interface_declaration' if protocol else 'concrete_implementation'
            summary = measured.get('summary', {})
            rows.append({'file': relative, 'line': node.lineno, 'function': name,
                         'signature': f'{name}({ast.unparse(node.args)}) -> {ast.unparse(node.returns) if node.returns else "UNANNOTATED"}',
                         'status': status, 'docstring': ast.get_docstring(node),
                         'coverage': summary, 'missing_lines': measured.get('missing_lines', []),
                         'body_exercised': bool(summary.get('covered_lines', 0)) if not protocol else None,
                         'coverage_is_not_correctness': True})
    return rows


def main(argv: list[str] | None = None) -> int:
    """Write the inventory and generated API reference against an explicit coverage JSON file."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--coverage', type=Path, default=Path('verification/coverage.json'))
    args = parser.parse_args(argv)
    root = Path(__file__).resolve().parents[1]
    measured = json.loads(args.coverage.read_text(encoding='utf-8'))
    rows = build_inventory(root, measured)
    payload = {'schema_version': 2, 'method_version': '0.2.0', 'coverage_source': str(args.coverage),
               'functions': rows, 'warning': 'Execution coverage and mock tests do not establish real-service or benchmark correctness.'}
    for path in (root / 'IMPLEMENTATION_MAP.json', root / 'verification' / 'function_audit.json'):
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    lines = ['# 函数 API 与注释索引', '', '由 scripts/audit_functions.py 从实际源码生成。覆盖只是执行证据，不是正确性证明。', '']
    current = ''
    for row in rows:
        if row['file'] != current:
            current = row['file']
            lines.extend(['## ' + current, ''])
        lines.extend(['### ' + row['function'], '', '```python', row['signature'], '```', '',
                      row['docstring'] or 'MISSING DOCSTRING', '',
                      f"状态：{row['status']}；语句执行：{row['coverage'].get('covered_lines', 0)}/{row['coverage'].get('num_statements', 0)}。", ''])
    (root / 'docs' / 'API_REFERENCE.md').write_text('\n'.join(lines), encoding='utf-8')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

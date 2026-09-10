'use strict';
/** Trusted parse-only IPC. Target code is passed as text, never imported or evaluated. */
const fs = require('node:fs');
const path = require('node:path');
const crypto = require('node:crypto');
const modulePath = process.argv[2];
const ts = modulePath ? require(path.resolve(modulePath)) : require('typescript');
if (ts.version !== '5.8.3') throw Error('requires TypeScript 5.8.3');
const input = JSON.parse(fs.readFileSync(0, 'utf8'));

/** Count compiler AST nodes without following parent backreferences. */
function countNodes(node) {
  let count = 1;
  ts.forEachChild(node, child => { count += countNodes(child); });
  return count;
}
if (!Array.isArray(input.files) || input.files.length > 5000) throw Error('invalid IPC');

/** Convert the accepted Boolean AST subset to the portable logic IR. */
function booleanTerm(node, parameters) {
  if (ts.isParenthesizedExpression(node)) return booleanTerm(node.expression, parameters);
  if (node.kind === ts.SyntaxKind.TrueKeyword || node.kind === ts.SyntaxKind.FalseKeyword)
    return {op: 'literal', value: node.kind === ts.SyntaxKind.TrueKeyword};
  if (ts.isIdentifier(node) && parameters.includes(node.text)) return {op: 'symbol', value: node.text};
  if (ts.isPrefixUnaryExpression(node) && node.operator === ts.SyntaxKind.ExclamationToken) {
    const child = booleanTerm(node.operand, parameters);
    return child ? {op: 'not', args: [child]} : null;
  }
  if (ts.isBinaryExpression(node)) {
    const ops = new Map([[ts.SyntaxKind.AmpersandAmpersandToken, 'and'],
      [ts.SyntaxKind.BarBarToken, 'or'], [ts.SyntaxKind.EqualsEqualsEqualsToken, 'eq'],
      [ts.SyntaxKind.ExclamationEqualsEqualsToken, 'ne']]);
    const op = ops.get(node.operatorToken.kind);
    const a = booleanTerm(node.left, parameters), b = booleanTerm(node.right, parameters);
    return op && a && b ? {op, args: [a, b]} : null;
  }
  if (ts.isConditionalExpression(node)) {
    const args = [node.condition, node.whenTrue, node.whenFalse].map(n => booleanTerm(n, parameters));
    return args.every(Boolean) ? {op: 'ite', args} : null;
  }
  return null;
}

/** Parse a file and return exact UTF-8 byte spans, not UTF-16 offsets. */
function parseFile(file) {
  if (typeof file.path !== 'string' || typeof file.source !== 'string') throw Error('invalid file');
  const ext = path.extname(file.path).toLowerCase();
  const kind = {'.tsx': ts.ScriptKind.TSX, '.jsx': ts.ScriptKind.JSX,
    '.ts': ts.ScriptKind.TS, '.js': ts.ScriptKind.JS, '.mjs': ts.ScriptKind.JS,
    '.cjs': ts.ScriptKind.JS}[ext];
  if (!kind) return {path: file.path, locations: [], functions: [], identifiers: [], unsupported: ['non_js_source']};
  const sf = ts.createSourceFile(file.path, file.source, ts.ScriptTarget.Latest, true, kind);
  const diagnostics = sf.parseDiagnostics || [];
  if (diagnostics.length) return {path: file.path, locations: [], functions: [], identifiers: [],
    unsupported: diagnostics.map(d => 'syntax:' + d.code)};
  const locations = [], functions = [], identifiers = new Set();
  const seen = new Set();
  /** Build a unique exact-content range and retain its syntax category. */
  function span(node, nodeKind, symbol = '') {
    const start = node.getStart(sf), end = node.end;
    const key = `${start}:${end}:${nodeKind}`;
    const result = {path: file.path, start_line: sf.getLineAndCharacterOfPosition(start).line + 1,
      end_line: sf.getLineAndCharacterOfPosition(Math.max(start, end-1)).line + 1,
      content_sha256: crypto.createHash('sha256').update(file.source.slice(start, end), 'utf8').digest('hex'),
      start_byte: Buffer.byteLength(file.source.slice(0, start), 'utf8'),
      end_byte: Buffer.byteLength(file.source.slice(0, end), 'utf8'), node_kind: nodeKind, symbol, node_count: countNodes(node)};
    if (!seen.has(key)) { locations.push(result); seen.add(key); }
    return result;
  }
  /** Extract small entry-function models only if every statement and read is supported. */
  function visit(node) {
    if (ts.isIdentifier(node)) identifiers.add(node.text);
    if (ts.isFunctionDeclaration(node) || ts.isFunctionExpression(node) || ts.isArrowFunction(node)) {
      const name = node.name?.text || (ts.isVariableDeclaration(node.parent) ? node.parent.name.getText(sf) : '');
      if (name) span(node, 'Function', name);
      const params = node.parameters.map(p => ts.isIdentifier(p.name) && !p.initializer && !p.dotDotDotToken ? p.name.text : null);
      const asyncNode = node.modifiers?.some(m => m.kind === ts.SyntaxKind.AsyncKeyword) || node.asteriskToken;
      const body = node.body;
      const expression = body && ts.isBlock(body)
        ? (body.statements.length === 1 && ts.isReturnStatement(body.statements[0]) ? body.statements[0].expression : null)
        : body;
      if (name && expression && params.every(Boolean) && new Set(params).size === params.length && params.length <= 8 && !asyncNode) {
        const term = booleanTerm(expression, params);
        if (term) functions.push({name, params, expression: term, site: span(expression, 'BooleanReturn', name),
          premise: 'direct_function_entry_boolean_arguments_identity_continuation'});
      }
    }
    if (ts.isIfStatement(node)) span(node.expression, 'Condition');
    if (ts.isConditionalExpression(node)) span(node.condition, 'Condition');
    if (ts.isJsxAttribute(node) && node.initializer) span(node.initializer, 'Consumer', node.name.getText(sf));
    if (ts.isJsxExpression(node) && node.expression) span(node.expression, 'Consumer', 'children');
    if (ts.isRegularExpressionLiteral(node)) span(node, 'RegExp');
    if (ts.isBinaryExpression(node) && node.operatorToken.kind === ts.SyntaxKind.EqualsToken)
      span(node, 'Assignment', node.left.getText(sf));
    ts.forEachChild(node, visit);
  }
  visit(sf);
  return {path: file.path, locations, functions, identifiers: [...identifiers].sort(), unsupported: []};
}

const output = {parser: 'typescript', version: ts.version, files: input.files.map(parseFile)};
process.stdout.write(JSON.stringify(output));

'use strict';
/** Trusted parse-only IPC. Target code is passed as text, never imported or evaluated. */
const fs = require('node:fs');
const path = require('node:path');
const crypto = require('node:crypto');
const modulePath = process.argv[2];
const ts = modulePath ? require(path.resolve(modulePath)) : require('typescript');
if (ts.version !== '5.8.3') throw Error('requires TypeScript 5.8.3');
const input = JSON.parse(fs.readFileSync(0, 'utf8'));

const LIMITS = Object.freeze({locations: 4096, functions: 512, identifiers: 8192,
  consumerGuards: 4096, consumerGuardBytes: 1024 * 1024, metadataBytes: 6 * 1024 * 1024,
  symbolBytes: 4096, syntaxDiagnostics: 256, editBlocks: 4096});

/** Count compiler AST nodes without following parent backreferences or recursing on source depth. */
function countNodes(node) {
  let count = 0;
  const work = [node];
  while (work.length) {
    const current = work.pop();
    count += 1;
    const children = [];
    ts.forEachChild(current, child => { children.push(child); });
    for (let index = children.length - 1; index >= 0; index -= 1) work.push(children[index]);
  }
  return count;
}

/** Hash every AST token in the legacy JSON representation without retaining an unbounded token array. */
function semanticHash(sourceFile, directives) {
  try {
    const hash = crypto.createHash('sha256');
    hash.update('[[');
    let first = true;
    const work = [sourceFile];
    while (work.length) {
      const node = work.pop();
      if (node.kind >= ts.SyntaxKind.FirstJSDocNode && node.kind <= ts.SyntaxKind.LastJSDocNode) continue;
      const children = node.getChildren(sourceFile);
      if (!children.length) {
        if (!first) hash.update(',');
        first = false;
        hash.update('[');
        hash.update(String(node.kind));
        hash.update(',');
        hash.update(JSON.stringify(node.getText(sourceFile)));
        hash.update(']');
      } else {
        for (let index = children.length - 1; index >= 0; index -= 1) work.push(children[index]);
      }
    }
    hash.update('],[');
    directives.forEach((directive, index) => {
      if (index) hash.update(',');
      hash.update(JSON.stringify(directive));
    });
    hash.update(']]');
    return hash.digest('hex');
  } catch (_) {
    // A partial or failed traversal must never be represented by a misleading semantic fingerprint.
    return null;
  }
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

/** Check atomicity without recursing on adversarial nesting. */
function atomic(node) {
  const work = [node];
  while (work.length) {
    const current = work.pop();
    if (ts.isFunctionLike(current) || ts.isClassLike(current) || ts.isBlock(current)) return false;
    ts.forEachChild(current, child => { work.push(child); });
  }
  return true;
}

function emptyResult(filePath, unsupported) {
  return {path: filePath, locations: [], functions: [], identifiers: [], semantic_hash: null,
    consumer_guards: [], edit_blocks: [], syntax_status: unsupported.some(d => d.startsWith('syntax:')) ? 'failed' : 'unknown',
    unsupported};
}

/** Parse a file and return exact UTF-8 byte spans, not UTF-16 offsets. */
function parseFile(file) {
  if (typeof file.path !== 'string' || typeof file.source !== 'string') throw Error('invalid file');
  const ext = path.extname(file.path).toLowerCase();
  const kind = {'.tsx': ts.ScriptKind.TSX, '.jsx': ts.ScriptKind.JSX,
    '.ts': ts.ScriptKind.TS, '.js': ts.ScriptKind.JS, '.mjs': ts.ScriptKind.JS,
    '.cjs': ts.ScriptKind.JS}[ext];
  if (!kind) return emptyResult(file.path, ['non_js_source']);
  const sf = ts.createSourceFile(file.path, file.source, ts.ScriptTarget.Latest, true, kind);
  const diagnostics = sf.parseDiagnostics || [];
  if (diagnostics.length) {
    const unsupported = diagnostics.slice(0, LIMITS.syntaxDiagnostics).map(d => 'syntax:' + d.code);
    if (diagnostics.length > LIMITS.syntaxDiagnostics) unsupported.push('analysis_syntax_diagnostic_limit');
    const result = emptyResult(file.path, unsupported);
    result.syntax_diagnostics = diagnostics.slice(0, LIMITS.syntaxDiagnostics).map(d => {
      const position = sf.getLineAndCharacterOfPosition(d.start ?? 0);
      return {code: d.code, line: position.line + 1, column: position.character + 1,
        message: ts.flattenDiagnosticMessageText(d.messageText, '\n').slice(0, 2048)};
    });
    return result;
  }

  const locations = [], functions = [], consumerGuards = [], editBlocks = [], identifiers = new Set();
  const blockKeys = new Set();
  const unsupported = new Set();
  const locationsByKey = new Map();
  let metadataBytes = 0;
  let consumerGuardBytes = 0;

  function reserveMetadata(bytes) {
    if (metadataBytes + bytes > LIMITS.metadataBytes) {
      unsupported.add('analysis_metadata_size_limit');
      return false;
    }
    metadataBytes += bytes;
    return true;
  }

  const directives = file.source.match(/\/\/\/[^\r\n]*|\/\/[ \t]*@(?:ts-|jsx)[^\r\n]*|\/\*(?:(?!\*\/)[^])*?@(?:jsx|__PURE__)(?:(?!\*\/)[^])*?\*\//g) || [];
  const semanticFingerprint = semanticHash(sf, directives);

  /** Build a unique exact-content range and retain its syntax category while bounding output. */
  function span(node, nodeKind, symbol = '') {
    const start = node.getStart(sf), end = node.end;
    const key = `${start}:${end}:${nodeKind}`;
    const existing = locationsByKey.get(key);
    if (existing) return existing;
    if (locations.length >= LIMITS.locations) {
      unsupported.add('analysis_location_limit');
      return null;
    }
    const symbolBytes = Buffer.byteLength(symbol, 'utf8');
    if (symbolBytes > LIMITS.symbolBytes || !reserveMetadata(320 + symbolBytes)) {
      unsupported.add('analysis_metadata_size_limit');
      return null;
    }
    const result = {path: file.path, start_line: sf.getLineAndCharacterOfPosition(start).line + 1,
      end_line: sf.getLineAndCharacterOfPosition(Math.max(start, end - 1)).line + 1,
      content_sha256: crypto.createHash('sha256').update(file.source.slice(start, end), 'utf8').digest('hex'),
      start_byte: Buffer.byteLength(file.source.slice(0, start), 'utf8'),
      end_byte: Buffer.byteLength(file.source.slice(0, end), 'utf8'), node_kind: nodeKind, symbol,
      node_count: countNodes(node)};
    locations.push(result);
    locationsByKey.set(key, result);
    return result;
  }

  function addIdentifier(identifier) {
    if (identifiers.has(identifier)) return;
    if (identifiers.size >= LIMITS.identifiers) {
      unsupported.add('analysis_identifier_limit');
      return;
    }
    if (!reserveMetadata(Buffer.byteLength(identifier, 'utf8') + 4)) return;
    identifiers.add(identifier);
  }

  function addFunction(item) {
    if (functions.length >= LIMITS.functions) {
      unsupported.add('analysis_function_limit');
      return;
    }
    const encoded = Buffer.byteLength(JSON.stringify(item), 'utf8');
    if (!reserveMetadata(encoded + 8)) return;
    functions.push(item);
  }

  function addConsumerGuard(site, fallback) {
    if (consumerGuards.length >= LIMITS.consumerGuards) {
      unsupported.add('analysis_consumer_guard_limit');
      return;
    }
    const fallbackBytes = Buffer.byteLength(fallback, 'utf8');
    if (consumerGuardBytes + fallbackBytes > LIMITS.consumerGuardBytes
        || !reserveMetadata(fallbackBytes + 48)) {
      unsupported.add('analysis_consumer_guard_limit');
      return;
    }
    consumerGuardBytes += fallbackBytes;
    consumerGuards.push({start_byte: site.start_byte, end_byte: site.end_byte, fallback});
  }

  /** Retain complete declarations and statements independently of semantic proof support. */
  function addEditBlock(node, owner) {
    const declaration = ts.isMethodDeclaration(node) || ts.isConstructorDeclaration(node)
      || ts.isGetAccessorDeclaration(node) || ts.isSetAccessorDeclaration(node);
    if (!declaration && (!ts.isStatement(node) || ts.isBlock(node) || ts.isEmptyStatement(node))) return;
    let start = node.getStart(sf), end = node.end;
    if (start >= end) return;
    const lineStart = file.source.lastIndexOf('\n', start - 1) + 1;
    if (/^[ \t]*$/.test(file.source.slice(lineStart, start))) start = lineStart;
    const newline = file.source.indexOf('\n', end);
    if (newline >= 0 && /^[ \t\r]*$/.test(file.source.slice(end, newline))) end = newline + 1;
    const key = `${start}:${end}`;
    if (blockKeys.has(key)) return;
    if (editBlocks.length >= LIMITS.editBlocks) {
      unsupported.add('analysis_edit_block_limit');
      return;
    }
    const symbol = node.name?.getText(sf) || owner;
    const bytes = Buffer.byteLength(symbol, 'utf8');
    if (bytes > LIMITS.symbolBytes || !reserveMetadata(256 + bytes)) {
      unsupported.add('analysis_metadata_size_limit');
      return;
    }
    blockKeys.add(key);
    editBlocks.push({start_byte: Buffer.byteLength(file.source.slice(0, start), 'utf8'),
      end_byte: Buffer.byteLength(file.source.slice(0, end), 'utf8'),
      content_sha256: crypto.createHash('sha256').update(file.source.slice(start, end), 'utf8').digest('hex'),
      node_kind: ts.SyntaxKind[node.kind], symbol});
  }

  /** Extract entry models and local edit sites with surrounding lexical ownership. */
  const work = [[sf, '']];
  while (work.length) {
    const [node, inheritedOwner] = work.pop();
    let owner = inheritedOwner;
    addEditBlock(node, owner);
    if (ts.isIdentifier(node)) addIdentifier(node.text);
    if (ts.isFunctionDeclaration(node) || ts.isFunctionExpression(node) || ts.isArrowFunction(node)) {
      const name = node.name?.text || (ts.isVariableDeclaration(node.parent) ? node.parent.name.getText(sf) : '');
      const functionSite = name ? span(node, 'Function', name) : null;
      if (name) owner = name;
      const params = node.parameters.map(p => ts.isIdentifier(p.name) && !p.initializer && !p.dotDotDotToken ? p.name.text : null);
      const asyncNode = node.modifiers?.some(m => m.kind === ts.SyntaxKind.AsyncKeyword) || node.asteriskToken;
      const body = node.body;
      const expression = body && ts.isBlock(body)
        ? (body.statements.length === 1 && ts.isReturnStatement(body.statements[0]) ? body.statements[0].expression : null)
        : body;
      const booleanTypes = node.parameters.every(p => !p.type || p.type.kind === ts.SyntaxKind.BooleanKeyword)
        && (!node.type || node.type.kind === ts.SyntaxKind.BooleanKeyword);
      if (name && expression && params.every(Boolean) && new Set(params).size === params.length
          && params.length <= 8 && !asyncNode && booleanTypes) {
        const term = booleanTerm(expression, params);
        const returnSite = term ? span(expression, 'BooleanReturn', name) : null;
        if (term && functionSite && returnSite) {
          addFunction({name, params, expression: term, site: returnSite,
            premise: 'direct_function_entry_boolean_arguments_identity_continuation'});
        }
      }
    }
    if (ts.isIfStatement(node) && atomic(node.expression)) span(node.expression, 'Condition', owner);
    if (ts.isConditionalExpression(node) && atomic(node.condition)) span(node.condition, 'Condition', owner);
    if (ts.isJsxAttribute(node) && node.initializer && ts.isStringLiteral(node.initializer))
      span(node.initializer, 'AttributeValue', node.name.getText(sf));
    if (ts.isJsxExpression(node) && node.expression && atomic(node.expression)) {
      const site = span(node.expression, 'Consumer', ts.isJsxAttribute(node.parent) ? node.parent.name.getText(sf) : 'children');
      let expression = node.expression;
      while (ts.isParenthesizedExpression(expression)) expression = expression.expression;
      if (site && ts.isConditionalExpression(expression)) {
        const fallback = ts.isParenthesizedExpression(expression.whenFalse)
          ? expression.whenFalse.expression : expression.whenFalse;
        addConsumerGuard(site, fallback.getText(sf));
      }
    }
    if (ts.isRegularExpressionLiteral(node)) span(node, 'RegExp', owner);
    if (ts.isReturnStatement(node) && node.expression && atomic(node.expression))
      span(node.expression, 'ReturnValue', owner);
    if (ts.isVariableDeclaration(node) && node.initializer && atomic(node.initializer))
      span(node.initializer, 'Initializer', node.name.getText(sf));
    if (ts.isCallExpression(node)) {
      for (const argument of node.arguments) if (atomic(argument)) span(argument, 'Argument', node.expression.getText(sf));
    }
    if (ts.isExpressionStatement(node) && atomic(node)) span(node, 'Statement', owner);
    if (ts.isBinaryExpression(node) && node.operatorToken.kind === ts.SyntaxKind.EqualsToken) {
      span(node, 'Assignment', node.left.getText(sf));
      if (atomic(node.right)) span(node.right, 'AssignmentValue', node.left.getText(sf));
    }
    const children = [];
    ts.forEachChild(node, child => { children.push(child); });
    for (let index = children.length - 1; index >= 0; index -= 1) work.push([children[index], owner]);
  }
  return {path: file.path, locations, functions, identifiers: [...identifiers].sort(),
    semantic_hash: semanticFingerprint, consumer_guards: consumerGuards, edit_blocks: editBlocks,
    syntax_status: 'passed', unsupported: [...unsupported].sort()};
}

const output = {parser: 'typescript', version: ts.version, files: input.files.map(parseFile)};
process.stdout.write(JSON.stringify(output));

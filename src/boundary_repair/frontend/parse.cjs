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

  const locations = [], functions = [], consumerGuards = [], editBlocks = [], localModels = [], identifiers = new Set();
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

  /** Extract finite scalar and JSX-construction projections from pure local control flow. */
  function addLocalModel(fn, ownerSite, name) {
    if (!ownerSite || !fn.body || fn.asteriskToken || fn.modifiers?.some(m => m.kind === ts.SyntaxKind.AsyncKeyword)) return;
    if (countNodes(fn.body) > 1024 || localModels.length >= 128) return;
    const roots = new Set(['this']), sorts = new Map(), definitions = new Map();
    let valid = true;
    for (const parameter of fn.parameters) {
      if (parameter.dotDotDotToken) { valid = false; break; }
      const names = ts.isIdentifier(parameter.name) ? [parameter.name] :
        ts.isObjectBindingPattern(parameter.name) ? parameter.name.elements.map(e => e.dotDotDotToken ? null : e.name) : [];
      if (!names.length || names.some(n => !n || !ts.isIdentifier(n))) { valid = false; break; }
      for (const identifier of names) {
        roots.add(identifier.text);
        const type = parameter.type?.kind;
        const sort = type === ts.SyntaxKind.BooleanKeyword ? 'boolean' : type === ts.SyntaxKind.StringKeyword ? 'string' :
          type === ts.SyntaxKind.NumberKeyword ? 'number' : null;
        if (sort && ts.isIdentifier(parameter.name)) sorts.set(identifier.text, sort);
      }
    }
    if (!valid) return;
    const purity = [fn.body];
    while (purity.length) {
      const node = purity.pop();
      if (ts.isFunctionLike(node)) continue;
      const regexCall = ts.isCallExpression(node) && ts.isPropertyAccessExpression(node.expression)
        && node.expression.name.text === 'test' && ts.isRegularExpressionLiteral(node.expression.expression);
      if ((ts.isCallExpression(node) && !regexCall) || ts.isNewExpression(node) || ts.isAwaitExpression(node)
          || ts.isYieldExpression(node) || ts.isPostfixUnaryExpression(node) || ts.isJsxSpreadAttribute(node)
          || ts.isSpreadAssignment(node) || ts.isDeleteExpression(node)
          || (ts.isPrefixUnaryExpression(node) && [ts.SyntaxKind.PlusPlusToken, ts.SyntaxKind.MinusMinusToken].includes(node.operator))
          || (ts.isBinaryExpression(node) && node.operatorToken.kind >= ts.SyntaxKind.FirstAssignment
              && node.operatorToken.kind <= ts.SyntaxKind.LastAssignment)) return;
      ts.forEachChild(node, child => { purity.push(child); });
    }
    const candidates = new Map(), observations = [];
    const lit = value => ({op: 'literal', value});
    const key = node => `${node.getStart(sf)}:${node.end}`;
    function access(node) {
      if (ts.isIdentifier(node)) return roots.has(node.text) ? node.text : null;
      if (node.kind === ts.SyntaxKind.ThisKeyword) return 'this';
      if (ts.isPropertyAccessExpression(node) && !node.questionDotToken) {
        const parent = access(node.expression);
        return parent ? parent + '.' + node.name.text : null;
      }
      return null;
    }
    function infer(node, env, depth = 0) {
      if (!node || depth > 24) return null;
      if (ts.isParenthesizedExpression(node)) return infer(node.expression, env, depth + 1);
      if (node.kind === ts.SyntaxKind.TrueKeyword || node.kind === ts.SyntaxKind.FalseKeyword) return 'boolean';
      if (ts.isStringLiteralLike(node)) return 'string';
      if (ts.isNumericLiteral(node)) return 'number';
      if (node.kind === ts.SyntaxKind.NullKeyword) return 'null';
      if (ts.isIdentifier(node) && env.has(node.text)) {
        const value = env.get(node.text);
        return infer(value.node, value.env, depth + 1);
      }
      const path = access(node);
      if (path) return sorts.get(path) || null;
      if (ts.isPrefixUnaryExpression(node) && node.operator === ts.SyntaxKind.ExclamationToken) return 'boolean';
      if (ts.isBinaryExpression(node)) {
        if ([ts.SyntaxKind.EqualsEqualsEqualsToken, ts.SyntaxKind.ExclamationEqualsEqualsToken,
          ts.SyntaxKind.LessThanToken, ts.SyntaxKind.LessThanEqualsToken, ts.SyntaxKind.GreaterThanToken,
          ts.SyntaxKind.GreaterThanEqualsToken].includes(node.operatorToken.kind)) return 'boolean';
        if ([ts.SyntaxKind.AmpersandAmpersandToken, ts.SyntaxKind.BarBarToken].includes(node.operatorToken.kind))
          return infer(node.left, env, depth + 1) === 'boolean' && infer(node.right, env, depth + 1) === 'boolean' ? 'boolean' : null;
      }
      if (ts.isConditionalExpression(node)) return infer(node.whenTrue, env, depth + 1) || infer(node.whenFalse, env, depth + 1);
      if (ts.isCallExpression(node) && ts.isPropertyAccessExpression(node.expression)
          && node.expression.name.text === 'test' && ts.isRegularExpressionLiteral(node.expression.expression)) return 'boolean';
      return null;
    }
    function remember(node, env, sort, guard = false) {
      if (!sort || !atomic(node) || candidates.size >= 48) return;
      candidates.set(key(node), {node, env, sort, guard});
    }
    function value(node, env, expected = null, replacement = null, depth = 0) {
      if (!node || depth > 24) return null;
      if (replacement && key(node) === replacement) return {op: 'symbol', value: '$edit'};
      if (ts.isParenthesizedExpression(node)) return value(node.expression, env, expected, replacement, depth + 1);
      if (node.kind === ts.SyntaxKind.TrueKeyword || node.kind === ts.SyntaxKind.FalseKeyword) return lit(node.kind === ts.SyntaxKind.TrueKeyword);
      if (ts.isStringLiteralLike(node)) return lit(node.text);
      if (ts.isNumericLiteral(node)) return lit(Number(node.text));
      if (node.kind === ts.SyntaxKind.NullKeyword) return lit(null);
      if (ts.isIdentifier(node) && env.has(node.text)) {
        const definition = env.get(node.text);
        const sort = infer(definition.node, definition.env) || expected;
        if (!replacement) remember(definition.node, definition.env, sort);
        return value(definition.node, definition.env, sort, replacement, depth + 1);
      }
      const path = access(node);
      if (path) {
        const sort = sorts.get(path) || expected;
        if (!sort || path === 'this' || (sorts.has(path) && expected && sorts.get(path) !== expected)) return null;
        if (!replacement) sorts.set(path, sort);
        return {op: 'symbol', value: path};
      }
      if (ts.isPrefixUnaryExpression(node) && node.operator === ts.SyntaxKind.ExclamationToken) {
        const child = value(node.operand, env, 'boolean', replacement, depth + 1);
        return child ? {op: 'not', args: [child]} : null;
      }
      if (ts.isBinaryExpression(node)) {
        const ops = new Map([[ts.SyntaxKind.EqualsEqualsEqualsToken, 'eq'], [ts.SyntaxKind.ExclamationEqualsEqualsToken, 'ne'],
          [ts.SyntaxKind.AmpersandAmpersandToken, 'and'], [ts.SyntaxKind.BarBarToken, 'or'],
          [ts.SyntaxKind.LessThanToken, 'lt'], [ts.SyntaxKind.LessThanEqualsToken, 'le'],
          [ts.SyntaxKind.GreaterThanToken, 'lt'], [ts.SyntaxKind.GreaterThanEqualsToken, 'le']]);
        const op = ops.get(node.operatorToken.kind);
        if (!op) return null;
        const sort = ['and', 'or'].includes(op) ? 'boolean' : ['lt', 'le'].includes(op) ? 'number' :
          infer(node.left, env) || infer(node.right, env);
        if (!sort) return null;
        let args = [value(node.left, env, sort, replacement, depth + 1), value(node.right, env, sort, replacement, depth + 1)];
        if ([ts.SyntaxKind.GreaterThanToken, ts.SyntaxKind.GreaterThanEqualsToken].includes(node.operatorToken.kind)) args.reverse();
        return args.every(Boolean) ? {op, args} : null;
      }
      if (ts.isConditionalExpression(node)) {
        if (!replacement) remember(node.condition, env, 'boolean', true);
        const sort = infer(node.whenTrue, env) || infer(node.whenFalse, env) || expected;
        const args = [value(node.condition, env, 'boolean', replacement, depth + 1),
          value(node.whenTrue, env, sort, replacement, depth + 1), value(node.whenFalse, env, sort, replacement, depth + 1)];
        return args.every(Boolean) ? {op: 'ite', args} : null;
      }
      if (ts.isCallExpression(node) && node.arguments.length === 1 && ts.isPropertyAccessExpression(node.expression)
          && node.expression.name.text === 'test' && ts.isRegularExpressionLiteral(node.expression.expression)) {
        const raw = node.expression.expression.getText(sf), end = raw.lastIndexOf('/');
        const pattern = raw.slice(1, end), flags = raw.slice(end + 1);
        const argument = value(node.arguments[0], env, 'string', replacement, depth + 1);
        return argument && ['', 'i'].includes(flags) ? {op: 'regex_test', args: [lit(pattern), lit(flags), argument]} : null;
      }
      return null;
    }
    function presence(node, env, tag, replacement = null) {
      if (ts.isParenthesizedExpression(node)) return presence(node.expression, env, tag, replacement);
      if (ts.isConditionalExpression(node)) {
        if (!replacement) remember(node.condition, env, 'boolean', true);
        const args = [value(node.condition, env, 'boolean', replacement), presence(node.whenTrue, env, tag, replacement),
          presence(node.whenFalse, env, tag, replacement)];
        return args.every(Boolean) ? {op: 'ite', args} : null;
      }
      if (ts.isJsxElement(node) || ts.isJsxSelfClosingElement(node)) {
        const opening = ts.isJsxElement(node) ? node.openingElement : node;
        if (opening.tagName.getText(sf) === tag) return lit(true);
        if (!ts.isJsxElement(node)) return lit(false);
        const children = node.children.filter(child => !ts.isJsxText(child)).map(child =>
          ts.isJsxExpression(child) ? child.expression ? presence(child.expression, env, tag, replacement) : lit(false) :
            presence(child, env, tag, replacement));
        return children.every(Boolean) ? {op: 'or', args: children} : null;
      }
      if (ts.isStringLiteralLike(node) || ts.isNumericLiteral(node) ||
          [ts.SyntaxKind.NullKeyword, ts.SyntaxKind.TrueKeyword, ts.SyntaxKind.FalseKeyword].includes(node.kind)) return lit(false);
      return null;
    }
    function observe(node, env, guards, property, projection, sort, tag = '') {
      const expression = projection === 'presence' ? presence(node, env, tag) : value(node, env, sort);
      const conditions = guards.map(g => {
        const term = value(g.node, g.env, 'boolean');
        return term && (g.positive ? term : {op: 'not', args: [term]});
      });
      if (!expression || !conditions.every(Boolean)) return;
      if (projection === 'value') remember(node, env, sort);
      const site = span(node, 'Observation', name);
      if (site) observations.push({node, env, guards, property, projection, sort, tag, site,
        expression, conditions});
    }
    function returned(node, env, guards) {
      const stack = [node];
      const scalar = infer(node, env);
      const returnSort = fn.type?.kind === ts.SyntaxKind.BooleanKeyword ? 'boolean' :
        fn.type?.kind === ts.SyntaxKind.StringKeyword ? 'string' : fn.type?.kind === ts.SyntaxKind.NumberKeyword ? 'number' : null;
      if (scalar && returnSort && scalar !== returnSort) { valid = false; return; }
      if (scalar) observe(node, env, guards, 'return', 'value', scalar);
      while (stack.length) {
        const current = stack.pop();
        if (ts.isFunctionLike(current)) continue;
        if (ts.isJsxAttribute(current) && current.initializer) {
          const expr = ts.isJsxExpression(current.initializer) ? current.initializer.expression : current.initializer;
          const attribute = current.name.getText(sf);
          const sort = expr && (infer(expr, env) || (['hidden', 'disabled', 'checked', 'selected'].includes(attribute) ? 'boolean' : 'string'));
          if (expr) observe(expr, env, guards, 'jsx.attribute:' + attribute, 'value', sort);
        }
        if (ts.isJsxExpression(current) && current.expression && !ts.isJsxAttribute(current.parent)) {
          const tags = new Set(), nodes = [current.expression];
          while (nodes.length) {
            const child = nodes.pop();
            if (ts.isJsxElement(child)) tags.add(child.openingElement.tagName.getText(sf));
            if (ts.isJsxSelfClosingElement(child)) tags.add(child.tagName.getText(sf));
            if (!ts.isFunctionLike(child)) ts.forEachChild(child, nested => { nodes.push(nested); });
          }
          if (tags.size) for (const tag of [...tags].sort().slice(0, 8))
            observe(current.expression, env, guards, 'jsx.children.contains:' + tag, 'presence', 'boolean', tag);
          else observe(current.expression, env, guards, 'jsx.children.value', 'value', infer(current.expression, env) || 'string');
        }
        ts.forEachChild(current, child => { stack.push(child); });
      }
    }
    function flow(statements, env, guards, depth = 0) {
      if (depth > 12) { valid = false; return; }
      for (let index = 0; index < statements.length; index++) {
        const statement = statements[index];
        if (ts.isVariableStatement(statement) && (statement.declarationList.flags & ts.NodeFlags.Const)) {
          for (const declaration of statement.declarationList.declarations) {
            if (!ts.isIdentifier(declaration.name) || !declaration.initializer || !atomic(declaration.initializer)) { valid = false; return; }
            env.set(declaration.name.text, {node: declaration.initializer, env: new Map(env)});
          }
        } else if (ts.isReturnStatement(statement) && statement.expression) {
          returned(statement.expression, env, guards);
          return;
        } else if (ts.isIfStatement(statement)) {
          remember(statement.expression, env, 'boolean', true);
          const rest = statements.slice(index + 1);
          const branch = part => !part ? rest : [...(ts.isBlock(part) ? part.statements : [part]), ...rest];
          flow(branch(statement.thenStatement), new Map(env), [...guards, {node: statement.expression, env: new Map(env), positive: true}], depth + 1);
          flow(branch(statement.elseStatement), new Map(env), [...guards, {node: statement.expression, env: new Map(env), positive: false}], depth + 1);
          return;
        } else if (!ts.isEmptyStatement(statement)) { valid = false; return; }
      }
    }
    if (ts.isBlock(fn.body)) flow([...fn.body.statements], definitions, []);
    else returned(fn.body, definitions, []);
    if (!valid || !observations.length || sorts.size > 8 || observations.length > 32) return;
    if ([...sorts.keys()].some(path => [...sorts.keys()].some(other => other.startsWith(path + '.')))) return;
    const grouped = new Map();
    for (const observer of observations) {
      const identity = key(observer.node) + ':' + observer.property;
      if (!grouped.has(identity)) grouped.set(identity, []);
      grouped.get(identity).push(observer);
    }
    const groups = [...grouped.values()];
    function projection(group, replacement = null) {
      const parts = [];
      for (const observer of group) {
        const expression = observer.projection === 'presence' ? presence(observer.node, observer.env, observer.tag, replacement) :
          value(observer.node, observer.env, observer.sort, replacement);
        const conditions = observer.guards.map(g => {
          const term = value(g.node, g.env, 'boolean', replacement);
          return term && (g.positive ? term : {op: 'not', args: [term]});
        });
        if (!expression || !conditions.every(Boolean)) return null;
        parts.push({expression, guard: {op: 'and', args: conditions}});
      }
      if (group[0].projection === 'presence') return {expression: {op: 'or', args: parts.map(p =>
        ({op: 'and', args: [p.guard, p.expression]}))}, conditions: []};
      let expression = parts[parts.length - 1].expression;
      for (let index = parts.length - 2; index >= 0; index--)
        expression = {op: 'ite', args: [parts[index].guard, parts[index].expression, expression]};
      return {expression, conditions: [{op: 'or', args: parts.map(p => p.guard)}]};
    }
    const edits = [];
    for (const [replacement, candidate] of candidates) {
      const continuations = [];
      for (let index = 0; index < groups.length; index++) {
        const summary = projection(groups[index], replacement);
        if (summary) continuations.push({observation: index, ...summary});
      }
      const expression = value(candidate.node, candidate.env, candidate.sort);
      const site = expression ? span(candidate.node, candidate.guard ? 'LocalGuard' : 'LocalValue', name) : null;
      if (site && continuations.length === groups.length) edits.push({site, output_sort: candidate.sort, expression, continuations});
    }
    if (!edits.length || sorts.size > 8) return;
    const model = {owner: ownerSite, name, inputs: [...sorts].sort().map(([name, sort]) => ({name, sort})), edits,
      observations: groups.map(group => ({site: group[0].site, property_name: group[0].property, output_sort: group[0].sort,
        projection: group[0].projection, ...projection(group)})),
      premises: ['pure_declared_entry_model', 'plain_data_without_getters_or_proxies',
        'JSX_construction_projection_not_browser_visibility', 'declared_scalar_sorts_not_all_JavaScript_inputs']};
    if (reserveMetadata(Buffer.byteLength(JSON.stringify(model), 'utf8'))) localModels.push(model);
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
    if (ts.isFunctionDeclaration(node) || ts.isFunctionExpression(node) || ts.isArrowFunction(node) || ts.isMethodDeclaration(node)) {
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
      const legacy = expression && params.every(Boolean) && booleanTypes && booleanTerm(expression, params);
      if (!legacy) addLocalModel(node, functionSite, name);
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
    semantic_hash: semanticFingerprint, consumer_guards: consumerGuards, edit_blocks: editBlocks, local_models: localModels,
    syntax_status: 'passed', unsupported: [...unsupported].sort()};
}

const output = {parser: 'typescript', version: ts.version, files: input.files.map(parseFile)};
process.stdout.write(JSON.stringify(output));

import { readFileSync, existsSync } from 'node:fs';
import { createRequire } from 'node:module';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import ts from 'typescript';

const require = createRequire(import.meta.url);
const sourceRoot = fileURLToPath(new URL('../src/', import.meta.url));
const cache = new Map();

// Compile the actual TS/TSX modules for node:test, sharing the installed React.
export function loadSource(name) {
  const filename = resolve(sourceRoot, name);
  if (cache.has(filename)) return cache.get(filename).exports;
  const module = { exports: {} };
  cache.set(filename, module);
  const compiled = ts.transpileModule(readFileSync(filename, 'utf8'), {
    compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022,
      jsx: ts.JsxEmit.ReactJSX, esModuleInterop: true },
  }).outputText;
  function localRequire(specifier) {
    if (!specifier.startsWith('.')) return require(specifier);
    const target = resolve(dirname(filename), specifier);
    return loadSource(existsSync(target + '.tsx') ? target + '.tsx' : target + '.ts');
  }
  new Function('require', 'module', 'exports', compiled)(localRequire, module, module.exports);
  return module.exports;
}

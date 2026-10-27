import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';
import ts from 'typescript';

const source = readFileSync(new URL('../src/protocol.ts', import.meta.url), 'utf8');
const compiled = ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.ESNext } }).outputText;
const { isProtocolResult } = await import(`data:text/javascript;base64,${Buffer.from(compiled).toString('base64')}`);
const valid = { title: 'Встреча', summary: 'Текст', date_hint: null, participants: ['Аля'], key_points: [], decisions: [], action_items: [], mom: { agenda: [], next_meeting: null } };

test('valid protocol remains renderable', () => assert.equal(isProtocolResult(valid), true));
test('legacy malformed responses cannot reach rendering or export controls', () => {
  for (const value of [null, {}, { ...valid, summary: {} }, { ...valid, participants: null },
    { ...valid, action_items: [{ task: 'Test', assignee: {}, due: null, priority: 'high' }] },
    { ...valid, mom: { agenda: [null], next_meeting: null } }]) {
    assert.equal(isProtocolResult(value), false);
  }
});

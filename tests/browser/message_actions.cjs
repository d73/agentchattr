const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const src = fs.readFileSync('static/chat.js', 'utf8');
const section = src.slice(src.indexOf('const MESSAGE_ACTION_ICONS'), src.indexOf('function todoCycle(msgId)'));
const update = src.slice(src.indexOf('function updateTodoState(msgId, status)'), src.indexOf('// --- Delete mode ---'));

test('pin states update the icon, hover tooltip and accessible label', () => {
  const attrs = {};
  const button = {title: '', innerHTML: '', setAttribute: (name, value) => { attrs[name] = value; }};
  const message = {classList: {add() {}, remove() {}}, querySelector: () => button};
  const document = {querySelector: () => message, getElementById: () => null};
  const ctx = {document};
  vm.runInNewContext(section + update, ctx);
  for (const [status, label] of [[null, 'Pin message'], ['todo', 'Mark pinned message done'], ['done', 'Unpin message']]) {
    ctx.updateTodoState(1, status);
    assert.equal(button.title, label);
    assert.equal(attrs['aria-label'], label);
    assert.match(button.innerHTML, /<svg/);
    assert.doesNotMatch(button.innerHTML, />\s*(pin|done|unpin)\s*</i);
  }
});

test('chat bubble toolbar uses labeled icons in the approved order', () => {
  const toolbar = src.match(/class="msg-actions bubble-actions"[^>]*>([\s\S]*?)<\/div>/);
  assert.ok(toolbar);
  const actions = [...toolbar[1].matchAll(/title="([^"]+)" aria-label="([^"]+)">\$\{messageActionIcon\(([^)]+)\)\}/g)];
  assert.deepEqual(actions.map(([, title, label, icon]) => {
    assert.equal(title, label);
    return [label, icon];
  }), [
    ['Copy message', "'copy'"],
    ['${pinAction.label}', 'pinAction.icon'],
    ['Reply', "'reply'"],
    ['Convert to Job', "'job'"],
    ['Delete message', "'delete'"],
  ]);
});

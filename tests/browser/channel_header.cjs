const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const root = path.join(__dirname, '../..');
const source = fs.readFileSync(path.join(root, 'static/chat.js'), 'utf8');

test('header tracks server label and selected channel as plain text', () => {
    const code = source.match(/let roomSubtitle = '';[\s\S]*?\nupdateRoomSubtitle\(\);/)[0];
    const label = { textContent: '' };
    const context = vm.createContext({ window: {}, document: { getElementById: () => label } });
    vm.runInContext("let activeChannel = 'general'; let username = 'user';\n" + code, context);
    assert.equal(label.textContent, 'user - # general');
    vm.runInContext("roomSubtitle = 'host'; updateRoomSubtitle()", context);
    assert.equal(label.textContent, 'user @ host - # general');
    vm.runInContext("username = 'Derek'; updateRoomSubtitle()", context);
    assert.equal(label.textContent, 'Derek @ host - # general');
    context.window._setActiveChannel('<work>');
    assert.equal(label.textContent, 'Derek @ host - # <work>');
});

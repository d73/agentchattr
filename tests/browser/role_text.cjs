const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { test } = require('node:test');
const vm = require('node:vm');

test('a stored role is escaped in both the pill label and tooltip', () => {
    const source = fs.readFileSync(path.join(__dirname, '../../static/chat.js'), 'utf8');
    const markup = source.match(/const senderRole = [\s\S]*?const rolePillHtml = [^\r\n]+/);
    assert.ok(markup, 'role pill markup is present');
    const role = 'Research "A" < B & C';
    const pill = vm.runInNewContext(markup[0] + '\nrolePillHtml;', {
        _agentRoles: { fixture: role },
        appearance: { name: 'fixture' },
        isSelf: false,
        escapeHtml: (text) => text.replaceAll('&', '&amp;').replaceAll('<', '&lt;').replaceAll('>', '&gt;'),
    });
    assert.match(pill, /title="Research &quot;A&quot; &lt; B &amp; C"/);
    assert.match(pill, />Research "A" &lt; B &amp; C<\/button>/);
});

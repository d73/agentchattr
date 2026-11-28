const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { test } = require('node:test');
const vm = require('node:vm');

const source = fs.readFileSync(path.join(__dirname, '../../static/chat.js'), 'utf8').replace(/\r\n/g, '\n');

test('the timeline treats a legacy message clock as text', () => {
    let bubble;
    const context = vm.createContext({
        window: {},
        document: {
            getElementById: () => null,
            createElement: () => {
                let value = '';
                return {
                    dataset: {},
                    classList: { add() {} },
                    set textContent(text) { value = String(text).replaceAll('&', '&amp;').replaceAll('<', '&lt;').replaceAll('>', '&gt;'); },
                    get innerHTML() { return value; },
                    set innerHTML(html) { value = html; },
                };
            },
        },
        maybeInsertDateDivider() {},
        username: 'fixture', agentConfig: {}, todos: {}, agentHats: {}, _agentRoles: {},
        senderAppearance: (message) => ({ name: message.sender, color: '#555', avatar: '' }),
        renderMarkdown: (text) => text, styleHashtags: (text) => text,
        todoAction: () => ({}), messageActionIcon: () => '',
        addCodeCopyButtons() {}, activeChannel: 'general',
    });
    for (const name of ['escapeHtml', 'appendMessage']) {
        const fn = source.match(new RegExp(`^function ${name}\\([\\s\\S]*?\\n}\\n`, 'm'));
        assert.ok(fn, `${name} is present`);
        vm.runInContext(fn[0], context);
    }
    context.appendMessage({ id: 1, sender: 'fixture', type: 'chat', text: 'Fixture', time: 'Before < noon & after' }, {
        history: true,
        container: { appendChild(element) { bubble = element; } },
    });
    assert.match(bubble.innerHTML, /class="msg-time">Before &lt; noon &amp; after<\/span>/);
});

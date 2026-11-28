const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const source = fs.readFileSync(path.join(__dirname, '../../static/chat.js'), 'utf8');
const sectionStart = source.indexOf('// --- Reply ---');
const sectionEnd = source.indexOf('function startReply(', sectionStart);
const code = source.slice(sectionStart, sectionEnd);

function fixture({ clipboard, legacy = true } = {}) {
    const feedback = [];
    const commands = [];
    const classes = new Set();
    const button = { classList: classes };
    const message = {
        dataset: { rawText: '**hello**' },
        querySelector: selector => selector === '.bubble-copy' ? button : { innerHTML: '<b>hello</b>' },
    };
    let refocus = 0;
    let removed = 0;
    const document = {
        activeElement: { focus() { refocus++; } },
        querySelector: () => message,
        getSelection: () => null,
        body: { appendChild() {} },
        createElement() {
            return { style: {}, select() {}, remove() { removed++; } };
        },
        execCommand(command) { commands.push(command); return legacy; },
    };
    const context = vm.createContext({
        document, navigator: { clipboard }, Blob,
        ClipboardItem: class { constructor(types) { this.types = types; } },
        showToast: (...args) => feedback.push(args), setTimeout() {},
    });
    vm.runInContext(code, context);
    return { context, classes, commands, feedback, cleanup: () => [refocus, removed] };
}

test('copies a message when the browser does not expose the Clipboard API', async () => {
    const f = fixture();
    await f.context.copyMessage(1);
    assert.deepEqual(f.commands, ['copy']);
    assert.deepEqual(f.cleanup(), [1, 1]);
    assert.ok(f.classes.has('copied'));
});

test('rich clipboard rejection falls back to raw Markdown', async () => {
    const clipboard = {
        async write() { throw new Error('HTML denied'); },
        async writeText(value) { assert.equal(value, '**hello**'); },
    };
    const f = fixture({ clipboard, legacy: false });
    await f.context.copyMessage(1);
    assert.deepEqual(f.commands, []);
    assert.ok(f.classes.has('copied'));
});

test('keeps both formats on the normal rich-copy path', async () => {
    const f = fixture({ clipboard: {
        async write([item]) {
            assert.equal(await item.types['text/html'].text(), '<b>hello</b>');
            assert.equal(await item.types['text/plain'].text(), '**hello**');
        },
    } });
    await f.context.copyMessage(1);
    assert.ok(f.classes.has('copied'));
    assert.deepEqual(f.commands, []);
});

test('reports a blocked clipboard without showing false success', async () => {
    const f = fixture({ clipboard: { async writeText() { throw new Error('Blocked'); } }, legacy: false });
    await f.context.copyMessage(1);
    assert.equal(f.classes.has('copied'), false);
    assert.match(f.feedback[0][0], /could not copy/i);
});

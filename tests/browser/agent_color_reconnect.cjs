const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const source = fs.readFileSync(path.join(__dirname, '../../static/chat.js'), 'utf8');
const start = source.indexOf('const COLOR_CACHE_KEY');
const code = source.slice(start, source.indexOf('let schedulesList', start));
const makeStorage = (entries = []) => {
    const data = new Map(entries);
    return { getItem: key => data.get(key) || null, setItem: (key, value) => data.set(key, value) };
};

function startClient(localStorage, sessionStorage = makeStorage()) {
    const sent = [];
    const ws = { readyState: 3, send: raw => sent.push(JSON.parse(raw)) };
    const ctx = vm.createContext({
        localStorage, sessionStorage, ws, WebSocket: { OPEN: 1 },
        refreshAgentColors() {}, showToast() {},
    });
    vm.runInContext(code, ctx);
    return { ctx, ws, sent, colors: () => ({ ...vm.runInContext('colorOverrides', ctx) }) };
}

test('reconnecting sends only the edited agent and accepts other agents’ newer colors', () => {
    const localStorage = makeStorage([['agentchattr-color-overrides', '{"claude":"#111111","codex":"#222222"}']]);
    const { ctx, ws, sent, colors } = startClient(localStorage);
    ctx.setAgentColorOverride('codex', '#444444');
    assert.equal(sent.length, 0);
    ws.readyState = 1;
    const remote = { claude: '#333333', codex: '#222222' };
    ctx.applyAgentColorSettings(remote);
    assert.deepEqual(colors(), { claude: '#333333', codex: '#444444' });
    assert.deepEqual(sent[0], { type: 'agent_color_changes', request_id: 1, changes: { codex: '#444444' } });
    ctx.handleAgentColorsSaved({ request_id: 1, data: colors() });
    assert.equal(sent.length, 1);
});

test('reset survives a reload before acknowledgement and preserves other agents', () => {
    const localStorage = makeStorage([['agentchattr-color-overrides', '{"claude":"#111111","codex":"#222222"}']]);
    const sessionStorage = makeStorage();
    const first = startClient(localStorage, sessionStorage);
    first.ws.readyState = 1;
    first.ctx.applyAgentColorSettings({ claude: '#111111', codex: '#222222' });
    first.ctx.setAgentColorOverride('codex', null);
    assert.deepEqual(first.sent[0].changes, { codex: null });

    const reloaded = startClient(localStorage, sessionStorage);
    reloaded.ws.readyState = 1;
    reloaded.ctx.applyAgentColorSettings({ claude: '#333333', codex: '#222222' });
    assert.deepEqual(reloaded.colors(), { claude: '#333333' });
    assert.deepEqual(reloaded.sent[0].changes, { codex: null });
    reloaded.ctx.handleAgentColorsSaved({ request_id: 1, data: { claude: '#333333' } });
    assert.equal(sessionStorage.getItem('agentchattr-agent-color-changes'), '{}');
});

test('acknowledging one edit keeps a newer queued edit for the same agent', () => {
    const { ctx, ws, sent, colors } = startClient(makeStorage());
    ws.readyState = 1;
    ctx.applyAgentColorSettings({ codex: '#222222' });
    ctx.setAgentColorOverride('codex', '#333333');
    ctx.setAgentColorOverride('codex', '#444444');
    ctx.handleAgentColorsSaved({ request_id: 1, data: { codex: '#333333' } });
    assert.deepEqual(colors(), { codex: '#444444' });
    assert.deepEqual(sent[1], { type: 'agent_color_changes', request_id: 2, changes: { codex: '#444444' } });
});

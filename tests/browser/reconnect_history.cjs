const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { test } = require('node:test');
const vm = require('node:vm');

const source = fs.readFileSync(path.join(__dirname, '../../static/chat.js'), 'utf8').replace(/\r\n/g, '\n');

function makeTimeline() {
    const shown = new Map();
    const sockets = [];
    const tasks = [];
    const retry = [];
    const container = {
        replaceChildren() { shown.clear(); },
        appendChild(fragment) {
            for (const message of fragment.messages) shown.set(message.id, message.text);
        },
    };
    const context = vm.createContext({
        console: { log() {} },
        location: { protocol: 'http:', host: 'chat.test' },
        SESSION_TOKEN: 'fixture',
        document: {
            createDocumentFragment: () => ({ messages: [], querySelectorAll: () => [] }),
            getElementById: (id) => id === 'messages' ? container : null,
        },
        appendMessage(message, { container: fragment }) {
            if (!shown.has(message.id)) fragment.messages.push(message);
        },
        resetEarlierHistory() {}, setHistoryIndicator() {}, updateEarlierHistoryControl() {},
        dayFloatRefresh() {}, scrollToBottom() {}, clearTimeout() {},
        WebSocket: class { constructor() { sockets.push(this); } },
        historyTasks: { port2: { postMessage: (generation) => tasks.push(generation) } },
        setTimeout: (callback) => { retry.push(callback); },
        window: {},
        autoScroll: false,
        historyGeneration: 0, historyNeedsReset: false,
        historyLoading: false, historyScheduled: false, historyReceived: false,
        pendingHistory: [], pendingLiveEvents: [], earlierHistory: {},
        lastMessageDate: null, lastMessageDates: {},
        ws: null, reconnectTimer: null, soundEnabled: false,
    });
    for (const name of ['scheduleHistoryRender', 'renderHistoryBatch', 'handleServerEvent', 'connectWebSocket']) {
        const fn = source.match(new RegExp(`^function ${name}\\([\\s\\S]*?\\n}\\n`, 'm'));
        assert.ok(fn, `${name} is present`);
        vm.runInContext(fn[0], context);
    }
    context.connectWebSocket();
    return {
        shown, sockets,
        reconnect() {
            sockets.at(-1).onclose({ code: 1006 });
            retry.shift()();
        },
        send(event, socket = sockets.at(-1)) {
            socket.onmessage({ data: JSON.stringify(event) });
        },
        render() {
            while (tasks.length) {
                if (tasks.shift() === context.historyGeneration) context.renderHistoryBatch();
            }
        },
    };
}

test('reconnect replaces stale messages with the server snapshot', () => {
    const timeline = makeTimeline();
    timeline.send({ type: 'history', messages: [{ id: 1, text: 'old edit' }, { id: 2, text: 'deleted while offline' }] });
    timeline.send({ type: 'history_complete', has_more: {} });
    timeline.render();

    timeline.reconnect();
    assert.deepEqual([...timeline.shown], [[1, 'old edit'], [2, 'deleted while offline']]);
    timeline.send({ type: 'history', messages: [{ id: 5, text: 'abandoned socket' }] }, timeline.sockets[0]);
    timeline.send({ type: 'history', messages: [{ id: 1, text: 'edited' }] });
    timeline.render();
    timeline.send({ type: 'history', messages: [{ id: 3, text: 'new' }] });
    timeline.send({ type: 'history_complete', has_more: {} });
    timeline.render();

    assert.deepEqual([...timeline.shown], [[1, 'edited'], [3, 'new']]);

    timeline.reconnect();
    timeline.send({ type: 'history_complete', has_more: {} });
    timeline.render();
    assert.equal(timeline.shown.size, 0);
});

const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const source = fs.readFileSync(path.join(__dirname, '../../static/chat.js'), 'utf8');
function between(from, to) {
    return source.slice(source.indexOf(from), source.indexOf(to, source.indexOf(from)));
}

const rename = {
    type: 'channel_renamed', old_name: 'work', new_name: 'renamed',
    data: { channels: ['general', 'renamed'], archived_channels: ['renamed'] },
};

function passiveTab(historyLoading) {
    const activity = [];
    const message = { dataset: { channel: 'work' } };
    const tab = vm.createContext({
        historyLoading, pendingLiveEvents: [], earlierHistory: { work: true },
        activeChannel: 'work', channelList: ['general', 'work'], channelUnread: { work: 2 },
        _channelMentions: {}, lastMessageDates: {}, pendingChannelSwitch: null,
        document: { getElementById: () => ({ children: [message] }) },
        window: {
            _setActiveChannel(name) { tab.activeChannel = name; activity.push(name); },
            setChannelPrefs(prefs) { tab.channelPrefs = prefs; },
        },
        Store: { set() {} }, Hub: { emit() {} }, localStorage: { setItem() {} },
        _seededUiPrefs: true, _seededColors: true,
        applyAgentColorSettings() {},
        resetEarlierHistory() {}, filterMessagesByChannel() {}, renderChannelTabs() {},
        updateEarlierHistoryControl() {},
    });
    vm.runInContext([
        between('function handleServerEvent(', 'function connectWebSocket('),
        between('function applySettings(', '// All right-hand slide-out panels'),
    ].join('\n'), tab);
    return { tab, activity, message };
}

test('passive tab follows the rename without briefly visiting General', () => {
    const { tab, activity, message } = passiveTab(false);
    tab.handleServerEvent(rename);
    assert.deepEqual(activity, ['renamed']);
    assert.deepEqual(tab.channelList, rename.data.channels);
    assert.deepEqual(tab.channelPrefs.archived, ['renamed']);
    assert.equal(message.dataset.channel, 'renamed');
    assert.equal(tab.earlierHistory.renamed, true);
});

test('later settings cannot overtake a rename buffered during history replay', () => {
    const { tab, activity, message } = passiveTab(true);
    tab.handleServerEvent(rename);
    tab.handleServerEvent({ type: 'settings', data: { channels: ['general', 'renamed', 'other'] } });
    assert.equal(tab.activeChannel, 'work');
    assert.equal(tab.pendingLiveEvents.length, 2);

    tab.historyLoading = false;
    for (const frame of tab.pendingLiveEvents) tab.handleServerEvent(frame);
    assert.deepEqual(activity, ['renamed']);
    assert.equal(message.dataset.channel, 'renamed');
    assert.deepEqual(tab.channelList, ['general', 'renamed', 'other']);

    tab.handleServerEvent({ type: 'settings', data: { channels: ['general'] } });
    assert.deepEqual(activity, ['renamed', 'general']);
});

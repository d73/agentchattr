const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const chat = fs.readFileSync(path.join(__dirname, '..', 'static', 'chat.js'), 'utf8');
function section(start, end) {
    return chat.slice(chat.indexOf(start), chat.indexOf(end, chat.indexOf(start)));
}

const message = { dataset: { sender: 'c7', rawText: 'Earlier message' } };
const preview = {};
const chips = ['build-agent', 'c7'].map(name => ({
    dataset: { agent: name },
    classList: new Set(),
}));
for (const chip of chips) chip.classList.remove = chip.classList.delete;
const document = {
    querySelector: () => message,
    querySelectorAll: () => chips,
    getElementById: id => id === 'reply-preview' ? preview : { focus() {} },
};
const context = vm.createContext({
    document, activeMentions: new Set(), activeChannel: 'general', replyingTo: null,
    _channelMentions: { work: ['build-agent'] },
    agentConfig: {}, agentNamesByIdentity: new Map(), colorOverrides: {},
    baseColors: { codex: { color: 'offline-codex' } },
    BRAND_AVATARS: { codex: 'codex-avatar', claude: 'claude-avatar' },
    USER_AVATAR: 'user-avatar',
    buildStatusPills() {}, buildMentionToggles() {}, buildSoundSettings() {},
    recolorMessages() { if (context.replyingTo) context.renderReplyPreview(); }, updateJobReplyTargetUI() {},
    persistMentions() {}, syncMentionAllToggle() {}, escapeHtml: String,
});
vm.runInContext([
    section('function resolveAgent(', 'function colorMentions('),
    section('function applyAgentConfig(', '// Re-apply overrides'),
    section('function startReply(', 'function cancelReply('),
].join('\n'), context);

const roster = {
    'build-agent': { identity_id: 'original-agent', base: 'codex', color: 'codex-color' },
    c7: { identity_id: 'new-agent', base: 'claude', color: 'claude-color' },
};
context.applyAgentConfig(roster);
Object.assign(message.dataset, { senderKind: 'agent', senderIdentity: 'original-agent', senderBase: 'codex' });
context.startReply(1);
assert.deepEqual([...context.activeMentions], ['build-agent']);
assert.match(preview.innerHTML, /build-agent/);
assert.equal(context.senderAppearance(context.renderedSender(message)).avatar, 'codex-avatar');

// The new roster can arrive before the rename event. The open reply and sticky
// selections should already follow the verified identity, even when its old ID is reused.
context.applyAgentConfig({
    ...roster,
    'build-agent': { ...roster.c7, identity_id: 'replacement-agent' },
    'new-builder': roster['build-agent'],
});
assert.deepEqual([...context.activeMentions], ['new-builder']);
assert.deepEqual(Array.from(context._channelMentions.work), ['new-builder']);
assert.match(preview.innerHTML, /new-builder/);

context.applyAgentConfig({ c7: roster.c7 });
context.startReply(1);
assert.deepEqual([...context.activeMentions], []);
const offline = context.senderAppearance(context.renderedSender(message));
assert.equal(offline.agent, null);
assert.equal(offline.color, 'offline-codex');
assert.equal(offline.avatar, 'codex-avatar');

const prefixRoster = { c7: roster.c7, 'c7-build': roster['build-agent'] };
for (const entries of [Object.entries(prefixRoster), Object.entries(prefixRoster).reverse()]) {
    context.applyAgentConfig(Object.fromEntries(entries));
    context.activeMentions.add('c7');
    Object.assign(message.dataset, { sender: 'c7-build-notes', senderKind: '', senderIdentity: '', senderBase: '' });
    context.startReply(1);
    assert.deepEqual([...context.activeMentions], ['c7']);
    assert.equal(context.senderAppearance(context.renderedSender(message)).avatar, 'user-avatar');
}

context.applyAgentConfig(roster);
context.activeMentions = new Set(['build-agent']);
Object.assign(message.dataset, { sender: 'c7', senderKind: 'human' });
context.startReply(1);
assert.deepEqual([...context.activeMentions], ['build-agent']);
assert.equal(context.senderAppearance(context.renderedSender(message)).avatar, 'user-avatar');

message.dataset.senderKind = '';
context.startReply(1);
assert.deepEqual([...context.activeMentions], ['c7']);
assert.equal(context.senderAppearance(context.renderedSender(message)).avatar, 'claude-avatar');
assert.equal(context.resolveAgent('constructor'), null);
console.log('Reply identity checks passed');

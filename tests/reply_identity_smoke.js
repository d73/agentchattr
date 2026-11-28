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
const activeMentions = new Set(['c7']);
const context = vm.createContext({
    document, activeMentions, activeChannel: 'general', replyingTo: null,
    agentConfig: {}, agentNamesByIdentity: new Map(), colorOverrides: {},
    baseColors: { codex: { color: 'offline-codex' } },
    BRAND_AVATARS: { codex: 'codex-avatar', claude: 'claude-avatar' },
    USER_AVATAR: 'user-avatar',
    buildStatusPills() {}, buildMentionToggles() {}, buildSoundSettings() {},
    recolorMessages() {}, updateJobReplyTargetUI() {},
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
assert.deepEqual([...activeMentions], ['build-agent']);
assert.match(preview.innerHTML, /build-agent/);
assert.equal(context.senderAppearance(context.renderedSender(message)).avatar, 'codex-avatar');

context.applyAgentConfig({ c7: roster.c7 });
context.startReply(1);
assert.deepEqual([...activeMentions], ['build-agent']);
const offline = context.senderAppearance(context.renderedSender(message));
assert.equal(offline.agent, null);
assert.equal(offline.color, 'offline-codex');
assert.equal(offline.avatar, 'codex-avatar');

const prefixRoster = { c7: roster.c7, 'c7-build': roster['build-agent'] };
for (const entries of [Object.entries(prefixRoster), Object.entries(prefixRoster).reverse()]) {
    context.applyAgentConfig(Object.fromEntries(entries));
    Object.assign(message.dataset, { sender: 'c7-build-notes', senderKind: '', senderIdentity: '', senderBase: '' });
    context.startReply(1);
    assert.deepEqual([...activeMentions], ['build-agent']);
    assert.equal(context.senderAppearance(context.renderedSender(message)).avatar, 'user-avatar');
}

context.applyAgentConfig(roster);
Object.assign(message.dataset, { sender: 'c7', senderKind: 'human' });
context.startReply(1);
assert.deepEqual([...activeMentions], ['build-agent']);
assert.equal(context.senderAppearance(context.renderedSender(message)).avatar, 'user-avatar');

message.dataset.senderKind = '';
context.startReply(1);
assert.deepEqual([...activeMentions], ['c7']);
assert.equal(context.senderAppearance(context.renderedSender(message)).avatar, 'claude-avatar');
assert.equal(context.resolveAgent('constructor'), null);
console.log('Reply identity checks passed');

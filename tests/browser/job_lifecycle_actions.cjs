const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const root = path.join(__dirname, '../../static');
const jobs = fs.readFileSync(path.join(root, 'jobs.js'), 'utf8');
const chat = fs.readFileSync(path.join(root, 'chat.js'), 'utf8');
const between = (source, start, end) => source.slice(source.indexOf(start), source.indexOf(end, source.indexOf(start)));

test('the picker contains only the four approved types and uses Orch presence', () => {
    const jobPresentations = new Map();
    const picker = { options: [{}], value: 'orch' };
    const ctx = vm.createContext({
        jobPresentations, renderJobsList() {},
        document: { getElementById: () => null, querySelector: () => picker },
    });
    vm.runInContext(
        between(jobs, 'const proposalWriterOptions', 'function jobPresentation(job)')
        + between(jobs, 'function updateOrchWriterAvailability', 'function showConvertToJobModal'), ctx,
    );
    assert.deepEqual(Array.from(vm.runInContext('proposalWriterOptions.map(([key]) => key)', ctx)),
        ['orch', 'antigravity', 'claude', 'codex']);
    assert.equal(ctx.updateOrchWriterAvailability(picker), false);
    assert.equal(picker.options[0].disabled, true);
    assert.equal(picker.value, 'antigravity');
    const update = ctx.registerJobPresentation('custom-orch-id', {});
    update([], { kind: 'workflow-coordinator', pluginId: 'different-id', online: true });
    assert.equal(picker.options[0].disabled, true);
    update([], { kind: 'workflow-coordinator', pluginId: 'custom-orch-id', online: true });
    assert.equal(ctx.updateOrchWriterAvailability(picker), true);
    assert.equal(picker.options[0].disabled, false);
});

test('the Job list uses canonical lifecycle stages when the coordinator is offline', () => {
    const ctx = vm.createContext({
        jobPresentations: new Map(), renderJobsList() {},
    });
    vm.runInContext(
        between(jobs, 'const jobPipelineLabels', '// Message Renderers'), ctx,
    );
    const update = ctx.registerJobPresentation('orch', {
        awaiting_approval: 'Awaiting approval', 'orch.coding': 'Coding',
    });
    const job = {
        id: 7, uid: 'job-abc', status: 'done',
        lifecycle: { stage_id: 'orch.coding', flags: [] },
    };
    update([{ id: 7, uid: 'different-job', stage: 'awaiting_approval', attention: true }]);
    assert.deepEqual({ ...ctx.jobPresentation(job) }, { key: 'pipeline-active_coding', attention: false });
    update([{ id: 7, uid: 'job-abc', stage: 'awaiting_approval', attention: true }]);
    assert.deepEqual({ ...ctx.jobPresentation(job) }, { key: 'pipeline-active_coding', attention: true });
    update([]);
    assert.deepEqual({ ...ctx.jobPresentation(job) }, { key: 'pipeline-active_coding', attention: false });
    assert.deepEqual({ ...ctx.jobPresentation({ status: 'open' }) }, { key: 'open', attention: false });
});

test('Orch brief messages expose readable fields and keep escaped JSON in a closed disclosure', () => {
    const ctx = vm.createContext({ window: {
        escapeHtml: text => text.replaceAll('&', '&amp;').replaceAll('<', '&lt;').replaceAll('>', '&gt;'),
    } });
    vm.runInContext(between(jobs, 'function renderOrchEvent', '// Job attachments'), ctx);
    const raw = 'orch-event ' + JSON.stringify({ v: 1, kind: 'brief', revision: 2, brief: {
        repo: 'team/repo', outcome: 'Show <img> safely', checks: ['Check results'],
        constraints: ['Use existing hooks'], gates: ['spec'],
    } });
    const html = ctx.renderOrchEvent(raw);
    assert.match(html, /<details class="job-event">/);
    assert.match(html, /<h3>Brief · Rev 2<\/h3>/);
    assert.match(html, /<h4>What<\/h4><p>Show &lt;img&gt; safely<\/p>/);
    assert.match(html, /<h4>Done when<\/h4><ul><li>Check results<\/li>/);
    assert.match(html, /<h4>Limits<\/h4><ul><li>Use existing hooks<\/li>/);
    assert.match(html, /<h4>Gates<\/h4>.*>Spec<\/span>/);
    assert.doesNotMatch(html, /<img>/);
    assert.doesNotMatch(ctx.renderOrchEvent('orch-event {"v":1,"kind":"ack"}'), /<article/);
    assert.equal(ctx.renderOrchEvent('orch-event {invalid'), null);
    assert.equal(ctx.renderOrchEvent('See the orch-event payload'), null);
});

test('Auto and the mention card use the same single POST and open existing Jobs', async () => {
    const sent = [];
    const opened = [];
    const jobsData = [];
    const ctx = vm.createContext({
        window: { SESSION_TOKEN: 'session', async requireOk() {} }, jobsData,
        async openJobFromBreadcrumb(id) { opened.push(id); },
        async fetch(path, options) {
            sent.push([path, JSON.parse(options.body)]);
            return { async json() { return { job: { id: 24 }, created: false }; } };
        },
    });
    vm.runInContext(between(jobs, 'async function postJobAction', 'function closeConvertJobModal'), ctx);
    await ctx.createOrchJob(42);
    await ctx.window.createOrchJobFromCard({ disabled: false }, 42, 90);
    assert.deepEqual(sent, [
        ['/api/jobs/orch-intake', { source_msg_id: 42, proposal_writer: 'orch' }],
        ['/api/jobs/orch-intake', { source_msg_id: 42, proposal_writer: 'orch', card_msg_id: 90 }],
    ]);
    assert.deepEqual(opened, [24, 24]);
    assert.equal(jobsData.length, 1);
});

function decisionCard(status, result) {
    const requested = [];
    const makeNode = () => ({ classList: { add() {} }, setAttribute() {} });
    const card = {
        children: [],
        querySelector(selector) { return this.children.find(node => node.className === selector.slice(1)); },
        querySelectorAll() { return [button]; },
        appendChild(node) { this.children.push(node); },
        replaceChildren(node) { this.children = [node]; },
    };
    const button = { disabled: false, closest: () => card };
    const ctx = vm.createContext({
        SESSION_TOKEN: 'session', document: { createElement: makeNode },
        window: { async requireOk(response) { if (response.status !== 200) throw new Error('server reply'); } },
        async fetch(path, options) {
            requested.push([path, options.headers, JSON.parse(options.body)]);
            return { status, async json() { return result; } };
        },
    });
    vm.runInContext(between(chat, 'function boundJobDecisionRequest', 'async function resolveDecision'), ctx);
    return { ctx, button, card, requested };
}

test('the first gate click sends only the session token and records the returned choice', async () => {
    const { ctx, button, card, requested } = decisionCard(200, { choice: 'Start' });
    await ctx.resolveBoundJobDecision({ id: 7, metadata: {
        orch_gate: { job_uid: 'job-abc', gate: 'staffing', revision: 2 },
    } }, 'Start', button);
    const [[path, headers, body]] = requested;
    assert.equal(path, '/api/jobs/job-abc/orch-gates/7/resolve');
    assert.deepEqual({ ...headers }, { 'Content-Type': 'application/json', 'X-Session-Token': 'session' });
    assert.deepEqual(body, { gate: 'staffing', revision: 2, choice: 'Start' });
    assert.equal(card.children[0].textContent, 'You chose: Start');
});

test('a stale Reopen click shows feedback inline and does not retry', async () => {
    const { ctx, button, card, requested } = decisionCard(409, undefined);
    const msg = { id: 7, metadata: {
        job_reopen: { job_id: 24, job_uid: 'job-abc', expected_version: 8, request_id: 'request-a' },
    } };
    await ctx.resolveBoundJobDecision(msg, 'Needs clarification', button);
    await ctx.resolveBoundJobDecision(msg, 'Needs clarification', button);
    const [[path, , body]] = requested;
    assert.equal(path, '/api/jobs/24/reopen-requests/request-a/decision');
    assert.deepEqual(body, { job_uid: 'job-abc', expected_version: 8, decision: 'clarify' });
    assert.equal(requested.length, 1);
    assert.match(card.children[0].textContent, /stale/);
});

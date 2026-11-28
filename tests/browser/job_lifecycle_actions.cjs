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
    assert.deepEqual({ ...ctx.jobPresentation(job) }, { filter: 'active_coding', workflow: true, attention: false });
    update([{ id: 7, uid: 'job-abc', stage: 'awaiting_approval', attention: true }]);
    assert.deepEqual({ ...ctx.jobPresentation(job) }, { filter: 'active_coding', workflow: true, attention: true });
    update([]);
    assert.deepEqual({ ...ctx.jobPresentation(job) }, { filter: 'active_coding', workflow: true, attention: false });
    assert.deepEqual({ ...ctx.jobPresentation({ status: 'open' }) }, { filter: 'scoping', workflow: false, attention: false });
});

test('status pills select across manual and Orch Jobs while preserving reorder restrictions', () => {
    const document = { getElementById: () => list, createElement: node };
    function node() {
        const children = [];
        const attributes = new Map();
        return {
            children, dataset: {}, classList: { add() {}, toggle() {} },
            set innerHTML(value) { this.html = value; children.length = 0; },
            get innerHTML() { return this.html; },
            appendChild(child) { children.push(child); },
            addEventListener() {},
            setAttribute(name, value) { attributes.set(name, value); },
            getAttribute(name) { return attributes.get(name); },
            querySelector(selector) { return children.find(child => child.className === selector.slice(1)); },
            contains(child) { return children.includes(child); },
            focus() { document.activeElement = this; },
        };
    }
    const list = node();
    const ctx = vm.createContext({
        jobsData: [
            { id: 1, title: 'Manual work', status: 'done' },
            { id: 2, title: 'Plan', status: 'open', lifecycle: { stage_id: 'orch.planning' } },
            { id: 3, title: 'Ready', status: 'open', lifecycle: { stage_id: 'orch.ready' } },
            { id: 4, title: 'Cancelled', status: 'archived', lifecycle: { stage_id: 'orch.cancelled' } },
        ],
        jobPresentations: new Map(), _expandedGroups: new Set(), jobUnread: {},
        _compareJobsForList: (a, b) => a.id - b.id,
        document,
        window: { escapeHtml: value => value },
    });
    vm.runInContext(between(jobs, 'const jobPipelineLabels', '// Message Renderers')
        + between(jobs, 'function renderJobsList', '// Job conversation view'), ctx);
    ctx.registerJobPresentation('orch', {
        scoping: 'Scoping', active_coding: 'Active coding', 'orch.planning': 'Planning', 'orch.ready': 'Ready',
        'orch.cancelled': 'Cancelled', 'orch.in_review': 'In review', 'orch.done': 'Done',
    });
    const [filters] = list.children;
    const headers = list.children.filter(child => child.className?.startsWith('jobs-group-header'));
    assert.deepEqual(headers.map(header => header.textContent), [
        'Scoping', 'Awaiting approval', 'Active coding (3)', 'In review', 'Done (1)',
    ]);
    assert.deepEqual(filters.children.map(filter => filter.textContent), [
        'All', 'Scoping', 'Awaiting approval', 'Active coding', 'In review', 'Done',
    ]);
    const codingCards = list.children[list.children.indexOf(headers[2]) + 1].children[0].children;
    assert.deepEqual(codingCards.map(card => card.draggable), [true, false, false]);
    assert.ok(codingCards.every(card => card.innerHTML.includes('>Active coding</span>')));
    const [cancelled] = list.children[list.children.indexOf(headers[4]) + 1].children[0].children;
    assert.equal(cancelled.draggable, false);
    assert.match(cancelled.innerHTML, />Done<\/span>/);

    const statusButtons = () => list.children[0].children;
    const pressedLabels = () => statusButtons().filter(button => button.getAttribute('aria-pressed') === 'true')
        .map(button => button.textContent);
    const shownGroups = () => list.children.filter(child => child.className?.startsWith('jobs-group-header'))
        .map(header => header.textContent);
    const toggle = label => {
        const button = statusButtons().find(button => button.textContent === label);
        button.focus();
        button.onclick();
        assert.equal(document.activeElement.textContent, label);
        assert.notEqual(document.activeElement, button);
    };

    assert.deepEqual(pressedLabels(), ['All', 'Scoping', 'Awaiting approval', 'Active coding', 'In review', 'Done']);
    toggle('In review');
    assert.deepEqual(pressedLabels(), ['Scoping', 'Awaiting approval', 'Active coding', 'Done']);
    toggle('In review');
    assert.equal(pressedLabels()[0], 'All');
    toggle('All');
    assert.deepEqual(pressedLabels(), []);
    assert.deepEqual(shownGroups(), []);
    assert.equal(list.querySelector('.jobs-empty').textContent, 'Select a status to see Jobs.');
    toggle('In review');
    assert.equal(list.querySelector('.jobs-empty').textContent, 'No Jobs in the selected statuses.');
    toggle('In review');
    toggle('Done');
    toggle('Active coding');
    assert.deepEqual(pressedLabels(), ['Active coding', 'Done']);
    assert.deepEqual(shownGroups(), ['Active coding (3)', 'Done (1)']);
    toggle('All');
    assert.equal(pressedLabels().length, 6);
    assert.equal(shownGroups().length, 5);
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

test('Auto and the mention card use the same single POST and keep focus in main chat', async () => {
    const sent = [];
    const opened = [];
    const jobsData = [];
    const focused = [];
    const ctx = vm.createContext({
        window: { SESSION_TOKEN: 'session', async requireOk() {} }, jobsData,
        document: { getElementById: id => ({ focus() { focused.push(id); } }) },
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
    assert.deepEqual(opened, []);
    assert.deepEqual(focused, ['input', 'input']);
    assert.equal(jobsData.length, 1);
});

test('the read-only history mode requires persisted activation, even for an Orch-managed Job', () => {
    const ctx = vm.createContext({});
    vm.runInContext(between(jobs, 'function isOrchManagedJob', 'const jobHistoryJumpIcon'), ctx);
    assert.equal(ctx.jobUsesOrchHistory({ lifecycle: { managed_by: 'manual' } }), false);
    assert.equal(ctx.jobUsesOrchHistory({ lifecycle: { managed_by: 'orch' } }), false);
    assert.equal(ctx.jobUsesOrchHistory({ lifecycle: { managed_by: 'orch' }, orch_workflow_active: true }), true);
});

test('a deleted history target does not switch channels or disturb the current focus', async () => {
    const requests = [];
    const ctx = vm.createContext({
        SESSION_TOKEN: 'session',
        fetch: async (url, options) => {
            requests.push([url, options.headers['X-Session-Token']]);
            return { status: 404 };
        },
        switchChannel: () => assert.fail('A missing exact message must not switch channels'),
    });
    vm.runInContext(between(chat, 'async function showOrchHistoryMessage', 'window.showOrchHistoryMessage'), ctx);
    await assert.rejects(() => ctx.showOrchHistoryMessage('orch-uuid', 11542, 'old-channel'), /no longer available/);
    assert.deepEqual(requests, [['/api/jobs/orch-uuid/orch-history/11542', 'session']]);
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

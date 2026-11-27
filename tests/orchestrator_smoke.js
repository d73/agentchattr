const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

class Element {
    constructor() {
        this.dataset = {};
        this.attributes = {};
        this.events = {};
        this.children = [];
    }
    get childElementCount() { return this.children.length; }
    append(...children) { this.children.push(...children); }
    replaceChildren(...children) { this.children = children; }
    setAttribute(key, value) { this.attributes[key] = value; }
    addEventListener(event, callback) { this.events[event] = callback; }
}

async function main() {
    const elements = Object.fromEntries(
        ['panel', 'health', 'counters', 'capacity', 'toggle', 'queue'].map(name => [`orch-${name}`, new Element()]),
    );
    const element = name => elements[`orch-${name}`];
    const stage = { count: 1, items: [{ id: 1, title: 'Approval fixture' }] };
    const snapshot = {
        configured: true, version: 1, daemon: { state: 'online' },
        pipeline: { awaiting_approval: stage },
        staffing: { active: 1, recommended: 2, available: 0, shortfall: 1 },
        jobs: [{ id: 1, title: 'Approval fixture', stage: 'awaiting_approval', gates: [{ action: 'local_confirm' }] },
            { id: 2, title: 'Review fixture', stage: 'constructor',
                prs: [{ repo: 'team/example', number: 4 }, { repo: 'https://example.net', number: 7 }] }],
    };
    let response = snapshot;
    let refresh;
    let opened;
    const document = {
        getElementById: id => elements[id],
        createElement: () => new Element(),
        createElementNS: () => new Element(),
        addEventListener() {},
    };
    const window = {
        SESSION_TOKEN: 'fixture-session',
        setInterval(fn) { refresh = fn; },
        openJobFromBreadcrumb(id) { opened = id; },
    };
    const fetch = async (url, options) => {
        assert.equal(url, '/api/orchestrator/status');
        assert.equal(options.headers['X-Session-Token'], 'fixture-session');
        return { ok: true, json: async () => response };
    };
    const script = fs.readFileSync(path.join(__dirname, '..', 'static', 'orchestrator.js'), 'utf8');
    vm.runInNewContext(script, { document, window, fetch, AbortSignal });
    await new Promise(setImmediate);

    assert.equal(element('panel').hidden, false);
    assert.equal(element('queue').hidden, true);
    element('toggle').events.click();
    assert.equal(element('toggle').attributes['aria-expanded'], 'true');
    assert.equal(element('queue').hidden, false);
    assert.equal(element('health').title, 'Orchestrator online');
    assert.equal(element('counters').children.length, 5);
    assert.match(element('counters').children[1].title, /Job #1: Approval fixture/);
    assert.equal(element('capacity').children[1].textContent, '1/2');
    assert.equal(element('capacity').children[2].disabled, true);
    const [approval, review] = element('queue').children[0].children;
    approval.children[0].events.click();
    assert.equal(opened, 1);
    assert.equal(approval.children[2].children[0].children[0].disabled, true);
    assert.equal(review.children[2].children.length, 1);
    assert.equal(review.children[2].children[0].href, 'https://github.com/team/example/pull/4');

    response = { configured: true, version: 1, daemon: { state: 'offline' } };
    await refresh();
    assert.equal(element('health').title, 'Orchestrator offline');
    assert.match(element('counters').children[1].title, /Approval fixture/);
    response = { configured: false, version: 1, daemon: { state: 'offline' } };
    await refresh();
    assert.equal(element('panel').hidden, true);
}

main().catch(error => { console.error(error); process.exitCode = 1; });

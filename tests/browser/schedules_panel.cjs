// Run with: node --test tests/browser/schedules_panel.cjs
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const source = fs.readFileSync(path.join(__dirname, '../../static/chat.js'), 'utf8');
const panelCode = source.slice(source.indexOf('function handleScheduleEvent('), source.indexOf('// --- Schedule popover ---'));

class Element {
    constructor() { this.children = []; this.dataset = {}; this.attributes = {}; this.className = ''; this.textContent = ''; }
    get classList() {
        return {
            contains: name => this.className.split(' ').includes(name),
            toggle: (name, force) => {
                const values = new Set(this.className.split(' ').filter(Boolean));
                const enabled = force === undefined ? !values.has(name) : force;
                if (enabled) values.add(name); else values.delete(name);
                this.className = [...values].join(' ');
                return enabled;
            },
        };
    }
    append(...nodes) { this.children.push(...nodes); }
    appendChild(node) { this.append(node); }
    replaceChildren(...nodes) { this.children = nodes; }
    setAttribute(name, value) { this.attributes[name] = value; }
    focus() { this.focused = true; }
    querySelector(selector) { return this.children.find(child => child.classList.contains(selector.slice(1))); }
}

function harness() {
    const ids = Object.fromEntries(['schedules-panel', 'schedules-list', 'schedules-badge', 'schedules-toggle', 'schedules-feedback'].map(id => [id, new Element()]));
    ids['schedules-panel'].className = 'hidden';
    const calls = [];
    const ctx = vm.createContext({
        document: {
            getElementById: id => ids[id],
            createElement: () => new Element(),
            querySelectorAll: () => ids['schedules-list'].children.filter(el => el.classList.contains('schedule-card')),
        },
        window: { _preserveScroll: fn => fn() },
        SESSION_TOKEN: 'test-token',
        fetch: async (url, options) => { calls.push({ url, ...options }); return { ok: false }; },
    });
    vm.runInContext('let schedulesList = [];\n' + panelCode, ctx);
    const task = (id, extra = {}) => ({ id, prompt: 'Daily summary', targets: ['codex'], channel: 'stocks', daily_at: '08:00', active: true, next_run: Date.now() / 1000 + 3600, ...extra });
    return { ctx, ids, calls, task };
}

test('incoming tasks update badge without opening the panel; pause-only tasks remain discoverable', () => {
    const { ctx, ids, task } = harness();
    ctx.handleScheduleEvent('create', task('paused', { active: false }));
    ctx.handleScheduleEvent('create', task('active'));
    assert.equal(ids['schedules-panel'].classList.contains('hidden'), true);
    assert.equal(ids['schedules-badge'].textContent, '2');
    assert.equal(ids['schedules-list'].children[0].dataset.id, 'active');
    ctx.handleScheduleEvent('delete', { id: 'active' });
    assert.equal(ids['schedules-badge'].textContent, '1');
    assert.equal(ids['schedules-list'].children[0].classList.contains('paused'), true);
});

test('panel toggle and create confirmation expose tasks, with accessible button state and empty state', () => {
    const { ctx, ids } = harness();
    ctx.showScheduleConfirmation();
    assert.equal(ids['schedules-panel'].classList.contains('hidden'), false);
    assert.equal(ids['schedules-toggle'].attributes['aria-expanded'], 'true');
    assert.match(ids['schedules-list'].children[0].textContent, /No scheduled tasks/);
    assert.equal(ids['schedules-feedback'].textContent, 'Scheduled task created.');
    ctx.toggleSchedulesPanel(false);
    assert.equal(ids['schedules-panel'].classList.contains('hidden'), true);
    assert.equal(ids['schedules-toggle'].attributes['aria-expanded'], 'false');
    assert.equal(ids['schedules-toggle'].focused, true);
});

test('cards retain full prompts, channel, targets, and one-shot timing; countdown refresh preserves controls', () => {
    const { ctx, ids, task } = harness();
    const prompt = '<img src=x onerror=alert(1)> ' + 'Long prompt '.repeat(25);
    ctx.handleScheduleEvent('create', task('once', { prompt, one_shot: true }));
    ctx.toggleSchedulesPanel(true);
    const row = ids['schedules-list'].children[0];
    assert.equal(row.querySelector('.schedule-prompt').textContent, prompt);
    assert.equal(row.querySelector('.schedule-targets').textContent, '@codex');
    assert.equal(row.querySelector('.schedule-meta').children[0].textContent, '#stocks');
    assert.match(row.querySelector('.schedule-interval').textContent, /^once/);
    const actions = row.querySelector('.schedule-actions');
    ctx.refreshScheduleCountdowns();
    assert.equal(ids['schedules-list'].children[0].querySelector('.schedule-actions'), actions);
});

test('pause/resume and delete use authenticated endpoints and update without a WebSocket event', async () => {
    const { ctx, ids, task, calls } = harness();
    ctx.handleScheduleEvent('create', task('one'));
    let active = false;
    ctx.fetch = async (url, options) => { calls.push({ url, ...options }); return { ok: true, json: async () => task('one', { active }) }; };
    await ctx.toggleSchedule('one');
    assert.equal(ids['schedules-list'].children[0].classList.contains('paused'), true);
    active = true;
    await ctx.toggleSchedule('one');
    assert.equal(ids['schedules-list'].children[0].classList.contains('paused'), false);
    await ctx.deleteSchedule('one');
    assert.equal(calls[0].url, '/api/schedules/one/toggle');
    assert.equal(calls[0].method, 'PATCH');
    assert.equal(calls[0].headers['X-Session-Token'], 'test-token');
    assert.equal(calls[2].method, 'DELETE');
    assert.match(ids['schedules-list'].children[0].textContent, /No scheduled tasks/);
    assert.equal(ids['schedules-badge'].classList.contains('hidden'), true);
});

test('failed actions retain the task and display an error; duplicate clicks are ignored while pending', async () => {
    const { ctx, ids, task } = harness();
    ctx.handleScheduleEvent('create', task('one'));
    let release;
    let calls = 0;
    ctx.fetch = () => { calls++; return new Promise(resolve => { release = resolve; }); };
    const pending = ctx.toggleSchedule('one');
    await ctx.toggleSchedule('one');
    assert.equal(calls, 1);
    release({ ok: false });
    await pending;
    assert.equal(ids['schedules-list'].children.length, 1);
    assert.equal(ids['schedules-list'].children[0].classList.contains('paused'), false);
    assert.match(ids['schedules-feedback'].textContent, /Could not pause or resume/);
    assert.equal(ids['schedules-feedback'].classList.contains('error'), true);
});

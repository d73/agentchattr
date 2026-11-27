(() => {
    const panel = document.getElementById('orch-panel');
    if (!panel) return;
    const health = document.getElementById('orch-health');
    const counters = document.getElementById('orch-counters');
    const capacity = document.getElementById('orch-capacity');
    const toggle = document.getElementById('orch-toggle');
    const queue = document.getElementById('orch-queue');
    const stages = {
        scoping: ['Scoping', 'M4 15l-1 5 5-1L20 7l-4-4L4 15zm10-10l4 4'],
        awaiting_approval: ['Awaiting approval', 'M12 2l8 4v5c0 5-4 8-8 11-4-3-8-6-8-11V6l8-4zm-4 10l3 3 5-6'],
        active_coding: ['Active coding', 'M6 3l15 9-15 9V3z'],
        in_review: ['In review', 'M2 12s4-7 10-7 10 7 10 7-4 7-10 7S2 12 2 12zm13 0a3 3 0 1 1-6 0 3 3 0 0 1 6 0z'],
        done: ['Done', 'M22 12a10 10 0 1 1-20 0 10 10 0 0 1 20 0zM7 12l3 3 6-7'],
    };
    const symbols = {
        chevron: 'M5 9l7 7 7-7',
        capacity: 'M7 7h10v10H7zM9 2v4m6-4v4M9 18v4m6-4v4M2 9h4m-4 6h4m12-6h4m-4 6h4',
        lock: 'M6 11h12v10H6zM8 11V7a4 4 0 1 1 8 0v4m-4 4v2',
    };
    const array = value => Array.isArray(value) ? value : [];
    const validCount = value => Number.isSafeInteger(value) && value >= 0;
    const count = value => validCount(value) ? value : 0;
    const jobId = value => Number.isSafeInteger(value) && value > 0;
    let lastSnapshot;
    let lastRendered;
    let requestActive = false;

    function element(tag, className, value) {
        const node = document.createElement(tag);
        if (className) node.className = className;
        if (value !== undefined) node.textContent = String(value);
        return node;
    }

    function icon(path) {
        const ns = 'http://www.w3.org/2000/svg';
        const svg = document.createElementNS(ns, 'svg');
        const line = document.createElementNS(ns, 'path');
        for (const [key, value] of Object.entries({
            viewBox: '0 0 24 24', fill: 'none', stroke: 'currentColor', 'stroke-width': '2',
            'stroke-linecap': 'round', 'stroke-linejoin': 'round', class: 'orch-icon', 'aria-hidden': 'true',
        })) svg.setAttribute(key, value);
        line.setAttribute('d', path);
        svg.append(line);
        return svg;
    }

    function describe(node, label) {
        node.title = label;
        node.setAttribute('aria-label', label);
    }

    function setOpen(open) {
        toggle.setAttribute('aria-expanded', String(open));
        describe(toggle, open ? 'Collapse orchestrator queue' : 'Expand orchestrator queue');
        queue.hidden = !open;
    }

    function drawCounters(data) {
        counters.replaceChildren();
        for (const [key, [label, path]] of Object.entries(stages)) {
            const stage = data?.[key];
            const badge = element('span', 'orch-count orch-stage');
            badge.dataset.stage = key;
            badge.tabIndex = 0;
            const items = array(stage?.items).filter(item => jobId(item?.id)).slice(0, 5);
            const detail = items.map(item => `Job #${item.id}: ${String(item.title || '').slice(0, 120)}`).join('\n');
            const total = count(stage?.count);
            describe(badge, `${label} (${total})${detail ? `\n${detail}` : ''}`);
            badge.append(icon(path), element('b', '', total));
            counters.append(badge);
        }
    }

    function staffingRange(data) {
        const { minimum, recommended } = data || {};
        if (!validCount(minimum) || !validCount(recommended) || recommended < minimum || recommended === 0) {
            return null;
        }
        return `${minimum}–${recommended}`;
    }

    function liveLabel(data, online) {
        if (!validCount(data?.present)) return '— live';
        return `${data.present} ${online ? 'live' : 'last seen'}`;
    }

    function drawCapacity(data, online, activeJob) {
        const range = staffingRange(data);
        const live = liveLabel(data, online);
        const parts = [];
        if (validCount(data?.present)) {
            let availability = `Codex pool: ${data.present} ${online ? 'online' : 'last seen online'}`;
            if (validCount(data.live_free) && validCount(data.live_busy)) {
                availability += ` (${data.live_free} free, ${data.live_busy} busy`;
                if (count(data.live_unknown)) availability += `, ${data.live_unknown} unknown`;
                availability += ')';
            }
            parts.push(availability);
        } else {
            parts.push('Live Codex count unavailable');
        }
        if (range) parts.push(`${activeJob ? `Job #${activeJob.id} range` : 'Staffing range'}: ${range} workers`);
        if (validCount(data?.captured)) {
            const onlineCount = validCount(data.captured_online) ? `, ${data.captured_online} online` : '';
            parts.push(`Start pool: ${data.captured} captured${onlineCount}`);
        }
        parts.push('Pool locks at Start. Add sessions manually before Start.');
        describe(capacity, parts.join('\n'));
        capacity.tabIndex = 0;
        capacity.replaceChildren(icon(symbols.capacity), element('span', '', `${live} · ${range || '—'}`));
    }

    function renderQueue(rows, staffing, online) {
        const jobs = array(rows).filter(row => jobId(row?.id));
        const list = element('div', 'orch-queue-list');
        for (const job of jobs) {
            const row = element('div', 'orch-row');
            const [label, glyph] = Object.hasOwn(stages, job.stage) ? stages[job.stage] : stages.scoping;
            const open = element('button', 'orch-job-link', `Job #${job.id}`);
            open.type = 'button';
            describe(open, `Open Job #${job.id}`);
            open.addEventListener('click', () => window.openJobFromBreadcrumb?.(job.id));
            const title = element('div', 'orch-row-title orch-stage');
            title.dataset.stage = job.stage;
            title.append(icon(glyph), element('span', '', job.title || label));
            describe(title, label);
            const actions = element('div', 'orch-row-actions');
            for (const pr of array(job.prs)) {
                if (!/^[\w.-]+\/[\w.-]+$/.test(pr?.repo || '') || !jobId(pr.number)) continue;
                const link = element('a', 'orch-pr', `PR #${pr.number}`);
                link.href = `https://github.com/${pr.repo}/pull/${pr.number}`;
                link.target = '_blank';
                link.rel = 'noopener noreferrer';
                describe(link, `Open pull request #${pr.number}`);
                actions.append(link);
            }
            const waiting = job.awaiting_local === true ||
                array(job.gates).some(gate => gate?.action === 'local_confirm');
            if (waiting) {
                const data = job.staffing || (jobs.length === 1 ? staffing : null);
                const range = staffingRange(data);
                if (range) actions.append(element('span', 'orch-job-staffing', `${range} workers (${liveLabel(data, online)})`));
                const badge = element('span', 'orch-local-badge');
                badge.append(icon(symbols.lock), element('span', '', 'Awaiting local confirm'));
                badge.tabIndex = 0;
                describe(badge, 'Use the local coordinator to confirm or start this Job');
                actions.append(badge);
            }
            row.append(open, title, actions);
            list.append(row);
        }
        if (!list.childElementCount) list.append(element('p', 'orch-empty', 'No jobs in the queue'));
        queue.replaceChildren(list);
    }

    function render(snapshot) {
        panel.hidden = snapshot?.configured !== true;
        if (panel.hidden) return;
        const state = ['online', 'stale', 'offline'].includes(snapshot.daemon?.state) ? snapshot.daemon.state : 'offline';
        const signature = JSON.stringify([state, snapshot.pipeline, snapshot.staffing, snapshot.jobs]);
        if (signature === lastRendered) return;
        lastRendered = signature;
        health.dataset.state = state;
        describe(health, `Orchestrator ${state}`);
        drawCounters(snapshot.pipeline);
        const online = state === 'online';
        const activeJob = array(snapshot.jobs).find(job => jobId(job?.id) && job.stage !== 'done');
        drawCapacity(snapshot.staffing, online, activeJob);
        renderQueue(snapshot.jobs, snapshot.staffing, online);
    }

    async function refresh() {
        if (requestActive || document.hidden) return;
        requestActive = true;
        try {
            const response = await fetch('/api/orchestrator/status', {
                headers: { 'X-Session-Token': window.SESSION_TOKEN || '' },
                cache: 'no-store',
                signal: AbortSignal.timeout(4000),
            });
            if (!response.ok) throw new Error('Status unavailable');
            const result = await response.json();
            if (result?.version !== 1) throw new Error('Status version unavailable');
            if (result.configured && result.daemon?.state === 'offline' && lastSnapshot?.configured) {
                render({ ...lastSnapshot, daemon: result.daemon });
            } else {
                lastSnapshot = result;
                render(result);
            }
        } catch {
            if (lastSnapshot?.configured) render({ ...lastSnapshot, daemon: { state: 'offline' } });
        } finally {
            requestActive = false;
        }
    }

    toggle.append(icon(symbols.chevron));
    setOpen(false);
    toggle.addEventListener('click', () => setOpen(queue.hidden));
    document.addEventListener('visibilitychange', refresh);
    window.setInterval(refresh, 5000);
    refresh();
})();

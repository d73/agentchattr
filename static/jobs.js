/**
 * jobs.js -- Jobs panel UI module
 *
 * Extracted from chat.js (PR 3 of monolith breakup).
 * Depends on core.js (Hub) and store.js (Store) loaded first.
 *
 * Owns all job state, rendering, and interaction logic.
 * Subscribes to Hub for WS events.
 *
 * Reads from window: activeChannel, username, SESSION_TOKEN, ws,
 *                    agentConfig, escapeHtml, getColor, renderMarkdown,
 *                    getMentionCandidates, openImageModal,
 *                    _lastMentionedAgent, _preserveScroll, startReply,
 *                    soundEnabled, playNotificationSound
 */

// ---------------------------------------------------------------------------
// Job state (moved from chat.js globals)
// ---------------------------------------------------------------------------

let jobsData = []; // all jobs from server
let activeJobId = null; // currently viewing job in conversation view
let jobUnread = {}; // { job_id: unread_message_count }
let jobReplyTargets = {}; // { job_id: default agent recipient }
let pendingDeleteJobId = null;
let archiveDeleteBatchIds = null; // Set<number> while client-side archive delete animation is active
let jobReorderMute = null; // { ids:Set<number>, channel, status, until:number, suppressed:boolean }
let jobReorderMuteTimer = null;
let _jobViewSwitching = false;
let jobPendingAttachments = [];
let jobUploads = 0;
let jobMentionVisible = false;
let jobMentionIndex = 0;
let jobMentionStart = -1;
let jobRecipientOffset = null;
let _draggedJobId = null;
let _draggedJobStatus = null;
let _pendingJobReflowTops = null;
let _pendingJobReflowTimer = null;
const _expandedGroups = new Set(['open']); // track which collapsible groups are open across re-renders
const jobPresentations = new Map();
let orchHistoryRequest = 0;
const jobPipelineLabels = new Map([
    ['scoping', 'Scoping'],
    ['awaiting_approval', 'Awaiting approval'],
    ['active_coding', 'Active coding'],
    ['in_review', 'In review'],
    ['done', 'Done'],
]);
const selectedJobStatuses = new Set(jobPipelineLabels.keys());
const jobPipelineStages = new Map([
    ['orch.drafting', 'scoping'],
    ['orch.awaiting_approval', 'awaiting_approval'],
    ['orch.planning', 'active_coding'],
    ['orch.ready', 'active_coding'],
    ['orch.coding', 'active_coding'],
    ['orch.in_review', 'in_review'],
    ['orch.done', 'done'],
    ['orch.cancelled', 'done'],
]);
const manualJobFilters = { open: 'scoping', done: 'active_coding', archived: 'done' };
const manualJobStatuses = new Map(Object.entries(manualJobFilters).map(([status, filter]) => [filter, status]));
const proposalWriterOptions = [
    ['orch', 'Orch (Auto)'],
    ['antigravity', 'Agy (Manual Default)'],
    ['claude', 'Claude'],
    ['codex', 'Codex'],
];

function registerJobPresentation(id, stages) {
    const presentation = { stages: new Map(Object.entries(stages)), rows: new Map() };
    jobPresentations.set(id, presentation);
    renderJobsList();
    return (rows, status) => {
        if (status?.kind === 'workflow-coordinator' && status.pluginId === id) {
            presentation.orchOnline = status.online === true;
            const picker = document.querySelector('#convert-job-modal .convert-job-agent');
            if (picker) updateOrchWriterAvailability(picker);
        }
        const next = new Map(rows.map(row => [row.id, row]));
        if (next.size === presentation.rows.size && [...next].every(([id, row]) => {
            const prior = presentation.rows.get(id);
            return prior?.stage === row.stage && prior?.attention === row.attention && prior?.uid === row.uid;
        })) return;
        presentation.rows = next;
        renderJobsList();
    };
}

function jobPresentation(job) {
    const stage = job.lifecycle?.stage_id;
    const pipeline = jobPipelineStages.get(stage);
    const flagged = job.lifecycle?.flags?.includes('attention') ?? false;
    for (const presentation of jobPresentations.values()) {
        const row = presentation.rows.get(job.id);
        if (!row || row.uid !== job.uid || !presentation.stages.has(row.stage)) continue;
        const filter = pipeline ?? jobPipelineStages.get(row.stage) ?? row.stage;
        if (jobPipelineLabels.has(filter)) return { filter, workflow: true, attention: Boolean(row.attention) || flagged };
    }
    if (jobPresentations.size && pipeline) {
        return { filter: pipeline, workflow: true, attention: flagged };
    }
    return { filter: manualJobFilters[job.status], workflow: false, attention: false };
}

function isOrchManagedJob(job) {
    return job?.lifecycle?.managed_by === 'orch';
}

function jobUsesOrchHistory(job) {
    return isOrchManagedJob(job) && job.orch_workflow_active === true;
}

function activeJobUsesOrchHistory() {
    return jobUsesOrchHistory(jobsData.find(job => job.id === activeJobId));
}

const jobHistoryJumpIcon = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M6 18 18 6M9 6h9v9"/></svg>';
const jobHistoryLabels = new Map([
    ['intake', 'Intake'], ['revise', 'Revision request'], ['reply', 'Reply'],
    ['brief', 'Brief'], ['spec', 'Spec'], ['mockup', 'Mockup'],
    ['staffing', 'Staffing'], ['blocker', 'Blocker'], ['cancel', 'Cancellation'],
    ['reopen', 'Reopen request'], ['closeout', 'Outcome'],
    ['attach', 'Orch attached'], ['set_flags', 'Flags changed'], ['complete', 'Job completed'],
    ['submit_brief', 'Brief submitted'], ['revise_brief', 'Brief revision requested'],
    ['approve_brief', 'Brief approved'], ['plan_ready', 'Plan ready'], ['revise_plan', 'Plan revision requested'],
    ['change_scope', 'Scope changed'], ['start', 'Work started'], ['return_to_backlog', 'Returned to planning'],
    ['begin_review', 'Moved to review'], ['resume_coding', 'Returned to coding'],
    ['assignment', 'Task assigned'], ['pr_ready', 'PR ready'], ['changes_requested', 'Changes requested'],
    ['merge_verified', 'Merge verified'], ['result_rejected', 'Result rejected'],
    ['deadline_missed', 'Blocked'], ['delivery_unconfirmed', 'Delivery unconfirmed'],
    ['result_accepted_late', 'Recovered'], ['ack_accepted_late', 'Acknowledgment accepted late'],
    ['operator_retry', 'Operator retried'], ['result_accepted', 'Result accepted'],
    ['cleanup_required', 'Cleanup required'],
]);
const jobHistoryReasons = new Map([
    ['approval_only_for_spec', 'Approval is only valid for spec review'],
    ['invalid_result', 'Submitted result did not meet the task requirements'],
    ['ack_deadline', 'Acknowledgment deadline passed'],
    ['evidence_deadline', 'Evidence deadline passed'],
]);
const jobHistoryEventPattern = /^(?:orch:[0-9a-f]{32}|message:[0-9]{1,16}|(?:lifecycle|reopen):[a-zA-Z0-9_-]{1,128})$/;
const jobHistoryDate = new Intl.DateTimeFormat(undefined, { dateStyle: 'medium', timeStyle: 'short' });
const jobHistoryExactDate = new Intl.DateTimeFormat(undefined, { dateStyle: 'long', timeStyle: 'long' });
const jobHistoryTime = new Intl.DateTimeFormat(undefined, { timeStyle: 'short' });
const jobHistoryWarningIcon = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M12 3 2 20h20L12 3zm0 6v5m0 3h.01"/></svg>';
const jobHistoryDetailsIcon = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" aria-hidden="true"><path d="M4 7h16M4 12h16M4 17h16M8 5v4m8 1v4m-6 1v4"/></svg>';

// ---------------------------------------------------------------------------
// Message Renderers
// ---------------------------------------------------------------------------
// Register job message renderers so appendMessage in chat.js can
// delegate to us via window._messageRenderers[msg.type](el, msg).

if (!window._messageRenderers) window._messageRenderers = {};

window._messageRenderers['job_created'] = function (el, msg) {
    el.classList.add('system-msg', 'job-breadcrumb');
    const actId = msg.metadata?.job_id;
    if (actId && !jobsData.some(a => a.id === actId)) {
        el.style.display = 'none';
        el.dataset.hiddenReason = 'no-job-data';
    }
    if (actId) {
        el.innerHTML = `<span class="job-breadcrumb-link" onclick="openJobFromBreadcrumb(${actId})" title="Open job">
            <svg width="14" height="14" viewBox="0 0 16 16" fill="currentColor" style="vertical-align:-2px;margin-right:4px;opacity:0.6"><rect x="2" y="1" width="5" height="7" rx="1" fill="none" stroke="currentColor" stroke-width="1.5"/><rect x="9" y="8" width="5" height="7" rx="1" fill="none" stroke="currentColor" stroke-width="1.5"/></svg>
            New job: <em>${window.escapeHtml(msg.text.replace('Job created: ', ''))}</em></span>`;
    } else {
        el.innerHTML = `<span class="msg-text">${window.escapeHtml(msg.text)}</span>`;
    }
};

// ---------------------------------------------------------------------------
// Job breadcrumb helpers
// ---------------------------------------------------------------------------

function _unhideResolvedBreadcrumbs() {
    // After jobsData is populated (e.g. from WS 'jobs' event), unhide any
    // breadcrumbs that were hidden during history replay because jobsData
    // was still empty when the job_created message was rendered.
    // Only targets elements with data-hidden-reason="no-job-data" to avoid
    // unhiding off-channel breadcrumbs hidden by the channel filter.
    const validIds = new Set(jobsData.map(a => a.id));
    document.querySelectorAll('.job-breadcrumb[data-hidden-reason="no-job-data"]').forEach(el => {
        const link = el.querySelector('.job-breadcrumb-link');
        if (!link) return;
        const onclick = link.getAttribute('onclick') || '';
        const m = onclick.match(/openJobFromBreadcrumb\((\d+)\)/);
        if (m && validIds.has(Number(m[1]))) {
            el.style.display = '';
            delete el.dataset.hiddenReason;
        }
    });
}

function _collapseJobBreadcrumbs(container, newEl) {
    // Collect consecutive job-breadcrumb elements ending with newEl
    const crumbs = [newEl];
    let prev = newEl.previousElementSibling;
    let existingGroup = null;
    while (prev) {
        if (prev.classList.contains('job-breadcrumb')) {
            crumbs.unshift(prev);
            prev = prev.previousElementSibling;
        } else if (prev.classList.contains('job-group')) {
            // Already a collapsed group — absorb its children
            existingGroup = prev;
            const inner = [...prev.querySelectorAll('.job-breadcrumb')];
            inner.forEach(c => crumbs.unshift(c));
            prev = prev.previousElementSibling;
            existingGroup.remove();
            break;
        } else {
            break;
        }
    }

    if (crumbs.length < 2) return; // nothing to collapse

    // Remember insertion point (the element after the last crumb = newEl)
    const insertBefore = newEl.nextSibling;

    // Build the group wrapper
    const group = document.createElement('div');
    group.className = 'job-group';
    const summary = document.createElement('div');
    summary.className = 'job-group-summary';
    summary.innerHTML = `<svg width="14" height="14" viewBox="0 0 16 16" fill="currentColor" style="vertical-align:-2px;margin-right:4px;opacity:0.6"><rect x="2" y="1" width="5" height="7" rx="1" fill="none" stroke="currentColor" stroke-width="1.5"/><rect x="9" y="8" width="5" height="7" rx="1" fill="none" stroke="currentColor" stroke-width="1.5"/></svg>${crumbs.length} jobs were started`;
    summary.onclick = () => {
        group.classList.toggle('expanded');
    };
    group.appendChild(summary);

    const list = document.createElement('div');
    list.className = 'job-group-list';
    for (const c of crumbs) {
        list.appendChild(c); // moves from container into list
    }
    group.appendChild(list);

    // Insert group at the position where the crumbs were
    container.insertBefore(group, insertBefore);
}

function _repairJobGroup(group) {
    if (!group || !group.classList.contains('job-group')) return;
    const listEl = group.querySelector('.job-group-list');
    const remaining = listEl ? listEl.querySelectorAll('.job-breadcrumb') : [];
    if (remaining.length === 0) {
        group.remove();
    } else if (remaining.length === 1) {
        // Unwrap single breadcrumb out of the group
        group.replaceWith(remaining[0]);
    } else {
        // Update the count text
        const summary = group.querySelector('.job-group-summary');
        if (summary) {
            summary.innerHTML = summary.innerHTML.replace(/\d+ jobs were started/, `${remaining.length} jobs were started`);
        }
    }
}

// ---------------------------------------------------------------------------
// Jobs input setup
// ---------------------------------------------------------------------------

function setupJobsInput() {
    const input = document.getElementById('jobs-conv-input-text');
    if (!input) return;
    input.addEventListener('keydown', (e) => {
        if (e.isComposing) return;
        if (e.key === 'Enter' && !e.shiftKey && !jobMentionVisible) {
            e.preventDefault();
            sendJobMessage();
            return;
        }
        if (e.key === 'Tab' && !jobMentionVisible && activeJobId) {
            e.preventDefault();
            cycleJobReplyTarget(e.shiftKey ? -1 : 1);
        }
    });
    // Auto-grow
    input.addEventListener('input', () => {
        input.style.height = 'auto';
        input.style.height = Math.min(input.scrollHeight, 120) + 'px';
    });
}

// ---------------------------------------------------------------------------
// Job reply target helpers
// ---------------------------------------------------------------------------

function getJobRecipientOptions() {
    const opts = [];
    for (const [name, cfg] of Object.entries(window.agentConfig)) {
        if (cfg.state === 'pending') continue;
        opts.push({
            name,
            label: cfg.label || name,
            color: cfg.color || 'var(--accent)',
        });
    }
    return opts;
}

function _normalizeJobRecipient(name, options = null) {
    if (!name) return '';
    const opts = options || getJobRecipientOptions();
    const wanted = String(name).toLowerCase();
    const hit = opts.find(o => o.name.toLowerCase() === wanted);
    return hit ? hit.name : '';
}

function _extractJobMentionTargets(text) {
    if (!text) return [];
    const opts = getJobRecipientOptions();
    if (opts.length === 0) return [];
    const byLower = {};
    for (const o of opts) byLower[o.name.toLowerCase()] = o.name;
    const hits = [];
    const re = /@([a-zA-Z][\w-]*)/g;
    let m;
    while ((m = re.exec(text)) !== null) {
        const key = m[1].toLowerCase();
        const canonical = byLower[key];
        if (canonical && !hits.includes(canonical)) hits.push(canonical);
    }
    return hits;
}

function resolveJobDefaultRecipient(job, messages = []) {
    const opts = getJobRecipientOptions();
    if (opts.length === 0) return '';
    const hasStored = job && Object.prototype.hasOwnProperty.call(jobReplyTargets, job.id);
    if (hasStored) {
        const stored = jobReplyTargets[job.id];
        if (stored === null) return '';
        const normalized = _normalizeJobRecipient(stored, opts);
        if (normalized) return normalized;
    }

    // New/empty jobs should start un-targeted.
    if (!messages || messages.length === 0) return '';

    // For active threads with history, infer from last non-self agent sender.
    for (let i = messages.length - 1; i >= 0; i--) {
        const sender = String(messages[i]?.sender || '');
        if (!sender || sender.toLowerCase() === window.username.toLowerCase()) continue;
        const normalized = _normalizeJobRecipient(sender, opts);
        if (normalized) return normalized;
    }

    // Fallback to assignee if present and valid.
    const assignee = _normalizeJobRecipient(job?.assignee || '', opts);
    if (assignee) return assignee;
    return '';
}

function updateJobReplyTargetUI() {
    const btn = document.getElementById('job-reply-target-btn');
    const dot = document.getElementById('job-reply-target-dot');
    const nameEl = document.getElementById('job-reply-target-name');
    if (!btn || !dot || !nameEl) return;
    const opts = activeJobId ? getJobRecipientOptions() : [];
    if (opts.length === 0) {
        btn.classList.add('hidden');
        if (jobRecipientOffset !== null) closeJobMentionMenu();
        return;
    }
    const hasStored = Object.prototype.hasOwnProperty.call(jobReplyTargets, activeJobId);
    let selected = null;
    if (hasStored) {
        const stored = jobReplyTargets[activeJobId];
        if (stored !== null) {
            const normalized = _normalizeJobRecipient(stored, opts);
            selected = opts.find(o => o.name === normalized) || null;
        }
    }
    if (!selected && hasStored && jobReplyTargets[activeJobId] !== null) {
        jobReplyTargets[activeJobId] = null;
    }
    if (selected) {
        dot.style.background = selected.color || 'var(--accent)';
        nameEl.textContent = selected.label || selected.name;
        btn.title = `Choose reply target (currently ${selected.label || selected.name})`;
    } else {
        nameEl.textContent = 'none';
        btn.title = 'Choose reply target';
    }
    btn.classList.toggle('no-target', !selected);
    btn.classList.remove('hidden');
}

function cycleJobReplyTarget(step = 1) {
    if (!activeJobId) return;
    const opts = getJobRecipientOptions();
    if (opts.length === 0) return;
    const current = _normalizeJobRecipient(jobReplyTargets[activeJobId], opts);
    let idx = opts.findIndex(o => o.name === current);
    if (idx < 0) {
        idx = step < 0 ? opts.length - 1 : 0;
    } else {
        idx = (idx + step + opts.length) % opts.length;
    }
    jobReplyTargets[activeJobId] = opts[idx].name;
    updateJobReplyTargetUI();
    document.getElementById('jobs-conv-input-text')?.focus();
}

// ---------------------------------------------------------------------------
// Jobs panel toggle and view switching
// ---------------------------------------------------------------------------

function toggleJobsPanel() {
    if (!document.getElementById('jobs-panel').classList.contains('hidden')) {
        window.closeSidePanel('jobs');
        return;
    }
    window._preserveScroll(() => {
        window.setSidePanelOpen('jobs', true);
        showJobsListView();
        renderJobsList();
    });
}

function _animateJobViewSwitch(exitView, enterView, direction, onDone) {
    // No animation — instant swap
    if (exitView) exitView.classList.add('hidden');
    if (enterView) enterView.classList.remove('hidden');
    if (onDone) onDone();
}

function showJobsListView(onDone) {
    const listView = document.getElementById('jobs-list-view');
    const convView = document.getElementById('jobs-conversation-view');
    activeJobId = null;
    closeJobMentionMenu();
    updateJobReplyTargetUI();
    if (!convView.classList.contains('hidden')) {
        _animateJobViewSwitch(convView, listView, 'back', onDone);
    } else {
        listView.classList.remove('hidden');
        convView.classList.add('hidden');
        _jobViewSwitching = false;
        if (onDone) onDone();
    }
}

// ---------------------------------------------------------------------------
// Job sorting and ordering helpers
// ---------------------------------------------------------------------------

function _jobSortValue(a) {
    const raw = Number(a?.sort_order);
    return Number.isFinite(raw) ? raw : 0;
}

function _compareJobsForList(a, b) {
    const byOrder = _jobSortValue(b) - _jobSortValue(a);
    if (byOrder !== 0) return byOrder;
    return (b.updated_at || 0) - (a.updated_at || 0);
}

function _clearJobReorderTargets(container) {
    if (!container) return;
    container.querySelectorAll('.reorder-target-before, .reorder-target-after').forEach((el) => {
        el.classList.remove('reorder-target-before', 'reorder-target-after');
    });
}

function _orderedIdsForJobGroup(status) {
    return jobsData
        .filter(a => a.status === status)
        .sort(_compareJobsForList)
        .map(a => Number(a.id));
}

function _applyLocalJobOrder(status, orderedIds) {
    const n = orderedIds.length;
    const byId = new Map();
    orderedIds.forEach((id, idx) => byId.set(Number(id), n - idx));
    for (const a of jobsData) {
        if (a.status !== status) continue;
        const nextOrder = byId.get(Number(a.id));
        if (nextOrder != null) a.sort_order = nextOrder;
    }
}

function _isJobsListVisible() {
    const panel = document.getElementById('jobs-panel');
    return Boolean(panel && !panel.classList.contains('hidden') && !activeJobId);
}

function _beginJobReorderMute({ ids = [], channel = window.activeChannel, status = null, durationMs = 650 } = {}) {
    const muteIds = new Set(
        (ids || []).map(id => Number(id)).filter(id => Number.isFinite(id))
    );
    if (muteIds.size === 0) return;
    const ttl = Math.max(180, Number(durationMs) || 0);
    if (jobReorderMuteTimer) clearTimeout(jobReorderMuteTimer);
    jobReorderMute = {
        ids: muteIds,
        channel,
        status,
        until: Date.now() + ttl,
        suppressed: false,
    };
    jobReorderMuteTimer = setTimeout(() => {
        const muted = jobReorderMute;
        jobReorderMute = null;
        jobReorderMuteTimer = null;
        if (muted && muted.suppressed && _isJobsListVisible()) {
            renderJobsList();
        }
    }, ttl);
}

function _shouldSuppressJobUpdateRender(data) {
    if (!jobReorderMute || !data) return false;
    if (Date.now() > jobReorderMute.until) {
        if (jobReorderMuteTimer) {
            clearTimeout(jobReorderMuteTimer);
            jobReorderMuteTimer = null;
        }
        jobReorderMute = null;
        return false;
    }
    const id = Number(data.id);
    if (!Number.isFinite(id) || !jobReorderMute.ids.has(id)) return false;
    if (jobReorderMute.status && data.status !== jobReorderMute.status) return false;
    jobReorderMute.suppressed = true;
    return true;
}

async function _persistJobOrder(status, orderedIds) {
    const resp = await fetch('/api/jobs/reorder', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'X-Session-Token': window.SESSION_TOKEN },
        body: JSON.stringify({ status, ordered_ids: orderedIds }),
    });
    if (!resp.ok) {
        throw new Error(`Failed to persist order: ${resp.status}`);
    }
}

async function reorderJobWithinGroup(status, draggedId, targetId, insertAfter) {
    const ordered = _orderedIdsForJobGroup(status);
    const from = ordered.indexOf(Number(draggedId));
    const to = ordered.indexOf(Number(targetId));
    if (from < 0 || to < 0 || from === to) return;

    const [moved] = ordered.splice(from, 1);
    let insertAt = to + (insertAfter ? 1 : 0);
    if (from < insertAt) insertAt -= 1;
    ordered.splice(insertAt, 0, moved);

    _beginJobReorderMute({ ids: ordered, status });
    _applyLocalJobOrder(status, ordered);
    _flipRenderJobs();
    try {
        await _persistJobOrder(status, ordered);
    } catch (err) {
        console.error(err);
        // Reload canonical state from server on failure.
        try {
            const resp = await fetch('/api/jobs', {
                headers: { 'X-Session-Token': window.SESSION_TOKEN },
            });
            if (resp.ok) {
                jobsData = await resp.json();
                syncJobUnreadCache();
                renderJobsList();
            }
        } catch (reloadErr) {
            console.error('Failed to reload jobs after reorder failure:', reloadErr);
        }
    }
}

// ---------------------------------------------------------------------------
// FLIP animation helpers
// ---------------------------------------------------------------------------

function _flushPendingJobReflow() {
    if (!_pendingJobReflowTops) return;
    const tops = _pendingJobReflowTops;
    _pendingJobReflowTops = null;
    animateJobListReflow(tops);
}

function _flipRenderJobs() {
    const prevTops = captureJobCardTops();
    const scrollContainer = document.getElementById('jobs-list-view');
    const scrollY = scrollContainer ? scrollContainer.scrollTop : 0;

    renderJobsList();

    if (scrollContainer) scrollContainer.scrollTop = scrollY;

    // During HTML5 drag lifecycle, some browsers suppress paint/transition work.
    // Queue reflow animation until dragend for reliable visual playback.
    if (_draggedJobId) {
        if (!_pendingJobReflowTops) _pendingJobReflowTops = prevTops;
        if (_pendingJobReflowTimer) clearTimeout(_pendingJobReflowTimer);
        // Fallback in case dragend does not fire (e.g. source node replaced).
        _pendingJobReflowTimer = setTimeout(() => {
            _pendingJobReflowTimer = null;
            _flushPendingJobReflow();
        }, 120);
        return;
    }
    animateJobListReflow(prevTops);
}

// ---------------------------------------------------------------------------
// Render jobs list
// ---------------------------------------------------------------------------

function renderJobsList() {
    const list = document.getElementById('jobs-list');
    if (!list) return;
    // Preserve in-progress create form across re-renders (save focus state)
    const activeForm = list.querySelector('.job-create-form');
    let savedForm = null, savedFocusSelector = null, savedSelStart = 0, savedSelEnd = 0;
    if (activeForm) {
        const focused = document.activeElement;
        if (focused && activeForm.contains(focused)) {
            savedFocusSelector = focused.tagName.toLowerCase() + (focused.className ? '.' + focused.className.split(' ')[0] : '');
            savedSelStart = focused.selectionStart || 0;
            savedSelEnd = focused.selectionEnd || 0;
        }
        savedForm = activeForm.parentNode.removeChild(activeForm);
    }
    const oldFilters = list.querySelector('.job-group-filters');
    const focusedFilter = oldFilters?.contains(document.activeElement)
        ? document.activeElement.dataset.filter : undefined;
    list.innerHTML = '';

    // Jobs are global — show all regardless of channel
    const channelJobs = jobsData;

    const pipelineMode = jobPresentations.size > 0;
    const groups = pipelineMode ? Array.from(jobPipelineLabels, ([filter, label]) => {
        const status = manualJobStatuses.get(filter);
        return { key: status ?? filter, label, filter, items: [], readOnly: !status };
    }) : [
        { key: 'open', label: 'TO DO', items: [] },
        { key: 'done', label: 'ACTIVE', items: [] },
        { key: 'archived', label: 'CLOSED', items: [] },
    ];
    const presentations = new Map();
    for (const a of channelJobs) {
        const presentation = jobPresentation(a);
        presentations.set(a.id, presentation);
        const g = groups.find(g => pipelineMode ? g.filter === presentation.filter : g.key === a.status);
        if (g) g.items.push(a);
    }

    if (pipelineMode) {
        const filters = document.createElement('div');
        const allSelected = selectedJobStatuses.size === jobPipelineLabels.size;
        filters.className = 'job-group-filters';
        filters.setAttribute('role', 'group');
        filters.setAttribute('aria-label', 'Filter Jobs by status');
        for (const [key, label] of [[null, 'All'], ...jobPipelineLabels]) {
            const filter = document.createElement('button');
            filter.type = 'button';
            filter.dataset.filter = key ?? '';
            filter.textContent = label;
            filter.setAttribute('aria-pressed', String(key === null ? allSelected : selectedJobStatuses.has(key)));
            filter.onclick = () => {
                if (key === null) {
                    if (allSelected) selectedJobStatuses.clear();
                    else for (const status of jobPipelineLabels.keys()) selectedJobStatuses.add(status);
                } else if (!selectedJobStatuses.delete(key)) {
                    selectedJobStatuses.add(key);
                }
                renderJobsList();
            };
            filters.appendChild(filter);
        }
        list.appendChild(filters);
        if (focusedFilter !== undefined) {
            [...filters.children].find(button => button.dataset.filter === focusedFilter)?.focus();
        }
    }

    const visibleGroups = pipelineMode ? groups.filter(group => selectedJobStatuses.has(group.filter)) : groups;
    if (pipelineMode && (visibleGroups.length === 0
        || (channelJobs.length > 0 && !visibleGroups.some(group => group.items.length > 0)))) {
        const empty = document.createElement('div');
        empty.className = 'jobs-empty';
        empty.textContent = visibleGroups.length ? 'No Jobs in the selected statuses.' : 'Select a status to see Jobs.';
        list.appendChild(empty);
    } else if (channelJobs.length === 0) {
        const ghost = document.createElement('div');
        ghost.className = 'sb-ghost-card';
        ghost.innerHTML = `
            <div class="sb-ghost-title">Create your first job</div>
            <div class="sb-ghost-meta">Track work items with threaded conversations. Use @mentions to loop in agents.</div>
        `;
        ghost.onclick = () => {
            const btn = document.querySelector('.jobs-create-btn');
            if (btn) btn.click();
        };
        list.appendChild(ghost);
    }

    for (const group of visibleGroups) {
        if (pipelineMode && !channelJobs.length) continue;
        // Sort by explicit manual order first; fallback to recency.
        group.items.sort(_compareJobsForList);

        const isCollapsible = !pipelineMode && (group.key === 'open' || group.key === 'archived');
        const isExpanded = _expandedGroups.has(group.key);
        const isCollapsed = isCollapsible && !isExpanded;
        const header = document.createElement('div');
        header.className = 'jobs-group-header ' + (pipelineMode ? 'job-pipeline-group' : group.key)
            + (isCollapsible ? ' collapsible' : '') + (isCollapsed ? ' collapsed' : '');
        header.dataset.status = group.key;
        const isEmpty = group.items.length === 0;
        header.textContent = isEmpty ? group.label : `${group.label} (${group.items.length})`;
        if (isEmpty) header.classList.add('empty-group');
        if (isCollapsible) {
            header.onclick = () => {
                header.classList.toggle('collapsed');
                const container = header.nextElementSibling;
                if (container) container.classList.toggle('collapsed');
                if (header.classList.contains('collapsed')) {
                    _expandedGroups.delete(group.key);
                } else {
                    _expandedGroups.add(group.key);
                }
            };
        }

        // Drop target: drag a card from another group onto this header to change its status
        header.addEventListener('dragover', (e) => {
            if (group.readOnly || !_draggedJobId || _draggedJobStatus === group.key) return;
            e.preventDefault();
            header.classList.add('drop-target');
        });
        header.addEventListener('dragleave', () => {
            header.classList.remove('drop-target');
        });
        header.addEventListener('drop', async (e) => {
            e.preventDefault();
            e.stopPropagation();
            header.classList.remove('drop-target');
            const draggedId = _draggedJobId;
            if (group.readOnly || !draggedId || _draggedJobStatus === group.key) return;
            try {
                const response = await fetch(`/api/jobs/${draggedId}`, {
                    method: 'PATCH',
                    headers: { 'Content-Type': 'application/json', 'X-Session-Token': window.SESSION_TOKEN },
                    body: JSON.stringify({ status: group.key }),
                });
                if (!response.ok) throw new Error(`Status change rejected: ${response.status}`);
                const act = jobsData.find(a => String(a.id) === String(draggedId));
                _beginJobReorderMute({ ids: [draggedId], channel: window.activeChannel, status: group.key });
                if (act) act.status = group.key;
                _flipRenderJobs();

            } catch (err) { console.error('Failed to change status:', err); }
        });

        list.appendChild(header);

        // Wrap group items in a container for collapsing (CSS grid animation)
        const itemsContainer = document.createElement('div');
        itemsContainer.className = 'jobs-group-items' + (isCollapsed ? ' collapsed' : '');
        const itemsInner = document.createElement('div');
        itemsInner.className = 'jobs-group-items-inner';

        for (const a of group.items) {
            const card = document.createElement('div');
            card.className = 'job-card';
            const { attention, workflow } = presentations.get(a.id);
            const editableOrder = !group.readOnly && !isOrchManagedJob(a) && (!pipelineMode || !workflow);
            card.dataset.id = a.id;
            card.onclick = () => openJobConversation(a.id);
            card.tabIndex = 0;
            card.setAttribute('role', 'button');
            card.addEventListener('keydown', event => {
                if (event.target !== card || !['Enter', ' '].includes(event.key)) return;
                event.preventDefault();
                card.click();
            });
            card.addEventListener('selectstart', (e) => e.preventDefault());

            const unread = jobUnread[a.id] || 0;

            const unreadHtml = unread > 0
                ? `<span class="job-unread-dot" title="Unread messages">${unread > 99 ? '99+' : unread}</span>`
                : '';
            if (pipelineMode) {
                card.classList.add('job-pipeline-card');
                card.classList.toggle('job-card-attention', attention);
                const escape = window.escapeHtml;
                const date = new Date(a.created_at * 1000);
                const time = a.created_at && Number.isFinite(date.getTime())
                    ? `<time datetime="${date.toISOString()}">${escape(date.toLocaleTimeString(undefined, { hour: 'numeric', minute: '2-digit' }))}</time>`
                    : '';
                card.innerHTML = `
                    <div class="job-card-header">
                        ${attention ? '<span class="job-attention-dot" title="Needs your attention"></span>' : ''}
                        <span class="job-title">${escape(a.title)}</span>
                        <span class="job-stage-badge" data-stage="${group.filter}">${escape(group.label)}</span>
                        ${unreadHtml}
                    </div>
                    ${a.body ? `<div class="job-card-preview">${escape(a.body)}</div>` : ''}
                    ${a.channel || time ? `<div class="job-card-footer">
                        ${a.channel ? `<span class="job-origin">#${escape(a.channel)}</span>` : ''}${time}
                    </div>` : ''}
                `;
            } else {
                const msgCount = (a.messages || []).filter(m => !m?.deleted).length;
                card.innerHTML = `
                    <div class="job-card-header">
                        <span class="job-status-dot ${a.status}"></span>
                        <span class="job-title">${window.escapeHtml(a.title)}</span>
                        <span class="job-msg-count">${msgCount > 0 ? msgCount : ''}</span>
                        ${unreadHtml}
                    </div>
                `;
            }

            card.draggable = editableOrder;
            card.addEventListener('dragstart', (e) => {
                if (!editableOrder) return;
                let ids = [card.dataset.id];
                if (group.key === 'archived') {
                    const selected = [...itemsContainer.querySelectorAll('.archive-selected')].map(c => c.dataset.id);
                    // If dragging one of the selected cards, drag the whole selection.
                    if (selected.length > 0 && selected.includes(card.dataset.id)) {
                        ids = selected;
                    }
                }

                _draggedJobId = card.dataset.id;
                _draggedJobStatus = group.key;

                e.dataTransfer.setData('application/x-job-id', String(card.dataset.id));
                e.dataTransfer.setData('application/x-job-status', group.key);
                e.dataTransfer.setData('application/x-job-multi', ids.length > 1 ? '1' : '0');
                e.dataTransfer.setData('application/x-archive-ids', JSON.stringify(ids));
                e.dataTransfer.effectAllowed = 'move';

                card.classList.add('reorder-dragging');
                if (group.key === 'archived') {
                    card.classList.remove('archive-holding');
                    document.body.classList.add('archive-no-select');
                    ids.forEach(id => {
                        const el = itemsContainer.querySelector(`.job-card[data-id="${id}"]`);
                        if (el) el.classList.add('archive-dragging');
                    });
                    itemsContainer.classList.add('archive-drag-active');

                    const trash = itemsContainer.querySelector('.archive-trash-zone');
                    if (trash) {
                        trash.classList.add('drop-ready');
                        const hint = trash.querySelector('.archive-trash-hint');
                        if (hint) hint.textContent = ids.length > 1 ? `Drop to delete ${ids.length} jobs` : 'Drop to delete job';
                    }
                } else {
                    itemsContainer.classList.add('job-reorder-active');
                }
            });
            card.addEventListener('dragover', (e) => {
                if (!editableOrder) return;
                if (_draggedJobStatus !== group.key || !_draggedJobId || _draggedJobId === card.dataset.id) return;
                e.preventDefault();
                // Clear all other indicators first, then set this one
                _clearJobReorderTargets(itemsContainer);
                const rect = card.getBoundingClientRect();
                const before = e.clientY < rect.top + rect.height / 2;
                card.classList.toggle('reorder-target-before', before);
                card.classList.toggle('reorder-target-after', !before);
            });
            card.addEventListener('drop', async (e) => {
                if (!editableOrder) return;
                const draggedId = _draggedJobId;
                const draggedStatus = _draggedJobStatus;
                _clearJobReorderTargets(itemsContainer);
                if (!draggedStatus || draggedStatus !== group.key || !draggedId) return;
                if (draggedId === card.dataset.id) return;
                e.preventDefault();
                e.stopPropagation();
                const rect = card.getBoundingClientRect();
                const insertAfter = e.clientY >= rect.top + rect.height / 2;
                await reorderJobWithinGroup(group.key, draggedId, card.dataset.id, insertAfter);
            });
            card.addEventListener('dragend', () => {
                _draggedJobId = null;
                _draggedJobStatus = null;
                document.body.classList.remove('archive-no-select');
                card.classList.remove('reorder-dragging');
                itemsContainer.classList.remove('job-reorder-active', 'archive-drag-active');
                itemsContainer.querySelectorAll('.archive-dragging').forEach(c => c.classList.remove('archive-dragging'));
                _clearJobReorderTargets(itemsContainer);
                const trash = itemsContainer.querySelector('.archive-trash-zone');
                if (trash) {
                    trash.classList.remove('drop-ready', 'hover');
                    updateArchiveTrashHint(itemsContainer);
                }
                if (_pendingJobReflowTimer) {
                    clearTimeout(_pendingJobReflowTimer);
                    _pendingJobReflowTimer = null;
                }
                _flushPendingJobReflow();
            });

            // Archived cards: shift+click for multi-select
            if (editableOrder && group.key === 'archived') {
                card.classList.add('archive-selectable');
                const clearHoldState = () => card.classList.remove('archive-holding');
                card.addEventListener('pointerdown', (e) => {
                    if (e.button !== 0) return;
                    card.classList.add('archive-holding');
                });
                card.addEventListener('pointerup', clearHoldState);
                card.addEventListener('pointercancel', clearHoldState);
                card.addEventListener('mouseleave', clearHoldState);
                const origOnclick = card.onclick;
                card.onclick = (e) => {
                    if (e.shiftKey) {
                        e.stopPropagation();
                        card.classList.toggle('archive-selected');
                        updateArchiveTrashHint(itemsContainer);
                        return;
                    }
                    origOnclick(e);
                };
            }

            itemsInner.appendChild(card);
        }

        itemsContainer.addEventListener('dragover', (e) => {
            if (_draggedJobStatus !== group.key || !_draggedJobId) return;
            e.preventDefault();
        });
        itemsContainer.addEventListener('drop', async (e) => {
            if (e.target.closest('.job-card') || e.target.closest('.archive-trash-zone')) return;
            const draggedStatus = _draggedJobStatus;
            const draggedId = _draggedJobId;
            _clearJobReorderTargets(itemsContainer);
            if (!draggedStatus || draggedStatus !== group.key || !draggedId) return;
            e.preventDefault();
            const cards = [...itemsContainer.querySelectorAll('.job-card[draggable="true"]')];
            const lastCard = cards[cards.length - 1];
            if (!lastCard) return;
            if (String(lastCard.dataset.id) === String(draggedId)) return;
            await reorderJobWithinGroup(group.key, draggedId, lastCard.dataset.id, true);
        });

        // Add trash zone at the bottom of archived group — always visible
        if (group.key === 'archived' && itemsInner.querySelector('.archive-selectable')) {
            const trashZone = document.createElement('div');
            trashZone.className = 'archive-trash-zone visible';
            trashZone.innerHTML = `<svg width="16" height="16" viewBox="0 0 16 16" fill="none"><path d="M3 4h10M6 4V3h4v1M5 4v8.5h6V4" stroke="currentColor" stroke-width="1.3" stroke-linecap="round" stroke-linejoin="round"/></svg><span class="archive-trash-hint">Drag here to delete</span>`;

            // Click to delete selected items
            trashZone.addEventListener('click', async () => {
                const selected = [...itemsContainer.querySelectorAll('.archive-selected')];
                if (selected.length === 0) return;
                await deleteArchiveIds(selected.map(c => c.dataset.id), trashZone);
            });

            // Drag-and-drop support
            trashZone.addEventListener('dragover', (e) => { e.preventDefault(); e.dataTransfer.dropEffect = 'move'; trashZone.classList.add('hover'); _clearJobReorderTargets(itemsInner); });
            trashZone.addEventListener('dragleave', () => { trashZone.classList.remove('hover'); });
            trashZone.addEventListener('drop', async (e) => {
                e.preventDefault();
                trashZone.classList.remove('hover');
                itemsContainer.classList.remove('archive-drag-active');
                document.body.classList.remove('archive-no-select');
                let ids;
                try { ids = JSON.parse(e.dataTransfer.getData('application/x-archive-ids')); } catch { return; }
                if (!ids || ids.length === 0) return;
                await deleteArchiveIds(ids, trashZone);
            });

            itemsInner.appendChild(trashZone);
        }

        itemsContainer.appendChild(itemsInner);
        list.appendChild(itemsContainer);
    }

    // Re-insert preserved create form at top and restore focus
    if (savedForm) {
        list.prepend(savedForm);
        if (savedFocusSelector) {
            const el = savedForm.querySelector(savedFocusSelector);
            if (el) {
                el.focus();
                try { el.setSelectionRange(savedSelStart, savedSelEnd); } catch {}
            }
        }
    }
}

// ---------------------------------------------------------------------------
// Job conversation view
// ---------------------------------------------------------------------------

async function openJobConversation(jobId, { focus = true } = {}) {
    if (activeJobId !== jobId) clearJobAttachments();
    const job = jobsData.find(a => a.id === jobId);
    if (!job) return;
    closeJobMentionMenu();
    activeJobId = jobId;
    markJobRead(jobId);

    const listView = document.getElementById('jobs-list-view');
    const convView = document.getElementById('jobs-conversation-view');
    const readOnly = jobUsesOrchHistory(job);
    convView.classList.toggle('orch-job-history-mode', readOnly);
    document.getElementById('orch-job-history').hidden = !readOnly;
    document.getElementById('job-history-badge').hidden = !readOnly;
    document.getElementById('job-history-footer').hidden = !readOnly;

    // Prepare content while conv view is still hidden
    // Set header — click to edit title inline
    const titleEl = document.getElementById('jobs-conv-title');
    titleEl.textContent = readOnly ? `Job #${job.id}` : job.title;
    titleEl.onclick = readOnly ? null : () => startEditJobTitle(job, titleEl);
    updateJobToggles(job.status);

    // Render unified brief card header
    let briefEl = convView.querySelector('.job-brief-card');
    if (briefEl) briefEl.remove();
    let legacyBody = convView.querySelector('.job-body-brief');
    if (legacyBody) legacyBody.remove();
    let legacyPinned = convView.querySelector('.job-pinned-msg');
    if (legacyPinned) legacyPinned.remove();

    if (readOnly) {
        listView.classList.add('hidden');
        convView.classList.remove('hidden');
        if (focus) titleEl.focus();
        await loadOrchJobHistory(job);
        return;
    }

    if (job.body) {
        briefEl = document.createElement('div');
        briefEl.className = 'job-brief-card';
        briefEl.innerHTML = `<div class="job-brief-text">${window.renderMarkdown(job.body)}</div>`;
        const messagesContainer = document.getElementById('jobs-conv-messages');
        messagesContainer.parentNode.insertBefore(briefEl, messagesContainer);
    }

    // Load messages before showing the view
    const messages = await loadJobMessages(jobId);
    if (activeJobId !== jobId || activeJobUsesOrchHistory()) return;
    const target = resolveJobDefaultRecipient(job, messages);
    if (target) jobReplyTargets[jobId] = target;
    updateJobReplyTargetUI();

    // Switch views — instant swap (animation deferred to future Motion Guard impl)
    listView.classList.add('hidden');
    convView.classList.remove('hidden');

    // Pre-fill starter text for empty jobs
    const jobInput = document.getElementById('jobs-conv-input-text');
    const msgCount = (job.messages || []).filter(m => !m?.deleted).length;
    if (msgCount === 0 && target) {
        const starterText = `@${target} start this job`;
        jobInput.value = starterText;
        jobInput.placeholder = 'Send to assign · click to edit';
        jobInput.classList.add('job-starter-prefill');
        const clearStarter = () => {
            if (jobInput.classList.contains('job-starter-prefill')) {
                jobInput.value = '';
                jobInput.placeholder = 'Message...';
                jobInput.classList.remove('job-starter-prefill');
            }
            jobInput.removeEventListener('focus', clearStarter);
            jobInput.removeEventListener('click', clearStarter);
        };
        jobInput.addEventListener('focus', clearStarter);
        jobInput.addEventListener('click', clearStarter);
    } else {
        jobInput.placeholder = 'Message...';
        jobInput.classList.remove('job-starter-prefill');
        if (focus) jobInput.focus();
    }
}

async function jumpFromJobHistory(job, messageId, channel, feedback) {
    feedback.textContent = 'Opening message…';
    try {
        await window.showOrchHistoryMessage(job.uid, messageId, channel);
        feedback.textContent = '';
    } catch (error) {
        feedback.textContent = error.message;
    }
}

function historyDate(value) {
    if (typeof value !== 'number') return null;
    const date = new Date(value * 1000);
    return Number.isFinite(date.getTime()) ? date : null;
}

function historyLabel(kind) {
    return jobHistoryLabels.get(kind) || (kind || 'History entry').replace(/[_-]+/g, ' ')
        .replace(/^./, letter => letter.toUpperCase());
}

function historyDisplayDate(date) {
    if (date.toDateString() === new Date().toDateString()) return `Today ${jobHistoryTime.format(date)}`;
    return jobHistoryDate.format(date);
}

function addHistoryOutcome(summary, entry) {
    const flags = rows => rows.map(historyLabel).join(', ');
    let outcome = jobHistoryReasons.get(entry.reason_code);
    if (Array.isArray(entry.flags_added) && Array.isArray(entry.flags_removed)) {
        outcome = [entry.flags_added.length && `Added ${flags(entry.flags_added)}`,
            entry.flags_removed.length && `Removed ${flags(entry.flags_removed)}`].filter(Boolean).join(' · ');
    } else if (Array.isArray(entry.flags)) {
        outcome = entry.flags.length ? flags(entry.flags) : 'No flags';
    }
    const resolution = historyDate(entry.resolved_at);
    const line = document.createElement('div');
    line.className = 'job-history-text';
    if (outcome) line.textContent = outcome;
    if (resolution) {
        const resolved = document.createElement('span');
        resolved.className = 'job-history-resolution';
        resolved.textContent = `✓ Resolved ${jobHistoryTime.format(resolution)}`;
        resolved.title = jobHistoryExactDate.format(resolution);
        line.append(resolved);
    }
    if (typeof entry.repo === 'string' && /^[a-zA-Z0-9_.-]+\/[a-zA-Z0-9_.-]+$/.test(entry.repo)) {
        const [owner, repo] = entry.repo.split('/');
        if ([owner, repo].every(part => part !== '.' && part !== '..')) {
            const base = `https://github.com/${owner}/${repo}`;
            for (const [label, path] of [
                Number.isSafeInteger(entry.pr) && [`PR #${entry.pr}`, `pull/${entry.pr}`],
                typeof entry.commit === 'string' && /^[a-f0-9]{40,64}$/.test(entry.commit)
                    && [entry.commit.slice(0, 8), `commit/${entry.commit}`],
            ].filter(Boolean)) {
                const link = document.createElement('a');
                link.href = `${base}/${path}`;
                link.target = '_blank';
                link.rel = 'noopener noreferrer';
                link.textContent = label;
                line.append(link);
            }
        }
    }
    if (line.textContent) summary.append(line);
}

function renderOrchJobHistory(job, history) {
    const channel = history.channel || job.channel;
    document.getElementById('job-history-title').textContent = job.title;
    const overview = document.getElementById('job-history-channel');
    overview.textContent = channel ? `#${channel}` : '';
    const stage = document.getElementById('job-history-stage');
    stage.dataset.stage = jobPresentation(job).filter || '';
    stage.textContent = jobPipelineLabels.get(stage.dataset.stage) || '';
    const feedback = document.getElementById('job-history-status');
    const footer = document.getElementById('job-history-jump');
    const target = history.current_action_message_id;
    footer.hidden = !Number.isSafeInteger(target);
    document.getElementById('job-history-footer').hidden = footer.hidden;
    const label = `Reply or approve in #${channel}`;
    footer.textContent = label;
    footer.insertAdjacentHTML('beforeend', jobHistoryJumpIcon);
    footer.title = label;
    footer.onclick = () => jumpFromJobHistory(job, target, channel, feedback);

    const timeline = document.getElementById('job-history-timeline');
    const focusedMessage = timeline.contains(document.activeElement) ? document.activeElement.dataset.messageId : null;
    const focusedEvent = timeline.contains(document.activeElement) ? document.activeElement.dataset.eventId : null;
    const details = document.getElementById('job-history-details');
    const detailCount = (history.entries || []).filter(entry => entry.detail).length;
    function showDetails(open) {
        details.setAttribute('aria-expanded', String(open));
        details.title = `${open ? 'Hide' : 'Show'} details (${detailCount})`;
        details.setAttribute('aria-label', details.title);
        for (const row of timeline.querySelectorAll('[data-history-detail]')) row.hidden = !open;
        for (const group of timeline.querySelectorAll('.job-history-group')) {
            group.hidden = ![...group.querySelectorAll('.job-history-row')].some(row => !row.hidden);
        }
    }
    details.hidden = !detailCount;
    details.innerHTML = jobHistoryDetailsIcon;
    details.onclick = () => showDetails(details.getAttribute('aria-expanded') !== 'true');
    timeline.replaceChildren();
    let activeRevision, groupRows;
    for (const entry of history.entries || []) {
        const revision = Number.isSafeInteger(entry.revision) ? entry.revision : null;
        if (activeRevision !== revision) {
            activeRevision = revision;
            const group = document.createElement('li');
            group.className = 'job-history-group';
            if (revision !== null) {
                const title = document.createElement('h4');
                title.textContent = `Rev ${revision}`;
                group.append(title);
            }
            groupRows = document.createElement('ol');
            group.append(groupRows);
            timeline.append(group);
        }
        const item = document.createElement('li');
        item.className = 'job-history-row';
        if (entry.detail) item.dataset.historyDetail = '';
        if (typeof entry.event_id === 'string' && jobHistoryEventPattern.test(entry.event_id)) {
            item.dataset.eventId = entry.event_id;
            item.tabIndex = -1;
        }
        if (entry.problem) {
            const icon = document.createElement('span');
            icon.className = 'job-history-problem';
            icon.title = entry.resolved_at ? 'Resolved problem' : 'Needs attention';
            icon.innerHTML = jobHistoryWarningIcon;
            item.append(icon);
            item.classList.add(entry.resolved_at ? 'job-history-resolved' : 'job-history-needs-action');
        }
        const summary = document.createElement('div');
        summary.className = 'job-history-milestone';
        const heading = document.createElement('strong');
        heading.textContent = [historyLabel(entry.kind), entry.choice].filter(Boolean).join(' · ');
        summary.appendChild(heading);
        const date = historyDate(entry.time);
        const stamp = [date && historyDisplayDate(date), entry.actor].filter(Boolean).join(' · ');
        if (stamp) {
            const meta = document.createElement('div');
            meta.className = 'job-history-meta';
            meta.textContent = stamp;
            if (date) meta.title = jobHistoryExactDate.format(date);
            summary.appendChild(meta);
        }
        addHistoryOutcome(summary, entry);
        item.appendChild(summary);
        if (Number.isSafeInteger(entry.message_id)) {
            const destination = entry.channel || channel;
            const button = document.createElement('button');
            button.className = 'side-panel-icon';
            button.type = 'button';
            button.title = `Jump to #${entry.message_id} in #${destination}`;
            button.setAttribute('aria-label', button.title);
            button.dataset.messageId = entry.message_id;
            button.innerHTML = jobHistoryJumpIcon;
            button.onclick = () => jumpFromJobHistory(job, entry.message_id, destination, feedback);
            item.appendChild(button);
        }
        if (entry.message_id === history.current_action_message_id && entry.message_id != null) {
            item.classList.add('job-history-needs-action');
        }
        groupRows.appendChild(item);
    }
    showDetails(details.getAttribute('aria-expanded') === 'true');
    if (focusedMessage) {
        [...timeline.querySelectorAll('[data-message-id]')]
            .find(button => button.dataset.messageId === focusedMessage)?.focus({ preventScroll: true });
    } else if (focusedEvent) {
        [...timeline.querySelectorAll('[data-event-id]')]
            .find(row => row.dataset.eventId === focusedEvent)?.focus({ preventScroll: true });
    }
}

async function refreshOrchJobHistory(job) {
    const request = ++orchHistoryRequest;
    const feedback = document.getElementById('job-history-status');
    try {
        const response = await fetch(`/api/jobs/${encodeURIComponent(job.uid)}/orch-history`, {
            headers: { 'X-Session-Token': window.SESSION_TOKEN },
        });
        await window.requireOk(response);
        const history = await response.json();
        if (activeJobId !== job.id || !activeJobUsesOrchHistory() || request !== orchHistoryRequest) return;
        if (history.job_uid !== job.uid) throw new Error('Job history does not match this Job.');
        renderOrchJobHistory(job, history);
        feedback.textContent = history.entries?.length ? '' : 'No milestones recorded yet.';
    } catch {
        if (activeJobId === job.id && request === orchHistoryRequest) {
            feedback.textContent = 'Job History is unavailable.';
        }
    }
}

async function loadOrchJobHistory(job) {
    document.getElementById('job-history-details').setAttribute('aria-expanded', 'false');
    renderOrchJobHistory(job, {});
    const brief = document.getElementById('job-history-brief');
    brief.innerHTML = job.body ? window.renderMarkdown(job.body) : '';
    brief.hidden = !job.body;
    document.getElementById('job-history-status').textContent = 'Loading history…';
    const audit = document.getElementById('job-history-audit');
    audit.hidden = true;
    audit.open = false;
    const messages = document.getElementById('job-history-audit-messages');
    await Promise.all([
        refreshOrchJobHistory(job),
        loadJobMessages(job.id, messages, true).then(rows => {
            if (activeJobId === job.id) audit.hidden = !rows.some(row => !row?.deleted);
        }),
    ]);
}

async function loadJobMessages(jobId, container = document.getElementById('jobs-conv-messages'), readOnly = false) {
    container.innerHTML = '';

    try {
        const resp = await fetch(`/api/jobs/${jobId}/messages`, {
            headers: { 'X-Session-Token': window.SESSION_TOKEN }
        });
        if (!resp.ok) return [];
        const msgs = await resp.json();
        if (activeJobId !== jobId || activeJobUsesOrchHistory() !== readOnly) return [];
        const visibleMsgs = msgs.filter(msg => !msg?.deleted);

        if (visibleMsgs.length === 0) {
            // Only show empty state if there's no brief card either
            const convView = document.getElementById('jobs-conversation-view');
            const hasBrief = !!convView.querySelector('.job-brief-card');
            if (!hasBrief && !readOnly) {
                container.innerHTML = '<div class="jobs-empty job-thread-empty">No messages yet. Start the conversation!</div>';
            }
            return msgs;
        }

        // Render all messages in the scrollable area
        for (const msg of visibleMsgs) {
            appendJobMessage(msg, container, readOnly);
        }

        if (!readOnly) container.scrollTop = container.scrollHeight;
        return msgs;
    } catch (e) {
        if (activeJobId === jobId) container.innerHTML = '<div class="jobs-empty">Failed to load messages.</div>';
        return [];
    }
}

// ---------------------------------------------------------------------------
// Job message delete
// ---------------------------------------------------------------------------

const _jobMsgDeleteTimers = new Map();
function _clearJobMsgDeleteTimer(jobId, msgId) {
    const key = `${jobId}:${msgId}`;
    const timer = _jobMsgDeleteTimers.get(key);
    if (timer) clearTimeout(timer);
    _jobMsgDeleteTimers.delete(key);
}

function _animateRemoveJobMessage(msgEl) {
    if (!msgEl || msgEl.dataset.removing === '1') {
        if (msgEl) msgEl?.remove?.();
        return;
    }
    msgEl.dataset.removing = '1';
    const h = msgEl.offsetHeight || 0;
    msgEl.style.maxHeight = `${h}px`;
    msgEl.style.overflow = 'hidden';
    msgEl.style.willChange = 'max-height, opacity, transform, margin, padding';
    requestAnimationFrame(() => {
        msgEl.classList.add('job-msg-removing');
        msgEl.style.maxHeight = '0px';
    });
    const cleanup = () => {
        msgEl.removeEventListener('transitionend', cleanup);
        msgEl.remove();
    };
    msgEl.addEventListener('transitionend', cleanup);
    setTimeout(cleanup, 240);
}

function _renderJobMsgDeleteDefault(actions, jobId, msgId) {
    if (!actions) return;
    actions.classList.remove('confirming');
    actions.innerHTML = `<button class="job-msg-del-btn" onclick="startDeleteJobMessage(${jobId}, ${msgId})">DEL</button>`;
}

function startDeleteJobMessage(jobId, msgId, event) {
    event?.preventDefault?.();
    event?.stopPropagation?.();
    const selector = `.job-msg[data-msg-id="${msgId}"]`;
    const msgEl = document.querySelector(selector);
    const actions = msgEl?.querySelector('.job-msg-actions');
    if (!actions) return;

    document.querySelectorAll('.job-msg-actions.confirming').forEach((el) => {
        const parent = el.closest('.job-msg');
        const priorId = Number(parent?.dataset.msgId || NaN);
        if (Number.isFinite(priorId) && priorId !== Number(msgId)) {
            _clearJobMsgDeleteTimer(jobId, priorId);
            _renderJobMsgDeleteDefault(el, jobId, priorId);
        }
    });

    actions.classList.add('confirming');
    actions.innerHTML = `
        <span class="job-msg-delete-label">Delete?</span>
        <button class="job-msg-confirm-yes" onclick="confirmDeleteJobMessage(${jobId}, ${msgId})">Yes</button>
        <button class="job-msg-confirm-no" onclick="cancelDeleteJobMessage(${jobId}, ${msgId})">No</button>
    `;
    _clearJobMsgDeleteTimer(jobId, msgId);
    const key = `${jobId}:${msgId}`;
    _jobMsgDeleteTimers.set(key, setTimeout(() => {
        cancelDeleteJobMessage(jobId, msgId);
    }, 4000));
}

async function confirmDeleteJobMessage(jobId, msgId, event) {
    event?.preventDefault?.();
    event?.stopPropagation?.();
    _clearJobMsgDeleteTimer(jobId, msgId);
    try {
        const resp = await fetch(`/api/jobs/${jobId}/messages/${msgId}`, {
            method: 'DELETE',
            headers: { 'X-Session-Token': window.SESSION_TOKEN },
        });
        if (!resp.ok) {
            cancelDeleteJobMessage(jobId, msgId);
            return;
        }
        const msgEl = document.querySelector(`.job-msg[data-msg-id="${msgId}"]`);
        _animateRemoveJobMessage(msgEl);
    } catch (err) {
        console.error('Failed to delete job message:', err);
        cancelDeleteJobMessage(jobId, msgId);
    }
}

function cancelDeleteJobMessage(jobId, msgId, event) {
    event?.preventDefault?.();
    event?.stopPropagation?.();
    _clearJobMsgDeleteTimer(jobId, msgId);
    const msgEl = document.querySelector(`.job-msg[data-msg-id="${msgId}"]`);
    const actions = msgEl?.querySelector('.job-msg-actions');
    _renderJobMsgDeleteDefault(actions, jobId, msgId);
}

// ---------------------------------------------------------------------------
// Append job message to conversation
// ---------------------------------------------------------------------------

function appendJobMessage(msg, container = document.getElementById('jobs-conv-messages'), readOnly = false) {
    if (!container || msg?.deleted) return;

    const div = document.createElement('div');
    div.className = 'job-msg' + (msg.type === 'suggestion' ? ' job-suggestion' : '');
    div.dataset.msgId = String(msg.id ?? '');
    const senderColor = window.getColor(msg);
    const msgId = Number(msg.id);
    const canDelete = Number.isFinite(msgId);
    const deleteActionHtml = canDelete && !readOnly
        ? `<div class="job-msg-actions"><button class="job-msg-del-btn" onclick="startDeleteJobMessage(${activeJobId}, ${msgId})">DEL</button></div>`
        : '';

    if (msg.type === 'suggestion') {
        const resolved = msg.resolved;
        div.innerHTML = `
            <div class="job-msg-header">
                <span class="suggestion-pill">Suggestion</span>
                <span class="job-msg-sender" style="color: ${senderColor}">${window.escapeHtml(msg.sender)}</span>
                <span class="job-msg-time">${msg.time || ''}</span>
                ${deleteActionHtml}
            </div>
            <div class="job-msg-text">${window.renderMarkdown(msg.text)}</div>
            <div class="suggestion-actions">${resolved
                ? `<span class="suggestion-resolved">${window.escapeHtml(resolved)}</span>`
                : readOnly ? '' : `<button class="suggestion-accept" onclick="acceptSuggestion(${activeJobId}, ${msg.id})">Accept</button><button class="suggestion-dismiss" onclick="dismissSuggestion(${activeJobId}, ${msg.id})">Dismiss</button>`
            }</div>
        `;
    } else {
        let attHtml = '';
        if (msg.attachments && msg.attachments.length > 0) {
            attHtml = '<div class="job-msg-attachments">';
            for (const att of msg.attachments) {
                attHtml += attachmentMarkup(att);
            }
            attHtml += '</div>';
        }
        div.innerHTML = `
            <div class="job-msg-header">
                <span class="job-msg-sender" style="color: ${senderColor}">${window.escapeHtml(msg.sender)}</span>
                <span class="job-msg-time">${msg.time || ''}</span>
                ${deleteActionHtml}
            </div>
            ${msg.text ? `<div class="job-msg-text">${renderOrchEvent(msg.text) ?? window.renderMarkdown(msg.text)}</div>` : ''}
            ${attHtml}
        `;
    }
    container.appendChild(div);
}

function renderOrchEvent(text) {
    const prefix = 'orch-event ';
    if (!text.startsWith(prefix)) return null;

    const raw = text.slice(prefix.length);
    let event;
    try {
        event = JSON.parse(raw);
    } catch {
        return null;
    }
    if (event?.v !== 1 || typeof event.kind !== 'string' || !event.kind.trim()) return null;

    const escape = window.escapeHtml;
    const revision = Number.isSafeInteger(event.revision) && event.revision > 0
        ? ` · Rev ${event.revision}` : '';
    const disclosure = `<details class="job-event">
        <summary title="Toggle raw Orch event JSON">
            <svg viewBox="0 0 16 16" aria-hidden="true" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><path d="m5.5 4-4 4 4 4m5-8 4 4-4 4m-1-10-3 12"/></svg>
            <span>orch-event · ${escape(event.kind)}${revision}</span>
        </summary>
        <pre class="job-event-json">${escape(raw)}</pre>
    </details>`;

    const brief = event.brief;
    if (event.kind !== 'brief' || typeof brief?.outcome !== 'string') return disclosure;

    const strings = values => Array.isArray(values)
        ? values.filter(value => typeof value === 'string' && value.trim()) : [];
    const list = (label, values) => {
        const items = strings(values);
        return items.length
            ? `<section><h4>${label}</h4><ul>${items.map(item => `<li>${escape(item)}</li>`).join('')}</ul></section>`
            : '';
    };
    const gates = strings(brief.gates);
    const repo = typeof brief.repo === 'string' && brief.repo.trim()
        ? `<span class="job-event-tag">${escape(brief.repo)}</span>` : '';
    return `${disclosure}
        <article class="job-event-brief">
            <header><h3>Brief${revision}</h3>${repo}</header>
            <section><h4>What</h4><p>${escape(brief.outcome)}</p></section>
            ${list('Done when', brief.checks)}
            ${list('Limits', brief.constraints)}
            ${gates.length ? `<section class="job-event-gates"><h4>Gates</h4>${gates.map(gate => `<span class="job-event-tag">${escape(gate[0].toUpperCase() + gate.slice(1))}</span>`).join('')}</section>` : ''}
        </article>`;
}

// ---------------------------------------------------------------------------
// Job attachments
// ---------------------------------------------------------------------------

async function sendJobMessage() {
    if (!activeJobId || activeJobUsesOrchHistory()) return;
    const input = document.getElementById('jobs-conv-input-text');
    const text = input.value.trim();
    if (jobUploads > 0 || (!text && jobPendingAttachments.length === 0)) return;
    closeJobMentionMenu();
    const explicitTargets = _extractJobMentionTargets(text);
    const hasBroadcastMention = /@(?:all|both)\b/i.test(text);
    let outboundText = text;
    if (explicitTargets.length > 0) {
        jobReplyTargets[activeJobId] = explicitTargets[0];
    } else if (!hasBroadcastMention) {
        const target = _normalizeJobRecipient(jobReplyTargets[activeJobId]);
        if (target) {
            outboundText = text ? `@${target} ${text}` : `@${target}`;
        }
    }

    try {
        const resp = await fetch(`/api/jobs/${activeJobId}/messages`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json', 'X-Session-Token': window.SESSION_TOKEN },
            body: JSON.stringify({
                text: outboundText,
                sender: window.username,
                attachments: jobPendingAttachments,
            }),
        });
        await window.requireOk(resp);
        if (resp.ok) {
            input.value = '';
            input.style.height = 'auto';
            clearJobAttachments();
            updateJobReplyTargetUI();
        }
    } catch (e) {
        window.showActionError('Could not send job message. Your text and attachments are still available.', e);
    }
}

async function uploadJobImage(file, jobId = activeJobId) {
    if (!activeJobId || activeJobUsesOrchHistory()) return;
    if (jobId !== activeJobId) return `${file.name}: job changed. Please attach the file again.`;
    const status = document.getElementById('job-attachment-status');
    jobUploads++;
    document.getElementById('job-send').disabled = true;
    status.textContent = `Uploading ${file.name}…`;
    try {
        const data = await uploadAttachment(file);
        if (jobId !== activeJobId) throw new Error('Job changed. Please attach the file again.');
        jobPendingAttachments.push(data);
        renderJobAttachments();
        status.textContent = '';
    } catch (error) {
        status.textContent = `${file.name}: ${error.message}`;
        return status.textContent;
    } finally {
        jobUploads--;
        document.getElementById('job-send').disabled = jobUploads > 0;
    }
}

function renderJobAttachments() {
    const container = document.getElementById('job-attachments');
    if (!container) return;
    container.innerHTML = '';
    jobPendingAttachments.forEach((att, i) => {
        container.appendChild(createAttachmentPreview(att, () => removeJobAttachment(i)));
    });
}

function removeJobAttachment(index) {
    jobPendingAttachments.splice(index, 1);
    renderJobAttachments();
}

function clearJobAttachments() {
    jobPendingAttachments = [];
    const container = document.getElementById('job-attachments');
    if (container) container.innerHTML = '';
}

// ---------------------------------------------------------------------------
// Job title editing and status toggles
// ---------------------------------------------------------------------------

function startEditJobTitle(job, titleEl) {
    if (titleEl.querySelector('input')) return; // already editing
    const input = document.createElement('input');
    input.type = 'text';
    input.className = 'job-title-input';
    input.value = job.title;
    input.maxLength = 120;
    titleEl.textContent = '';
    titleEl.appendChild(input);
    input.focus();
    input.select();

    const commit = async () => {
        if (activeJobId !== job.id || activeJobUsesOrchHistory()) return;
        const newTitle = input.value.trim();
        if (newTitle && newTitle !== job.title) {
            try {
                await fetch(`/api/jobs/${job.id}`, {
                    method: 'PATCH',
                    headers: { 'Content-Type': 'application/json', 'X-Session-Token': window.SESSION_TOKEN },
                    body: JSON.stringify({ title: newTitle }),
                });
                job.title = newTitle;
            } catch (e) { console.error('Failed to update title:', e); }
        }
        titleEl.textContent = job.title;
        titleEl.onclick = () => startEditJobTitle(job, titleEl);
    };

    input.addEventListener('keydown', (e) => {
        if (e.key === 'Enter') { e.preventDefault(); input.blur(); }
        if (e.key === 'Escape') { input.value = job.title; input.blur(); }
    });
    input.addEventListener('blur', commit, { once: true });
}

async function toggleJobStatus(status) {
    if (!activeJobId) return;
    const job = jobsData.find(a => a.id === activeJobId);
    if (!job || jobUsesOrchHistory(job)) return;
    const oldStatus = job.status;
    const statusLabels = { 'open': 'TO DO', 'done': 'ACTIVE', 'archived': 'CLOSED' };

    try {
        await fetch(`/api/jobs/${activeJobId}`, {
            method: 'PATCH',
            headers: { 'Content-Type': 'application/json', 'X-Session-Token': window.SESSION_TOKEN },
            body: JSON.stringify({ status }),
        });
        // Update local data
        job.status = status;
        if (status === 'archived') {
            jobsBack();
        } else {
            updateJobToggles(status);
        }
        renderJobsList();
    } catch (e) {
        console.error('Failed to update job status:', e);
    }
}

function updateJobToggles(activeStatus) {
    const toggles = document.querySelectorAll('#jobs-status-toggles .job-toggle');
    toggles.forEach(t => {
        t.classList.toggle('active', t.dataset.status === activeStatus);
    });
}

// ---------------------------------------------------------------------------
// Create job
// ---------------------------------------------------------------------------

function showCreateJob() {
    const list = document.getElementById('jobs-list');
    if (!list) return;
    // Remove existing form if any
    const existing = list.querySelector('.job-create-form');
    if (existing) { existing.remove(); return; }

    const form = document.createElement('div');
    form.className = 'job-create-form';
    form.innerHTML = `
        <input type="text" placeholder="Job title" class="job-create-title" maxlength="120" autofocus>
        <textarea placeholder="Description (optional)" class="job-create-body" maxlength="1000" rows="2"></textarea>
        <div class="job-create-actions">
            <button class="cancel-btn" onclick="this.closest('.job-create-form').remove()">Cancel</button>
            <button class="create-btn" onclick="submitCreateJob(this)">Create</button>
        </div>
    `;
    list.prepend(form);
    const titleInput = form.querySelector('.job-create-title');
    titleInput.focus();
    // Enter on title moves to body, Enter on empty body submits
    titleInput.addEventListener('keydown', (e) => {
        if (e.key === 'Enter') {
            e.preventDefault();
            form.querySelector('.job-create-body').focus();
        } else if (e.key === 'Escape') {
            form.remove();
        }
    });
    const bodyTA = form.querySelector('.job-create-body');
    bodyTA.addEventListener('keydown', (e) => {
        if (e.key === 'Escape') form.remove();
    });
    bodyTA.addEventListener('input', () => {
        bodyTA.style.height = 'auto';
        bodyTA.style.height = bodyTA.scrollHeight + 'px';
    });
}

async function submitCreateJob(btn) {
    const form = btn.closest('.job-create-form');
    const titleInput = form.querySelector('.job-create-title');
    const title = titleInput.value.trim();
    if (!title) { titleInput.focus(); return; }
    const bodyInput = form.querySelector('.job-create-body');
    const jobBody = bodyInput ? bodyInput.value.trim() : '';

    try {
        const response = await fetch('/api/jobs', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json', 'X-Session-Token': window.SESSION_TOKEN },
            body: JSON.stringify({
                title,
                body: jobBody,
                type: 'job',
                channel: window.activeChannel,
                created_by: window.username,
                assignee: window._lastMentionedAgent || '',
            }),
        });
        await window.requireOk(response);
        form.remove();
    } catch (e) {
        window.showActionError('Could not create job. Your text is still in the form.', e);
    }
}

// ---------------------------------------------------------------------------
// Navigation
// ---------------------------------------------------------------------------

function jobsBack() {
    const lastId = activeJobId;
    const restoreFocus = activeJobUsesOrchHistory();
    showJobsListView(() => {
        renderJobsList();
        if (restoreFocus) {
            document.querySelector(`#jobs-list .job-card[data-id="${lastId}"]`)?.focus();
        }
    });
}

// ---------------------------------------------------------------------------
// Job WS event handler
// ---------------------------------------------------------------------------

function handleJobEvent(action, data) {
    let suppressListRender = false;
    if (action === 'create') {
        if (!jobsData.some(a => a.id === data.id)) jobsData.push(data);
        if (!Object.prototype.hasOwnProperty.call(jobUnread, data.id)) {
            jobUnread[data.id] = 0;
        }
    } else if (action === 'update') {
        const idx = jobsData.findIndex(a => a.id === data.id);
        const wasReadOnly = jobUsesOrchHistory(jobsData[idx]);
        if (idx >= 0) jobsData[idx] = data;
        if (activeJobId === data.id) {
            if (wasReadOnly !== jobUsesOrchHistory(data)) {
                void openJobConversation(data.id, { focus: false });
            } else if (wasReadOnly) {
                void refreshOrchJobHistory(data);
            }
        }
        if (!Object.prototype.hasOwnProperty.call(jobUnread, data.id)) {
            jobUnread[data.id] = 0;
        }
        suppressListRender = _shouldSuppressJobUpdateRender(data);
    } else if (action === 'message') {
        // data = { job_id, message }
        const job = jobsData.find(a => a.id === data.job_id);
        if (job) {
            if (!job.messages) job.messages = [];
            job.messages.push(data.message);
        }
        const panel = document.getElementById('jobs-panel');
        const convView = document.getElementById('jobs-conversation-view');
        const isViewingThis = Boolean(
            panel &&
            !panel.classList.contains('hidden') &&
            convView &&
            !convView.classList.contains('hidden') &&
            activeJobId === data.job_id
        );
        const sender = (data.message && data.message.sender) ? String(data.message.sender) : '';
        const isSelfMessage = sender.toLowerCase() === window.username.toLowerCase();
        const msgType = data.message.type || 'chat';
        if (!isSelfMessage) {
            const normalized = _normalizeJobRecipient(sender);
            const hasStoredTarget = Object.prototype.hasOwnProperty.call(jobReplyTargets, data.job_id);
            if (normalized && hasStoredTarget && jobReplyTargets[data.job_id] !== null) {
                jobReplyTargets[data.job_id] = normalized;
                if (isViewingThis) updateJobReplyTargetUI();
            }
        }

        // Play notification sound for new job messages from others (matching channel behavior)
        if (window.soundEnabled && !document.hasFocus() && msgType === 'chat' && !isSelfMessage && sender && window.mentionsUser && window.mentionsUser(data.message.text)) {
            window.playNotificationSound(sender);
        }

        // If we're viewing this job, append the message. Otherwise count unread.
        if (isViewingThis) {
            if (jobUsesOrchHistory(job)) {
                const audit = document.getElementById('job-history-audit');
                audit.hidden = false;
                appendJobMessage(data.message, document.getElementById('job-history-audit-messages'), true);
            } else {
                appendJobMessage(data.message);
                const container = document.getElementById('jobs-conv-messages');
                if (container) container.scrollTop = container.scrollHeight;
            }
            markJobRead(data.job_id);
        } else if (!isSelfMessage) {
            jobUnread[data.job_id] = (jobUnread[data.job_id] || 0) + 1;
            // A mention of you in another job thread plays the normal mention sound
            if (window.soundEnabled && document.hasFocus() && msgType === 'chat' && window.playNotificationSound && window.mentionsUser && window.mentionsUser(data.message.text)) {
                window.playNotificationSound(sender);
            }
        }
    } else if (action === 'message_delete') {
        // data = { job_id, message_id }
        const job = jobsData.find(a => a.id === data.job_id);
        if (job && Array.isArray(job.messages)) {
            const hit = job.messages.find(m => Number(m.id) === Number(data.message_id));
            if (hit) {
                hit.deleted = true;
                hit.text = '';
                hit.attachments = [];
            }
        }
        const panel = document.getElementById('jobs-panel');
        const convView = document.getElementById('jobs-conversation-view');
        const isViewingThis = Boolean(
            panel &&
            !panel.classList.contains('hidden') &&
            convView &&
            !convView.classList.contains('hidden') &&
            activeJobId === data.job_id
        );
        if (isViewingThis) {
            const id = jobUsesOrchHistory(job) ? 'job-history-audit-messages' : 'jobs-conv-messages';
            const msgEl = document.getElementById(id).querySelector(`.job-msg[data-msg-id="${data.message_id}"]`);
            _animateRemoveJobMessage(msgEl);
            markJobRead(data.job_id);
        }
    } else if (action === 'delete') {
        jobsData = jobsData.filter(a => a.id !== data.id);
        delete jobUnread[data.id];
        // Remove breadcrumb from timeline and fix stale group wrappers
        document.querySelectorAll('.job-breadcrumb').forEach(el => {
            const link = el.querySelector('.job-breadcrumb-link');
            if (link) {
                const onclick = link.getAttribute('onclick') || '';
                if (onclick.includes(`openJobFromBreadcrumb(${data.id})`)) {
                    const group = el.closest('.job-group');
                    el.remove();
                    if (group) _repairJobGroup(group);
                }
            }
        });
        if (activeJobId === data.id) {
            showJobsListView();
        }
    }
    updateJobsBadge();
    // Re-render list if visible
    const panel = document.getElementById('jobs-panel');
    if (panel && !panel.classList.contains('hidden') && !activeJobId) {
        if (_jobViewSwitching) return;
        if (action === 'delete' && archiveDeleteBatchIds && archiveDeleteBatchIds.has(Number(data.id))) {
            return;
        }
        if (suppressListRender) return;
        renderJobsList();
    }
}

// ---------------------------------------------------------------------------
// Convert-to-Job Lightbox
// ---------------------------------------------------------------------------

function updateOrchWriterAvailability(select) {
    const online = [...jobPresentations.values()].some(presentation => presentation.orchOnline);
    const auto = select.options[0];
    auto.disabled = !online;
    auto.title = online ? '' : 'Orch is offline';
    select.title = auto.title;
    if (!online && select.value === 'orch') select.value = 'antigravity';
    return online;
}

function showConvertToJobModal(msgId) {
    const msgEl = document.querySelector(`.message[data-id="${msgId}"]`);
    if (!msgEl) return;
    const rawText = msgEl.dataset.rawText || '';

    let modal = document.getElementById('convert-job-modal');
    if (!modal) {
        modal = document.createElement('div');
        modal.id = 'convert-job-modal';
        modal.className = 'convert-job-modal hidden';
        modal.innerHTML = `
            <div class="convert-job-dialog">
                <h3 class="convert-job-title">Convert to Job</h3>
                <div class="convert-job-preview"></div>
                <label class="convert-job-label" for="convert-job-writer">Proposal Writer</label>
                <select id="convert-job-writer" class="convert-job-agent"></select>
                <div class="convert-job-actions">
                    <button class="convert-job-cancel">Cancel</button>
                    <button class="convert-job-confirm">Create Job</button>
                </div>
            </div>`;
        modal.addEventListener('click', (e) => {
            if (e.target === modal) closeConvertJobModal();
        });
        document.body.appendChild(modal);
    }

    modal.dataset.sourceMsgId = String(msgId);

    // Populate message preview
    const previewEl = modal.querySelector('.convert-job-preview');
    previewEl.innerHTML = window.renderMarkdown(rawText.substring(0, 500));

    const selectEl = modal.querySelector('.convert-job-agent');
    selectEl.innerHTML = '';
    for (const [name, label] of proposalWriterOptions) {
        const opt = document.createElement('option');
        opt.value = name;
        opt.textContent = label;
        selectEl.appendChild(opt);
    }
    selectEl.value = updateOrchWriterAvailability(selectEl) ? 'orch' : 'antigravity';

    // Wire buttons (clone to remove old listeners)
    const cancelBtn = modal.querySelector('.convert-job-cancel');
    const confirmBtn = modal.querySelector('.convert-job-confirm');
    const newCancel = cancelBtn.cloneNode(true);
    cancelBtn.parentNode.replaceChild(newCancel, cancelBtn);
    const newConfirm = confirmBtn.cloneNode(true);
    confirmBtn.parentNode.replaceChild(newConfirm, confirmBtn);

    newCancel.addEventListener('click', closeConvertJobModal);
    newConfirm.addEventListener('click', _doConvertToJob);

    modal.classList.remove('hidden');
    requestAnimationFrame(() => selectEl.focus());
}

async function _doConvertToJob() {
    const modal = document.getElementById('convert-job-modal');
    if (!modal) return;
    const writer = modal.querySelector('.convert-job-agent').value;
    const sourceMsgId = Number(modal.dataset.sourceMsgId);
    const button = modal.querySelector('.convert-job-confirm');
    if (button.disabled || !proposalWriterOptions.some(([value]) => value === writer)
        || !Number.isSafeInteger(sourceMsgId)) return;

    button.disabled = true;
    try {
        if (writer === 'orch') {
            await createOrchJob(sourceMsgId);
        } else {
            const result = await postJobAction('/api/jobs/proposal-requests', {
                writer_base: writer, source_msg_id: sourceMsgId,
            });
            window.showToast?.(result.status === 'queued' ? 'Proposal request queued.' : 'Proposal requested.');
        }
        closeConvertJobModal();
    } catch (e) {
        window.showActionError('Could not create Job. Please try again.', e);
    } finally {
        button.disabled = false;
    }
}

async function postJobAction(path, body) {
    const response = await fetch(path, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'X-Session-Token': window.SESSION_TOKEN },
        body: JSON.stringify(body),
    });
    await window.requireOk(response);
    return response.json();
}

async function createOrchJob(sourceMsgId, cardMsgId) {
    const body = { source_msg_id: sourceMsgId, proposal_writer: 'orch' };
    if (cardMsgId !== undefined) body.card_msg_id = cardMsgId;
    const { job } = await postJobAction('/api/jobs/orch-intake', body);
    if (!jobsData.some(existing => existing.id === job.id)) jobsData.push(job);
    window.showToast?.(`Job #${job.id} is linked to this conversation.`);
    document.getElementById('input')?.focus();
}

window.createOrchJobFromCard = async function(button, sourceMsgId, cardMsgId) {
    if (button.disabled) return;
    button.disabled = true;
    try {
        await createOrchJob(sourceMsgId, cardMsgId);
    } catch (e) {
        window.showActionError('Could not create Job. Please try again.', e);
    } finally {
        button.disabled = false;
    }
};

function closeConvertJobModal() {
    const modal = document.getElementById('convert-job-modal');
    if (modal) modal.classList.add('hidden');
}

// ---------------------------------------------------------------------------
// Delete-Job Lightbox
// ---------------------------------------------------------------------------

function showDeleteJobModal(jobId) {
    let modal = document.getElementById('delete-job-modal');
    if (!modal) {
        modal = document.createElement('div');
        modal.id = 'delete-job-modal';
        modal.className = 'convert-job-modal hidden';
        modal.innerHTML = `
            <div class="convert-job-dialog delete-job-dialog">
                <h3 class="convert-job-title">Delete Job Permanently?</h3>
                <p class="convert-job-subtitle">This removes the job and its messages permanently. This cannot be undone.</p>
                <div class="delete-job-target"></div>
                <div class="convert-job-actions">
                    <button class="convert-job-cancel">Cancel</button>
                    <button class="delete-job-confirm">Delete</button>
                </div>
            </div>`;
        modal.addEventListener('click', (e) => {
            if (e.target === modal) closeDeleteJobModal();
        });
        document.body.appendChild(modal);
    }

    pendingDeleteJobId = jobId;
    const job = jobsData.find(a => a.id === jobId);
    const target = modal.querySelector('.delete-job-target');
    if (target) {
        const title = job?.title || `Job #${jobId}`;
        target.textContent = title;
    }

    const cancelBtn = modal.querySelector('.convert-job-cancel');
    const confirmBtn = modal.querySelector('.delete-job-confirm');
    const newCancel = cancelBtn.cloneNode(true);
    cancelBtn.parentNode.replaceChild(newCancel, cancelBtn);
    const newConfirm = confirmBtn.cloneNode(true);
    confirmBtn.parentNode.replaceChild(newConfirm, confirmBtn);
    newCancel.addEventListener('click', closeDeleteJobModal);
    newConfirm.addEventListener('click', confirmDeleteJobPermanent);

    modal.classList.remove('hidden');
    requestAnimationFrame(() => newConfirm.focus());
}

function closeDeleteJobModal() {
    const modal = document.getElementById('delete-job-modal');
    if (modal) modal.classList.add('hidden');
    pendingDeleteJobId = null;
}

function startJobFromMessage(msgId) {
    showConvertToJobModal(msgId);
}

// ---------------------------------------------------------------------------
// Proposal accept/dismiss/request-changes
// ---------------------------------------------------------------------------

async function acceptProposal(msgId) {
    const msgEl = document.querySelector(`.message[data-id="${msgId}"]`);
    if (!msgEl) return;
    const title = msgEl.dataset.proposalTitle;
    const body = msgEl.dataset.proposalBody;
    const proposalSender = msgEl.dataset.proposalSender;
    if (!title) return;

    try {
        const job = await postJobAction('/api/jobs', {
            title,
            body: body || '',
            type: 'job',
            channel: msgEl.dataset.channel || window.activeChannel,
            created_by: proposalSender,
            anchor_msg_id: msgId,
        });
        if (job && job.id) {
            // Update the proposal card to show "Accepted"
            const card = msgEl.querySelector('.proposal-card');
            if (card) {
                card.classList.add('proposal-resolved');
                const actions = card.querySelector('.proposal-actions');
                if (actions) actions.innerHTML = '<div class="proposal-status-resolved">Accepted</div>';
            }
            if (!jobsData.some(existing => existing.id === job.id)) jobsData.push(job);
            await openJobFromBreadcrumb(job.id);
        }
    } catch (e) {
        window.showActionError('Could not accept the proposal. Please try again.', e);
    }
}

async function dismissProposal(msgId) {
    // Demote on server — converts proposal to regular chat message
    try {
        await fetch(`/api/messages/${msgId}/demote`, {
            method: 'POST',
            headers: { 'X-Session-Token': window.SESSION_TOKEN },
        });
    } catch (e) {
        console.error('Failed to demote proposal:', e);
    }
}

async function requestChangesProposal(msgId) {
    // Demote proposal to chat message, then open a reply to it
    try {
        await fetch(`/api/messages/${msgId}/demote`, {
            method: 'POST',
            headers: { 'X-Session-Token': window.SESSION_TOKEN },
        });
        // Wait briefly for WS edit event to re-render the message as chat
        setTimeout(() => window.startReply(msgId), 200);
    } catch (e) {
        console.error('Failed to request changes on proposal:', e);
    }
}

// ---------------------------------------------------------------------------
// Breadcrumb navigation
// ---------------------------------------------------------------------------

async function openJobFromBreadcrumb(jobId) {
    const job = jobsData.find(a => a.id === jobId);
    if (!job) return;
    window._preserveScroll(() => window.setSidePanelOpen('jobs', true));
    await openJobConversation(jobId);
}

// ---------------------------------------------------------------------------
// Suggestion accept/dismiss in jobs
// ---------------------------------------------------------------------------

async function acceptSuggestion(jobId, msgIndex) {
    try {
        await fetch(`/api/jobs/${jobId}/messages/${msgIndex}/resolve`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json', 'X-Session-Token': window.SESSION_TOKEN },
            body: JSON.stringify({ resolution: 'accepted' }),
        });
        // Reload conversation to reflect change
        await loadJobMessages(jobId);
    } catch (e) {
        console.error('Failed to accept suggestion:', e);
    }
}

async function dismissSuggestion(jobId, msgIndex) {
    try {
        await fetch(`/api/jobs/${jobId}/messages/${msgIndex}/resolve`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json', 'X-Session-Token': window.SESSION_TOKEN },
            body: JSON.stringify({ resolution: 'dismissed' }),
        });
        await loadJobMessages(jobId);
    } catch (e) {
        console.error('Failed to dismiss suggestion:', e);
    }
}

// ---------------------------------------------------------------------------
// Permanent delete for archived jobs
// ---------------------------------------------------------------------------

async function deleteJobPermanent(jobId) {
    showDeleteJobModal(jobId);
}

async function confirmDeleteJobPermanent() {
    const jobId = pendingDeleteJobId;
    if (!jobId) return;
    closeDeleteJobModal();
    try {
        await fetch(`/api/jobs/${jobId}?permanent=true`, {
            method: 'DELETE',
            headers: { 'X-Session-Token': window.SESSION_TOKEN },
        });
        jobsData = jobsData.filter(a => a.id !== jobId);
        renderJobsList();
    } catch (e) {
        console.error('Failed to delete job:', e);
    }
}

// ---------------------------------------------------------------------------
// Archive trash helpers
// ---------------------------------------------------------------------------

function captureJobCardTops() {
    const tops = new Map();
    document.querySelectorAll('#jobs-list .job-card').forEach((card) => {
        const id = Number(card.dataset.id);
        if (!Number.isFinite(id)) return;
        tops.set(id, card.getBoundingClientRect().top);
    });
    return tops;
}

function animateJobListReflow(prevTops) {
    if (!prevTops || prevTops.size === 0) return;
    const moved = [];
    document.querySelectorAll('#jobs-list .job-card').forEach((card) => {
        const id = Number(card.dataset.id);
        if (!prevTops.has(id)) return;
        const previous = prevTops.get(id);
        const next = card.getBoundingClientRect().top;
        const dy = previous - next;
        if (Math.abs(dy) < 1) return;
        moved.push({ card, dy });
    });
    if (moved.length === 0) return;

    // FLIP: set initial offset (no transition)
    for (const { card, dy } of moved) {
        card.style.transition = 'none';
        card.style.transform = `translateY(${dy}px)`;
    }

    // Force the browser to commit the offset before starting the transition
    void document.body.offsetHeight;

    // Use setTimeout to escape any browser D&D paint suppression logic
    setTimeout(() => {
        for (const { card } of moved) {
            card.style.transition = 'transform 260ms cubic-bezier(0.22, 1, 0.36, 1)';
            card.style.transform = 'translateY(0)';
            const cleanup = () => {
                card.style.transition = '';
                card.style.transform = '';
                card.removeEventListener('transitionend', cleanup);
            };
            card.addEventListener('transitionend', cleanup);
            // Fallback cleanup in case transitionend doesn't fire
            setTimeout(cleanup, 300);
        }
    }, 20);
}

async function deleteArchiveIds(ids, trashZone) {
    const normalizedIds = [...new Set(
        (ids || []).map(id => Number(id)).filter(id => Number.isFinite(id))
    )];
    if (normalizedIds.length === 0) return;
    const prevTops = captureJobCardTops();
    archiveDeleteBatchIds = new Set(normalizedIds);
    const itemsContainer = trashZone.closest('.jobs-group-items');
    if (itemsContainer) {
        for (const id of normalizedIds) {
            const el = itemsContainer.querySelector(`.job-card[data-id="${id}"]`);
            if (el) el.classList.add('archive-removing');
        }
    }
    trashZone.classList.add('chomping');
    const deletedIds = [];
    for (const id of normalizedIds) {
        try {
            const resp = await fetch(`/api/jobs/${id}?permanent=true`, {
                method: 'DELETE',
                headers: { 'X-Session-Token': window.SESSION_TOKEN },
            });
            if (resp.ok) deletedIds.push(id);
        } catch (err) { console.error('Failed to delete job:', err); }
    }
    if (deletedIds.length > 0) {
        const deletedSet = new Set(deletedIds);
        jobsData = jobsData.filter(a => !deletedSet.has(Number(a.id)));
        for (const id of deletedIds) delete jobUnread[id];
        renderJobsList();
        animateJobListReflow(prevTops);
        updateJobsBadge();
    }
    setTimeout(() => { trashZone.classList.remove('chomping'); }, 500);
    setTimeout(() => { archiveDeleteBatchIds = null; }, 1200);
}

function updateArchiveTrashHint(container) {
    const trash = container.querySelector('.archive-trash-zone');
    if (!trash) return;
    const count = container.querySelectorAll('.archive-selected').length;
    const hint = trash.querySelector('.archive-trash-hint');
    if (count > 0) {
        trash.classList.add('has-selection');
        hint.textContent = `Delete ${count} selected`;
    } else {
        trash.classList.remove('has-selection');
        hint.textContent = 'Drag here to delete';
    }
}

// ---------------------------------------------------------------------------
// Unread badge helpers
// ---------------------------------------------------------------------------

function syncJobUnreadCache() {
    const validIds = new Set((jobsData || []).map(a => Number(a.id)));
    for (const id of validIds) {
        if (!Object.prototype.hasOwnProperty.call(jobUnread, id)) {
            jobUnread[id] = 0;
        }
    }
    for (const key of Object.keys(jobUnread)) {
        const id = Number(key);
        if (!validIds.has(id)) {
            delete jobUnread[key];
        }
    }
}

function updateJobsBadge() {
    const badge = document.getElementById('jobs-badge');
    if (!badge) return;
    let total = 0;
    for (const count of Object.values(jobUnread)) {
        total += Number(count || 0);
    }
    badge.textContent = total > 99 ? '99+' : String(total);
    badge.classList.toggle('hidden', total === 0);
}

function markJobRead(jobId) {
    if (jobId == null) return;
    jobUnread[jobId] = 0;
    updateJobsBadge();
}

// ---------------------------------------------------------------------------
// Job @mention autocomplete
// ---------------------------------------------------------------------------

function setupJobMentions() {
    const input = document.getElementById('jobs-conv-input-text');
    const menu = document.getElementById('job-mention-menu');
    const button = document.getElementById('job-reply-target-btn');
    if (!input || !menu || !button) return;

    input.addEventListener('input', updateJobMentionMenu);
    button.addEventListener('click', (e) => {
        if (jobRecipientOffset !== null) {
            closeJobMentionMenu();
            button.focus();
            return;
        }
        const offset = e.detail ? e.clientX - button.getBoundingClientRect().left : 0;
        openJobRecipientMenu(offset);
    });
    button.addEventListener('keydown', (e) => {
        if (e.key !== 'ArrowDown') return;
        e.preventDefault();
        openJobRecipientMenu(0);
    });
    const handlePickerKeys = (e) => {
        if (!jobMentionVisible || e.isComposing) return;
        const items = menu.children;
        if (e.key === 'ArrowUp' || e.key === 'ArrowDown') {
            e.preventDefault();
            const step = e.key === 'ArrowUp' ? -1 : 1;
            highlightJobMention(menu, (jobMentionIndex + step + items.length) % items.length);
            items[jobMentionIndex]?.scrollIntoView({ block: 'nearest' });
        } else if (e.key === 'Tab' || (e.key === 'Enter' && !e.shiftKey)) {
            e.preventDefault();
            if (items[jobMentionIndex]) chooseJobMention(items[jobMentionIndex].dataset.name);
        } else if (e.key === 'Escape') {
            e.preventDefault();
            const restoreButton = jobRecipientOffset !== null;
            closeJobMentionMenu();
            if (restoreButton) button.focus();
        }
    };
    input.addEventListener('keydown', handlePickerKeys);
    menu.addEventListener('keydown', handlePickerKeys);
    document.addEventListener('pointerdown', (e) => {
        if (!jobMentionVisible || menu.contains(e.target) || button.contains(e.target)) return;
        if (jobRecipientOffset === null && input.contains(e.target)) return;
        closeJobMentionMenu();
    });
    window.addEventListener('resize', positionJobRecipientMenu);
}

function closeJobMentionMenu() {
    const menu = document.getElementById('job-mention-menu');
    menu.classList.add('hidden');
    menu.removeAttribute('aria-activedescendant');
    document.getElementById('jobs-conv-input-text').removeAttribute('aria-activedescendant');
    document.getElementById('job-reply-target-btn').setAttribute('aria-expanded', 'false');
    jobMentionVisible = false;
    jobRecipientOffset = null;
}

function positionJobRecipientMenu() {
    if (jobRecipientOffset === null) return;
    const menu = document.getElementById('job-mention-menu');
    const composer = menu.parentElement.getBoundingClientRect();
    const panel = document.getElementById('jobs-panel').getBoundingClientRect();
    const button = document.getElementById('job-reply-target-btn').getBoundingClientRect();
    const gap = 4;
    const inset = 8;
    const width = Math.min(224, composer.width - 2 * inset);
    const anchor = button.left - composer.left + jobRecipientOffset;
    menu.style.width = `${width}px`;
    menu.style.left = `${Math.max(inset, Math.min(anchor, composer.width - width - inset))}px`;
    menu.style.bottom = `${composer.bottom - button.top + gap}px`;
    menu.style.maxHeight = `${Math.max(0, Math.min(200, button.top - panel.top - gap - inset))}px`;
}

function highlightJobMention(menu, index) {
    jobMentionIndex = index;
    Array.from(menu.children).forEach((row, i) => {
        row.classList.toggle('active', i === index);
    });
    const control = jobRecipientOffset === null ? document.getElementById('jobs-conv-input-text') : menu;
    control.setAttribute('aria-activedescendant', menu.children[index].id);
}

function openJobRecipientMenu(offset) {
    const options = getJobRecipientOptions();
    if (!activeJobId || options.length === 0) return;
    const selected = _normalizeJobRecipient(jobReplyTargets[activeJobId], options);
    options.sort((a, b) => a.label.localeCompare(b.label, undefined, { sensitivity: 'base', numeric: true })
        || a.name.localeCompare(b.name));
    closeJobMentionMenu();
    jobRecipientOffset = offset;
    showJobMentionMenu(options, 0, selected);
    positionJobRecipientMenu();
    document.getElementById('job-reply-target-btn').setAttribute('aria-expanded', 'true');
    document.getElementById('job-mention-menu').focus({ preventScroll: true });
}

function updateJobMentionMenu() {
    const input = document.getElementById('jobs-conv-input-text');
    const text = input.value;
    const cursor = input.selectionStart;

    let atPos = -1;
    for (let i = cursor - 1; i >= 0; i--) {
        if (text[i] === '@') { atPos = i; break; }
        if (!/[\w\-\s]/.test(text[i])) break;
        if (cursor - i > 30) break;
    }

    if (atPos < 0 || (atPos > 0 && /\w/.test(text[atPos - 1]))) {
        closeJobMentionMenu();
        return;
    }

    const query = text.slice(atPos + 1, cursor).toLowerCase();
    jobMentionStart = atPos;

    const candidates = window.getMentionCandidates();
    const matches = candidates.filter(c =>
        c.name.toLowerCase().includes(query) || c.label.toLowerCase().includes(query)
    );

    if (matches.length === 0) {
        closeJobMentionMenu();
        return;
    }

    const index = jobRecipientOffset === null ? Math.min(jobMentionIndex, matches.length - 1) : 0;
    if (jobRecipientOffset !== null) closeJobMentionMenu();
    showJobMentionMenu(matches, index);
}

function showJobMentionMenu(matches, index, selectedRecipient) {
    const menu = document.getElementById('job-mention-menu');
    const isRecipient = jobRecipientOffset !== null;
    menu.classList.toggle('job-recipient-menu', isRecipient);
    menu.setAttribute('aria-label', isRecipient ? 'Reply target' : 'Agents');
    if (!isRecipient) {
        for (const property of ['width', 'left', 'bottom', 'max-height']) menu.style.removeProperty(property);
    }
    menu.replaceChildren();
    matches.forEach((item, i) => {
        const row = document.createElement('div');
        row.id = `job-mention-option-${i}`;
        row.className = 'mention-item';
        row.setAttribute('role', 'option');
        const selected = item.name === selectedRecipient;
        row.setAttribute('aria-selected', String(selected));
        row.dataset.name = item.name;
        const dot = document.createElement('span');
        dot.className = 'mention-dot';
        dot.style.background = item.color || 'var(--accent)';
        const label = document.createElement('span');
        label.className = 'mention-name';
        label.textContent = item.label;
        row.append(dot, label);
        if (selected) {
            const check = document.createElement('span');
            check.className = 'job-recipient-check';
            check.setAttribute('aria-hidden', 'true');
            check.textContent = '✓';
            row.appendChild(check);
        }
        row.addEventListener('mousedown', (e) => e.preventDefault());
        row.addEventListener('click', () => chooseJobMention(item.name));
        row.addEventListener('mouseenter', () => highlightJobMention(menu, i));
        menu.appendChild(row);
    });

    highlightJobMention(menu, index);
    menu.classList.remove('hidden');
    jobMentionVisible = true;
}

function chooseJobMention(name) {
    if (jobRecipientOffset === null) {
        selectJobMention(name);
        return;
    }
    const recipient = _normalizeJobRecipient(name);
    if (activeJobId && recipient) {
        jobReplyTargets[activeJobId] = recipient;
        updateJobReplyTargetUI();
    }
    closeJobMentionMenu();
    document.getElementById('jobs-conv-input-text').focus();
}

function selectJobMention(name) {
    const input = document.getElementById('jobs-conv-input-text');
    window._lastMentionedAgent = name; // track for future job creation
    const text = input.value;
    const cursor = input.selectionStart;
    const before = text.slice(0, jobMentionStart);
    const after = text.slice(cursor);
    const mention = `@${name} `;
    input.value = before + mention + after;
    const newPos = jobMentionStart + mention.length;
    input.setSelectionRange(newPos, newPos);
    input.focus();
    closeJobMentionMenu();
}

// ---------------------------------------------------------------------------
// Hub subscriptions -- handle job WebSocket events
// ---------------------------------------------------------------------------

Hub.on('jobs', function (event) {
    jobsData = event.data || [];
    syncJobUnreadCache();
    updateJobsBadge();
    renderJobsList();
    // Unhide breadcrumbs that were hidden because jobsData was empty during replay
    _unhideResolvedBreadcrumbs();
});

Hub.on('job', function (event) {
    handleJobEvent(event.action, event.data);
});

for (const type of ['message', 'message_update']) {
    Hub.on(type, event => {
        const job = jobsData.find(row => row.id === activeJobId);
        if (!jobUsesOrchHistory(job)) return;
        const metadata = (event.data || event.message)?.metadata || {};
        const binding = metadata.orch_workflow || metadata.orch_gate || metadata.job_reopen;
        if (binding?.job_uid === job.uid) void refreshOrchJobHistory(job);
    });
}

// ---------------------------------------------------------------------------
// Init
// ---------------------------------------------------------------------------

function _jobsInit() {
    setupJobsInput();
    setupJobMentions();
    window.addEventListener('hashchange', openJobEventLink);
    document.addEventListener('click', event => {
        const href = event.target.closest('a')?.getAttribute('href');
        if (href?.startsWith('#job/') && href === window.location.hash && event.button === 0
                && !event.ctrlKey && !event.metaKey && !event.shiftKey && !event.altKey) {
            event.preventDefault();
            void openJobEventLink();
        }
    });
    void openJobEventLink();
}

async function openJobEventLink() {
    const hash = window.location.hash;
    const route = /^#job\/([a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12})\/event\/([^/]{1,160})$/.exec(hash);
    if (!hash.startsWith('#job/')) return;
    try {
        if (!route) throw new Error('Event unavailable');
        const [, uid, eventId] = route;
        const options = { headers: { 'X-Session-Token': window.SESSION_TOKEN } };
        const result = await fetch(`/api/jobs/${uid}/orch-events/${encodeURIComponent(eventId)}`, options);
        if (!result.ok) throw new Error('Event unavailable');
        const event = await result.json();
        if (event.job_uid !== uid || hash !== window.location.hash) return;
        let sourceUnavailable = false;
        if (event.event_id === eventId && Number.isSafeInteger(event.message_id)) {
            try {
                await window.showOrchHistoryMessage(uid, event.message_id);
                return;
            } catch {
                sourceUnavailable = true;
            }
        }
        let job = jobsData.find(row => row.uid === uid);
        if (!job) {
            const response = await fetch('/api/jobs', options);
            if (!response.ok) throw new Error('Event unavailable');
            const jobs = await response.json();
            job = jobs.find(row => row.uid === uid);
            if (job && !jobsData.some(row => row.uid === uid)) jobsData.push(job);
        }
        if (hash !== window.location.hash) return;
        if (!jobUsesOrchHistory(job)) throw new Error('Event unavailable');
        window.setSidePanelOpen('jobs', true);
        await openJobConversation(job.id, { focus: false });
        if (hash !== window.location.hash || activeJobId !== job.id) return;
        const timeline = document.getElementById('job-history-timeline');
        const row = event.event_id === eventId && [...timeline.querySelectorAll('[data-event-id]')]
            .find(element => element.dataset.eventId === eventId);
        if (row) {
            if (row.hidden) document.getElementById('job-history-details').click();
            row.focus({ preventScroll: true });
            row.scrollIntoView({ block: 'center' });
            if (sourceUnavailable) {
                document.getElementById('job-history-status').textContent = 'Source message unavailable. The event is shown in Job History.';
            }
        } else {
            document.getElementById('job-history-status').textContent = 'Event unavailable.';
        }
    } catch {
        if (hash === window.location.hash) window.showToast?.('Event unavailable.', 'error');
    }
}

// ---------------------------------------------------------------------------
// Window exports (for inline onclick handlers in generated HTML)
// ---------------------------------------------------------------------------

window.toggleJobsPanel = toggleJobsPanel;
window.showCreateJob = showCreateJob;
window.jobsBack = jobsBack;
window.toggleJobStatus = toggleJobStatus;
window.openJobConversation = openJobConversation;
window.openJobFromBreadcrumb = openJobFromBreadcrumb;
window.sendJobMessage = sendJobMessage;
window.activeJobUsesOrchHistory = activeJobUsesOrchHistory;
window.cycleJobReplyTarget = cycleJobReplyTarget;
window.startDeleteJobMessage = startDeleteJobMessage;
window.confirmDeleteJobMessage = confirmDeleteJobMessage;
window.cancelDeleteJobMessage = cancelDeleteJobMessage;
window.startJobFromMessage = startJobFromMessage;
window.acceptProposal = acceptProposal;
window.dismissProposal = dismissProposal;
window.requestChangesProposal = requestChangesProposal;
window.acceptSuggestion = acceptSuggestion;
window.dismissSuggestion = dismissSuggestion;
window.deleteJobPermanent = deleteJobPermanent;
window.showDeleteJobModal = showDeleteJobModal;
window.closeDeleteJobModal = closeDeleteJobModal;
window.submitCreateJob = submitCreateJob;
window.uploadJobImage = uploadJobImage;
window.removeJobAttachment = removeJobAttachment;
window.reorderJobWithinGroup = reorderJobWithinGroup;
window._collapseJobBreadcrumbs = _collapseJobBreadcrumbs;
window.syncJobUnreadCache = syncJobUnreadCache;
window.updateJobsBadge = updateJobsBadge;
window.renderJobsList = renderJobsList;
window.markJobRead = markJobRead;
window.handleJobEvent = handleJobEvent;

window.Jobs = { init: _jobsInit };

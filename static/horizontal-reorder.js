// Mark direct children with data-reorderable, including items used only as drop anchors.
function setupHorizontalReorder(row, { move, status, label, canDrag = () => true, previewLabel = true }) {
    const selector = '[data-reorderable]';
    const horizontalDirection = getComputedStyle(row).direction === 'rtl' ? -1 : 1;
    let drag = null;
    let frame = null;
    let suppressClick = false;

    function items() {
        return [...row.querySelectorAll(`:scope > ${selector}`)];
    }

    function reorder(item, anchor, after) {
        if (!move(item, anchor, after)) return;
        const ordered = items();
        status.textContent = `${label(item)} moved to position ${ordered.indexOf(item) + 1} of ${ordered.length}.`;
    }

    function targetAt(x, source) {
        const candidates = items().filter(pill => pill !== source);
        const before = candidates.find(pill => {
            const rect = pill.getBoundingClientRect();
            return horizontalDirection * (x - rect.left - rect.width / 2) < 0;
        });
        if (before) return { pill: before, after: false };
        return { pill: candidates.at(-1), after: true };
    }

    function startDrag() {
        const { pill, id, origin } = drag;
        row.setPointerCapture(id);
        row.classList.add('is-reordering');
        pill.classList.add('reorder-origin');
        drag.preview = document.createElement('div');
        drag.preview.className = 'reorder-preview';
        drag.preview.style.width = `${origin.width}px`;
        drag.preview.style.height = `${origin.height}px`;
        const icon = pill.querySelector(':scope > svg');
        if (icon) drag.preview.append(icon.cloneNode(true));
        if (previewLabel) drag.preview.append(label(pill));
        drag.preview.setAttribute('aria-hidden', 'true');
        drag.indicator = document.createElement('div');
        drag.indicator.className = 'reorder-drop-indicator';
        drag.indicator.setAttribute('aria-hidden', 'true');
        document.body.append(drag.preview, drag.indicator);
    }

    function updateDrag() {
        frame = null;
        const { pill, preview, indicator, x, y } = drag;
        if (!row.contains(pill)) {
            finishDrag(false);
            return;
        }
        const bounds = row.getBoundingClientRect();
        const inside = x >= bounds.left - 12 && x <= bounds.right + 12 &&
            y >= bounds.top - 12 && y <= bounds.bottom + 12;
        const edge = 24;
        if (inside) {
            const delta = x < bounds.left + edge ? -10 : x > bounds.right - edge ? 10 : 0;
            const previous = row.scrollLeft;
            if (delta) row.scrollLeft += delta;
            if (row.scrollLeft !== previous) frame = requestAnimationFrame(updateDrag);
        }

        preview.style.left = `${x - drag.grabX}px`;
        preview.style.top = `${y - drag.grabY}px`;
        const target = inside ? targetAt(x, pill) : null;
        drag.target = target;
        indicator.hidden = !target?.pill;
        if (!target?.pill) return;
        const rect = target.pill.getBoundingClientRect();
        const rightEdge = target.after === (horizontalDirection === 1);
        const left = rightEdge ? rect.right + 3 : rect.left - 3;
        indicator.style.left = `${Math.max(bounds.left + 2, Math.min(bounds.right - 2, left))}px`;
        indicator.style.top = `${bounds.top}px`;
        indicator.style.height = `${bounds.height}px`;
    }

    function finishDrag(commit) {
        if (!drag) return;
        const { pill, id, preview, indicator, target } = drag;
        drag = null;
        if (frame !== null) {
            cancelAnimationFrame(frame);
            frame = null;
        }
        if (!preview) return;
        suppressClick = true;
        if (row.hasPointerCapture(id)) row.releasePointerCapture(id);
        row.classList.remove('is-reordering');
        pill.classList.remove('reorder-origin');
        preview.remove();
        indicator.remove();
        if (!commit || !target?.pill || !row.contains(target.pill)) return;
        const ordered = items();
        const from = ordered.indexOf(pill);
        const anchor = ordered.indexOf(target.pill);
        const to = anchor + Number(target.after) - Number(from < anchor);
        if (from !== to) reorder(pill, target.pill, target.after);
    }

    row.addEventListener('pointerdown', event => {
        suppressClick = false;
        if (event.button !== 0 || !event.isPrimary || event.pointerType === 'touch') return;
        const pill = event.target.closest(selector);
        if (pill?.parentElement !== row || !canDrag(pill) || items().length < 2) return;
        const origin = pill.getBoundingClientRect();
        const { clientX: x, clientY: y } = event;
        drag = { pill, id: event.pointerId, x, y, origin, grabX: x - origin.left, grabY: y - origin.top };
    });
    window.addEventListener('pointermove', event => {
        if (drag?.id !== event.pointerId) return;
        if (!row.contains(drag.pill)) {
            finishDrag(false);
            return;
        }
        if (!drag.preview && Math.hypot(event.clientX - drag.x, event.clientY - drag.y) < 6) return;
        if (!drag.preview) startDrag();
        event.preventDefault();
        drag.x = event.clientX;
        drag.y = event.clientY;
        if (frame === null) frame = requestAnimationFrame(updateDrag);
    });
    window.addEventListener('pointerup', event => {
        if (drag?.id !== event.pointerId) return;
        if (drag.preview) {
            if (frame !== null) cancelAnimationFrame(frame);
            drag.x = event.clientX;
            drag.y = event.clientY;
            updateDrag();
        }
        finishDrag(true);
    });
    window.addEventListener('pointercancel', event => {
        if (drag?.id === event.pointerId) finishDrag(false);
    });
    row.addEventListener('lostpointercapture', event => {
        if (drag?.id === event.pointerId) finishDrag(false);
    });
    window.addEventListener('blur', () => finishDrag(false));
    window.addEventListener('keydown', event => {
        if (event.key === 'Escape' && drag?.preview) {
            event.preventDefault();
            event.stopPropagation();
            finishDrag(false);
        }
    }, true);

    function suppressDropClick(event) {
        if (!suppressClick || event.detail === 0) return;
        event.preventDefault();
        event.stopImmediatePropagation();
    }
    row.addEventListener('click', suppressDropClick, true);
    row.addEventListener('dblclick', suppressDropClick, true);
    row.addEventListener('keydown', event => {
        if (!event.altKey || !event.shiftKey || event.ctrlKey || event.metaKey) return;
        if (event.key !== 'ArrowLeft' && event.key !== 'ArrowRight') return;
        const pill = event.target.closest(selector);
        if (pill?.parentElement !== row || !canDrag(pill)) return;
        event.preventDefault();
        event.stopPropagation();
        const ordered = items();
        const after = (event.key === 'ArrowRight') === (horizontalDirection === 1);
        const anchor = ordered[ordered.indexOf(pill) + (after ? 1 : -1)];
        if (!anchor) return;
        reorder(pill, anchor, after);
        pill.focus({ preventScroll: true });
        pill.scrollIntoView({ block: 'nearest', inline: 'nearest' });
    });
}

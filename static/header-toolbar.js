(function () {
    'use strict';

    const toolbar = document.getElementById('header-toolbar');

    function apply(order) {
        const buttons = new Map(Array.from(toolbar.children, button => [button.id, button]));
        if (!Array.isArray(order) || order.length !== buttons.size
            || new Set(order).size !== buttons.size || !order.every(id => buttons.has(id))) return;

        const focused = document.activeElement;
        for (const [index, id] of order.entries()) {
            const button = buttons.get(id);
            const current = toolbar.children[index];
            if (button !== current) toolbar.insertBefore(button, current);
        }
        if (toolbar.contains(focused) && document.activeElement !== focused) {
            focused.focus({ preventScroll: true });
        }
    }

    function setup(save) {
        const status = document.getElementById('header-toolbar-order-status');
        for (const button of toolbar.children) {
            button.setAttribute('aria-keyshortcuts', 'Alt+Shift+ArrowLeft Alt+Shift+ArrowRight');
        }
        window.setupHorizontalReorder(toolbar, {
            label: button => button.title,
            previewLabel: false,
            status,
            move(button, anchor, after) {
                const buttons = Array.from(toolbar.children);
                const from = buttons.indexOf(button);
                if (from < 0 || button === anchor || !buttons.includes(anchor)) return false;
                buttons.splice(from, 1);
                const to = buttons.indexOf(anchor) + Number(after);
                if (from === to) return false;
                buttons.splice(to, 0, button);
                const order = buttons.map(item => item.id);
                apply(order);
                save({ toolbar_order: order });
                return true;
            },
        });
    }

    window.HeaderToolbar = { apply, setup };
})();

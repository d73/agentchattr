// Run with: node --test tests/browser/markdown_links.cjs
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const source = fs.readFileSync(path.join(__dirname, '../../static/chat.js'), 'utf8');
const start = source.indexOf('const SAFE_LINK_SCHEMES');
const end = source.indexOf('function renderMarkdown(text)');
assert.ok(start >= 0 && end > start, 'link sanitizer block not found in chat.js');

// Minimal stand-in for the textarea entity-decoding trick used by isSafeLinkHref.
function decodeEntities(value) {
    return String(value)
        .replace(/&#x([0-9a-f]+);?/gi, (_, h) => String.fromCharCode(parseInt(h, 16)))
        .replace(/&#(\d+);?/g, (_, d) => String.fromCharCode(parseInt(d, 10)))
        .replace(/&colon;/gi, ':')
        .replace(/&amp;/g, '&');
}
function makeContext() {
    const ctx = vm.createContext({
        document: {
            createElement: () => {
                let raw = '';
                return {
                    set innerHTML(v) { raw = v; },
                    get value() { return decodeEntities(raw); },
                };
            },
        },
    });
    vm.runInContext(source.slice(start, end), ctx);
    return ctx;
}

const blocked = (ctx, html) => ctx.sanitizeLinkHrefs(html).includes('data-blocked-href');

test('dangerous schemes lose their href, including obfuscated forms', () => {
    const ctx = makeContext();
    for (const href of [
        'javascript:alert(1)',
        'JaVaScRiPt:alert(1)',
        ' javascript:alert(1)',
        'java&#x09;script:alert(1)',
        '&#106;avascript:alert(1)',
        'jav\tascript:alert(1)',
        'data:text/html;base64,AAAA',
        'vbscript:msgbox(1)',
    ]) {
        assert.equal(blocked(ctx, `<a href="${href}">x</a>`), true, href);
        assert.equal(ctx.sanitizeLinkHrefs(`<a href="${href}">x</a>`).includes('href="'), false, href);
    }
});

test('ordinary links are left untouched', () => {
    const ctx = makeContext();
    for (const href of [
        'https://example.com/a?b=1&amp;c=2',
        'http://example.com',
        'mailto:someone@example.com',
        'tel:+15551234567',
        '/relative/path',
        '#anchor',
        'page.html',
    ]) {
        const html = `<a href="${href}" title="t">x</a>`;
        assert.equal(ctx.sanitizeLinkHrefs(html), html, href);
    }
});

test('text outside anchors and anchors without href are not rewritten', () => {
    const ctx = makeContext();
    const html = '<p>javascript:alert(1) <a name="x">y</a> <code>&lt;a href="javascript:x"&gt;</code></p>';
    assert.equal(ctx.sanitizeLinkHrefs(html), html);
});

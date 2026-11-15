// Run with: node --test tests/browser/outbound_links.cjs
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const source = fs.readFileSync(path.join(__dirname, '../../static/chat.js'), 'utf8');
const start = source.indexOf('function linkifyUrls(');
const end = source.indexOf('let serverPlatform', start);
assert.ok(start > 0 && end > start, 'linkifyUrls not found');
const ctx = vm.createContext({});
vm.runInContext(source.slice(start, end), ctx);
const link = html => ctx.linkifyUrls(html);
const NEW_TAB = 'target="_blank" rel="noopener noreferrer"';

test('http, https, uppercase and protocol-relative links open in a new tab', () => {
    for (const href of ['http://a.com/x', 'https://a.com/x', 'HTTPS://A.COM/x', '//a.com/docs']) {
        assert.match(link(`<a href="${href}">Site</a>`), new RegExp(NEW_TAB), href);
    }
});

test('existing target/rel are replaced, not duplicated', () => {
    const out = link('<a href="https://a.com" target="_self" rel=nofollow>x</a>');
    assert.equal(out.match(/target=/g).length, 1);
    assert.equal(out.match(/rel=/g).length, 1);
    assert.match(out, new RegExp(NEW_TAB));
});

test('local, hash and mailto links are left alone', () => {
    for (const href of ['/api/x', '#top', 'mailto:a@b.com', 'foo.html']) {
        const html = `<a href="${href}">x</a>`;
        assert.equal(link(html), html, href);
    }
});

test('bare URLs are still auto-linked', () => {
    assert.match(link('see https://a.com/x now'), /<a href="https:\/\/a\.com\/x" target="_blank" rel="noopener">/);
});

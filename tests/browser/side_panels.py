
"""Verify the shared right-side panels in headless Chromium."""

import argparse
import json
import os
import re
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread

from playwright.sync_api import expect, sync_playwright


PANELS = {
    "jobs": "jobs-panel",
    "schedules": "schedules-panel",
    "rules": "rules-panel",
    "pins": "pins-panel",
    "settings": "settings-bar",
}


def mock_chat_routes(page, connect):
    page.route(
        "https://cdn.jsdelivr.net/**",
        lambda route: route.fulfill(body="window.marked = {setOptions() {}, parse: text => text};"),
    )
    page.route_web_socket(re.compile(r"/ws\?"), connect)
    page.route("**/api/**", lambda route: route.fulfill(
        json=[] if route.request.url.endswith(("/messages", "/roles")) else {},
    ))


def check_panels(page, url, screenshots):
    errors = []
    sockets = []
    socket_frames = []

    def route_socket(socket):
        sockets.append(socket)
        socket.on_message(lambda frame: socket_frames.append(json.loads(frame)))

    page.on("pageerror", lambda error: errors.append(str(error)))
    mock_chat_routes(page, route_socket)
    page.goto(url)
    page.evaluate("""() => {
        agentConfig = {codex: {label: 'Codex', color: '#60a5fa'}};
        buildSoundSettings();
        Hub.emit('jobs', {data: [{id: 42, title: 'Review', status: 'open', messages: []}]});
    }""")

    for name, panel_id in PANELS.items():
        toggle = page.locator(f"#{name}-toggle")
        panel = page.locator(f"#{panel_id}")
        toggle.click()
        expect(toggle).to_have_attribute("aria-expanded", "true")
        expect(panel).to_have_css("margin-right", "0px")
        expect(panel).to_have_css("width", "340px")
        expect(panel).to_have_css("min-width", "260px")
        expect(panel).to_have_css("max-width", "640px")
        expect(panel).to_have_css("transition-property", "margin-right, opacity")
        expect(panel).to_have_css("transition-duration", "0.2s, 0.16s")
        expect(panel).to_have_css("transition-timing-function", "cubic-bezier(0.2, 0, 0, 1), ease")
        assert not panel.evaluate("panel => panel.inert")
        for other_name, other_id in PANELS.items():
            if other_name != name:
                closed = page.locator(f"#{other_id}")
                expect(page.locator(f"#{other_name}-toggle")).to_have_attribute("aria-expanded", "false")
                expect(closed).to_have_css("display", "flex")
                assert closed.evaluate("panel => panel.inert")

        header = panel.locator(".side-panel-header:visible")
        expect(header).to_have_css("height", "44px")
        expect(header.locator(".side-panel-title")).to_have_css("font-size", "13px")
        expect(header.locator(".side-panel-title")).to_have_css("font-weight", "600")
        for button in header.get_by_role("button").all():
            expect(button).to_have_css("width", "26px")
            expect(button).to_have_css("height", "26px")
            assert button.get_attribute("title") == button.get_attribute("aria-label")
            assert button.locator("svg").count() == 1

        grip = panel.get_by_role("separator")
        grip.press("ArrowLeft")
        expect(panel).to_have_css("width", "356px")
        grip.press("Home")
        expect(panel).to_have_css("width", "260px")
        grip.press("End")
        expect(panel).to_have_css("width", "640px")
        grip.press("ArrowRight")
        expect(grip).to_have_attribute("aria-valuenow", "624")
        panel.evaluate("panel => panel.style.removeProperty('--panel-size')")

        if screenshots and name != "settings":
            toggle.focus()
            panel.screenshot(path=screenshots / f"{name}.png", animations="disabled")

    settings = page.locator("#settings-bar")
    profile = settings.locator('[data-section="profile"]')
    appearance = settings.locator('[data-section="appearance"]')
    alerts = settings.locator('[data-section="alerts"]')
    agents = settings.locator('[data-section="agents"]')
    sections = (profile, appearance, alerts, agents)
    assert all(not section.evaluate("section => section.open") for section in sections)
    for section in sections:
        assert "▾" in section.locator("summary").evaluate("el => getComputedStyle(el, '::after').content")
        section.locator("summary").press("Enter")
        assert section.evaluate("section => section.open")
        assert all(not other.evaluate("section => section.open") for other in sections if other is not section)
        assert "▴" in section.locator("summary").evaluate("el => getComputedStyle(el, '::after').content")
        expect(section.locator("summary")).to_have_css("border-inline-start-width", "1px")
        grip = settings.get_by_role("separator").bounding_box()
        assert section.locator("summary").bounding_box()["x"] >= grip["x"] + grip["width"]

    expect(settings.get_by_role("button", name="Export history")).to_be_visible()
    expect(settings.get_by_role("button", name="Import history")).to_be_visible()
    alerts.locator("summary").click()
    expect(settings.get_by_role("group", name="Per-agent sounds").get_by_label("Codex sound")).to_be_visible()
    page.evaluate("""() => {
        Object.defineProperty(Notification, 'permission', { value: 'denied', configurable: true });
        MentionAlerts.render();
    }""")
    mention_alerts = alerts.get_by_role("combobox", name="Mention alerts")
    expect(mention_alerts).to_be_disabled()
    expect(mention_alerts).to_have_value("off")
    expect(alerts.locator("#mention-alerts-status")).to_be_empty()
    expect(alerts.locator("#mention-alerts-field")).to_have_attribute("title", "Permission blocked in browser settings")
    attention_sound = alerts.get_by_role("combobox", name="Needs attention sound")
    assert attention_sound.locator("option").all_text_contents()[:2] == ["Use Default", "Use agent's sound"]
    expect(attention_sound).to_have_value("")
    page.evaluate("""() => {
        soundPrefs = {default: 'gentle-pop', codex: 'warm-bell'};
        window.soundPreviews = [];
        playSoundFile = name => window.soundPreviews.push(name);
    }""")
    attention_sound.select_option("default")
    assert page.evaluate("""() => {
        playAttentionSound('Codex');
        soundPrefs.default = 'none';
        playAttentionSound('Codex');
        return window.soundPreviews;
    }""") == ["gentle-pop", "gentle-pop"]
    attention_sound.select_option("")
    assert page.evaluate("""() => {
        playAttentionSound('Codex');
        return window.soundPreviews.at(-1);
    }""") == "warm-bell"
    profile.locator("summary").click()
    expect(settings.get_by_role("button", name="Reset color")).to_be_visible()
    appearance.locator("summary").click()
    font_size = appearance.get_by_role("spinbutton", name="Font Size")
    expect(font_size).to_have_value("14")
    assert font_size.evaluate("el => el.closest('.settings-field').previousElementSibling.querySelector('label').textContent") == "Font"
    assert sockets

    font = appearance.get_by_role("combobox", name="Font")
    families = {
        "open-sans": ("Open Sans", "Open Sans"),
        "roboto": ("Roboto", "Roboto"),
        "helvetica": ("Helvetica", "Helvetica Neue"),
        "montserrat": ("Montserrat", "Montserrat"),
        "avenir": ("Avenir", "Avenir Next"),
        "mono": ("Monospace", "Cascadia Code"),
    }
    assert font.locator("option").all_text_contents() == [label for label, _ in families.values()]
    expect(font).to_have_value("open-sans")
    sockets[-1].send(json.dumps({"type": "settings", "data": {"font": "sans"}}))
    expect(page.locator("body")).to_have_attribute("data-chat-font", "open-sans")
    for value, (_, family) in families.items():
        font.select_option(value)
        expect(page.locator("body")).to_have_attribute("data-chat-font", value)
        assert page.locator("#input").evaluate(
            "(el, family) => getComputedStyle(el).fontFamily.split(',')[0].replace(/[\"']/g, '') === family",
            family,
        )
        if value in {"open-sans", "roboto", "montserrat"}:
            for style in ("normal", "italic"):
                assert page.evaluate("""async ([family, style]) => {
                    const faces = await document.fonts.load(style + ' 600 14px "' + family + '"');
                    return faces.some(face => face.status === 'loaded');
                }""", [family, style])
    font_updates = [frame for frame in socket_frames if "font" in frame.get("data", {})]
    assert [frame["data"] for frame in font_updates] == [{"font": key} for key in families]

    sockets[-1].send(json.dumps({
        "type": "settings", "data": {"font": "sans", "font_size": 14},
        "typography_request_id": font_updates[0]["typography_request_id"],
    }))
    expect(page.locator("body")).to_have_attribute("data-chat-font", "mono")
    sockets[-1].send(json.dumps({
        "type": "settings", "data": {"font": "mono", "font_size": 14},
        "typography_request_id": font_updates[-1]["typography_request_id"],
    }))
    sockets[-1].send(json.dumps({"type": "settings", "data": {"font": "unknown"}}))
    expect(font).to_have_value("mono")

    appearance.get_by_role("button", name="Increase font size").click()
    expect(font_size).to_have_value("15")
    expect(page.locator("#input")).to_have_css("font-size", "15px")
    font_size.fill("20")
    expect(page.locator("#input")).to_have_css("font-size", "20px")
    expect(font_size).to_be_focused()
    size_updates = [frame for frame in socket_frames if "font_size" in frame.get("data", {})]
    assert [frame["data"] for frame in size_updates] == [{"font_size": 15}, {"font_size": 20}]

    for update in size_updates:
        sockets[-1].send(json.dumps({
            "type": "settings", "data": {"font": "mono", **update["data"]},
            "typography_request_id": update["typography_request_id"],
        }))
        expect(page.locator("#input")).to_have_css("font-size", "20px")

    font_size.fill("99")
    font_size.press("Enter")
    expect(font_size).to_have_value("20")
    assert sum("font_size" in frame.get("data", {}) for frame in socket_frames) == 2

    font.select_option("avenir")
    attempted = next(frame for frame in reversed(socket_frames) if frame.get("data") == {"font": "avenir"})
    page.evaluate("window.typographyTestSocket = ws")
    sockets[-1].close()
    page.wait_for_function("ws !== window.typographyTestSocket && ws.readyState === WebSocket.OPEN")
    resent = next(frame for frame in reversed(socket_frames) if frame.get("data") == {"font": "avenir"})
    assert attempted["typography_request_id"] != resent["typography_request_id"]
    sockets[-1].send(json.dumps({
        "type": "settings", "data": {"font": "avenir"},
        "typography_request_id": resent["typography_request_id"],
    }))
    expect(font).to_have_value("avenir")
    if screenshots:
        page.locator("#settings-toggle").focus()
        settings.screenshot(path=screenshots / "settings.png", animations="disabled")

    page.get_by_role("button", name="Close settings").click()
    page.locator("#settings-toggle").click()
    expect(profile.locator("summary")).to_be_focused()
    assert all(not section.evaluate("section => section.open") for section in sections)
    page.evaluate("localStorage.setItem('agentchattr-settings-open-section', 'appearance')")
    page.reload()
    sockets[-1].send(json.dumps({"type": "settings", "data": {"font": "avenir"}}))
    expect(page.locator("body")).to_have_attribute("data-chat-font", "avenir")
    page.evaluate("Hub.emit('jobs', {data: [{id: 42, title: 'Review', status: 'open', messages: []}]})")
    page.locator("#settings-toggle").click()
    expect(profile.locator("summary")).to_be_focused()
    assert all(not section.evaluate("section => section.open") for section in sections)

    page.evaluate("openJobFromBreadcrumb(42)")
    expect(page.locator("#jobs-conv-title")).to_have_text("Review")
    expect(page.locator("#settings-toggle")).to_have_attribute("aria-expanded", "false")
    page.get_by_role("button", name="Close Jobs").click()
    expect(page.locator("#jobs-toggle")).to_be_focused()

    page.locator("#rules-toggle").click()
    remind = page.get_by_role("button", name="Remind agents")
    remind.click()
    expect(page.locator("#rules-remind-status")).to_have_text("Reminder queued for the next trigger.")
    assert remind.locator("svg").count() == 1
    page.get_by_role("button", name="Close Rules").click()
    expect(page.locator("#rules-toggle")).to_be_focused()

    page.emulate_media(reduced_motion="reduce")
    for name, panel_id in PANELS.items():
        page.locator(f"#{name}-toggle").click()
        expect(page.locator(f"#{panel_id}")).to_have_css("transition-property", "none")
        expect(page.locator(f"#{panel_id}")).to_have_css("margin-right", "0px")
        page.locator(f"#{panel_id} .side-panel-close:visible").click()
        expect(page.locator(f"#{name}-toggle")).to_be_focused()

    # The mobile overlay uses the same transition and stays inside the viewport.
    page.set_viewport_size({"width": 390, "height": 760})
    page.emulate_media(reduced_motion="no-preference")
    page.locator("#settings-toggle").click()
    expect(settings).to_have_css("margin-right", "0px")
    box = settings.bounding_box()
    assert 0 <= box["x"] <= box["x"] + box["width"] <= 390
    assert page.evaluate("scrollX === 0")
    assert page.locator("#content-area").evaluate("el => { el.scrollLeft = 50; return el.scrollLeft === 0; }")
    bounds = settings.get_by_role("separator").bounding_box()
    x, y = bounds["x"] + 3, bounds["y"] + bounds["height"] / 2
    page.mouse.move(x, y)
    page.mouse.down()
    page.mouse.move(x + 50, y, steps=5)
    page.mouse.up()
    expect(settings).to_have_css("width", "290px")
    assert not errors, errors


def assert_theme_contrast(page):
    ratios = page.evaluate("""() => {
        const canvas = document.createElement('canvas');
        const ctx = canvas.getContext('2d');
        function luminance() {
            const rgb = Array.from(ctx.getImageData(0, 0, 1, 1).data).slice(0, 3);
            const [r, g, b] = rgb.map(value => {
                const s = value / 255;
                return s <= 0.04045 ? s / 12.92 : ((s + 0.055) / 1.055) ** 2.4;
            });
            return 0.2126 * r + 0.7152 * g + 0.0722 * b;
        }
        return Object.fromEntries(['body', '#input', '#send', '.settings-section[open] summary',
                                   '.channel-sidebar-row.active', '.theme-palette-card'].map(selector => {
            const element = document.querySelector(selector);
            const backgrounds = [];
            for (let current = element; current; current = current.parentElement) {
                backgrounds.push(getComputedStyle(current).backgroundColor);
            }
            ctx.clearRect(0, 0, 1, 1);
            for (const background of backgrounds.reverse()) {
                ctx.fillStyle = background;
                ctx.fillRect(0, 0, 1, 1);
            }
            const background = luminance();
            ctx.fillStyle = getComputedStyle(element).color;
            ctx.fillRect(0, 0, 1, 1);
            const foreground = luminance();
            return [selector, (Math.max(foreground, background) + 0.05) / (Math.min(foreground, background) + 0.05)];
        }));
    }""")
    assert all(ratio >= 4.5 for ratio in ratios.values()), ratios


def check_themes(page, url, screenshots):
    errors = []
    sockets = []
    updates = []
    saved = {"font": "mono", "font_size": 18, "channels": ["general"],
             "ui_prefs": {"sidebar_mode": "sidebar"}}
    defer_settings = False
    page.on("pageerror", lambda error: errors.append(str(error)))

    def connect(socket):
        sockets.append(socket)

        def capture(frame):
            update = json.loads(frame)
            if update.get("typography_request_id"):
                updates.append(update)

        socket.on_message(capture)
        if not defer_settings:
            socket.send(json.dumps({"type": "settings", "data": saved}))
        socket.send(json.dumps({"type": "history_complete", "has_more": False}))

    def acknowledge(update):
        saved.update(update["data"])
        sockets[-1].send(json.dumps({
            "type": "settings", "data": saved,
            "typography_request_id": update["typography_request_id"],
        }))

    mock_chat_routes(page, connect)
    page.emulate_media(color_scheme="light")
    page.goto(url)
    settings = page.locator("#settings-bar")
    appearance = settings.locator('[data-section="appearance"]')
    mode = appearance.get_by_role("radiogroup", name="Mode")
    palette = appearance.get_by_role("radiogroup", name="Palette")
    root = page.locator("html")
    body = page.locator("body")
    page.locator("#settings-toggle").click()
    appearance.locator("summary").click()
    expect(page.locator(".channel-sidebar-row.active")).to_be_visible()
    expect(mode.get_by_role("radio", name="Dark")).to_be_checked()
    expect(palette.get_by_role("radio", name="Midnight")).to_be_checked()
    expect(body).to_have_css("background-color", "rgb(16, 16, 24)")
    expect(page.locator("body")).to_have_attribute("data-chat-font", "mono")
    expect(page.locator("#input")).to_have_css("font-size", "18px")
    page.locator("#input").fill("Draft to verify the active Send button")

    for label in ("Dark", "Light", "Auto"):
        control = mode.get_by_role("radio", name=label, exact=True)
        assert control.locator("..").get_attribute("title")
        assert control.locator("..").locator("svg").count() == 1

    mode.get_by_role("radio", name="Dark").press("ArrowRight")
    light = mode.get_by_role("radio", name="Light")
    expect(light).to_be_focused()
    expect(light).to_be_checked()
    expect(light.locator("..").locator("span")).to_have_css("outline-style", "solid")
    palette.get_by_role("radio", name="Paper").press("ArrowRight")
    sage = palette.get_by_role("radio", name="Sage")
    expect(sage).to_be_focused()
    expect(sage).to_be_checked()
    expect(sage.locator("..").locator(".theme-palette-card")).to_have_css("outline-style", "solid")
    assert [update["data"] for update in updates] == [{"theme_mode": "light"}, {"theme_palette": "green"}]

    palettes = {
        "Dark": (
            ("indigo", "Midnight", "rgb(16, 16, 24)"),
            ("green", "Obsidian", "rgb(17, 17, 19)"),
            ("blue", "Nordic", "rgb(19, 25, 34)"),
            ("amber", "Ember", "rgb(23, 19, 18)"),
        ),
        "Light": (
            ("indigo", "Paper", "rgb(248, 249, 252)"),
            ("green", "Sage", "rgb(241, 246, 242)"),
            ("blue", "Frost", "rgb(240, 245, 250)"),
            ("amber", "Sand", "rgb(246, 241, 231)"),
        ),
    }
    for mode_name, choices in palettes.items():
        mode.get_by_role("radio", name=mode_name).locator("..").click()
        assert palette.locator('input:checked').input_value() == ("green" if mode_name == "Dark" else "amber")
        for family, label, bg in choices:
            choice = palette.get_by_role("radio", name=label)
            choice.locator("..").click()
            expect(choice).to_be_checked()
            expect(root).to_have_attribute("data-palette", family)
            expect(body).to_have_css("background-color", bg)
            page.wait_for_function("""() => {
                const section = getComputedStyle(document.querySelector('.settings-section[open] summary'));
                const channel = getComputedStyle(document.querySelector('.channel-sidebar-row.active'));
                return ['backgroundColor', 'borderColor', 'borderRadius', 'color']
                    .every(property => section[property] === channel[property]);
            }""")
            expect(settings.locator(".settings-section:not([open]) summary").first).to_have_css(
                "background-color", "rgba(0, 0, 0, 0)",
            )
            assert_theme_contrast(page)
        if screenshots:
            settings.screenshot(path=screenshots / f"theme-{mode_name.lower()}.png", animations="disabled")

    contrast = appearance.get_by_label("Contrast", exact=True)
    contrast.select_option("high")
    expect(body).to_have_class(re.compile("high-contrast"))
    assert_theme_contrast(page)

    mode.get_by_role("radio", name="Auto").locator("..").click()
    expect(root).to_have_attribute("data-theme", "light")
    assert updates[-1]["data"] == {"theme_mode": "auto"}
    update_count = len(updates)
    page.emulate_media(color_scheme="dark")
    expect(palette.get_by_role("radio", name="Ember")).to_be_checked()
    expect(root).to_have_attribute("data-theme", "dark")
    page.emulate_media(color_scheme="light")
    expect(palette.get_by_role("radio", name="Sand")).to_be_checked()
    assert len(updates) == update_count

    page.evaluate("""() => {
        ws.addEventListener('message', event => {
            const data = JSON.parse(event.data);
            if (data.typography_request_id) window.receivedAppearanceId = data.typography_request_id;
        });
    }""")
    first, last = [update for update in updates if "theme_palette" in update["data"]][-2:]
    acknowledge(first)
    page.wait_for_function("id => window.receivedAppearanceId === id", arg=first["typography_request_id"])
    expect(palette.get_by_role("radio", name="Sand")).to_be_checked()
    acknowledge(last)
    page.wait_for_function("id => window.receivedAppearanceId === id", arg=last["typography_request_id"])
    acknowledge(updates[-1])
    assert saved["theme_mode"] == "auto" and saved["theme_palette"] == "amber"

    sockets[-1].close(code=1011)
    page.wait_for_function("ws.readyState === WebSocket.CLOSED")
    palette.get_by_role("radio", name="Sage").locator("..").click()
    expect(root).to_have_attribute("data-palette", "green")
    assert len(updates) == update_count
    with page.expect_console_message(predicate=lambda m: m.text == "WebSocket connected"):
        page.wait_for_function("ws.readyState === WebSocket.OPEN")
    expect(root).to_have_attribute("data-palette", "green")
    assert updates[-1]["data"] == {"theme_palette": "green"}
    acknowledge(updates[-1])
    page.wait_for_function("pendingAppearanceSettings.size === 0")

    defer_settings = True
    page.reload()
    expect(root).to_have_attribute("data-theme", "light")
    expect(root).to_have_attribute("data-palette", "green")
    expect(body).to_have_css("background-color", "rgb(241, 246, 242)")
    page.locator("#settings-toggle").click()
    appearance.locator("summary").click()
    expect(mode.get_by_role("radio", name="Auto")).to_be_checked()
    expect(palette.get_by_role("radio", name="Sage")).to_be_checked()

    saved.update(theme_mode="dark", theme_palette="blue")
    sockets[-1].send(json.dumps({"type": "settings", "data": saved}))
    expect(root).to_have_attribute("data-theme", "dark")
    expect(palette.get_by_role("radio", name="Nordic")).to_be_checked()
    assert page.evaluate("JSON.parse(localStorage.getItem('agentchattr-theme'))") == {
        "theme_mode": "dark", "theme_palette": "blue",
    }
    expect(page.locator("#input")).to_have_css("font-size", "18px")

    chat_script = re.compile(r"/static/chat[.]js[?]")
    page.route(chat_script, lambda route: route.fulfill(body=""))
    page.reload()
    expect(body).to_have_css("background-color", "rgb(19, 25, 34)")
    expect(root).to_have_attribute("data-palette", "blue")
    page.unroute(chat_script)

    page.evaluate("localStorage.setItem('agentchattr-theme', '{invalid')")
    page.reload()
    expect(root).to_have_attribute("data-theme", "dark")
    expect(root).to_have_attribute("data-palette", "indigo")
    assert not errors, errors


class QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, *_):
        pass


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--screenshots", type=Path)
    args = parser.parse_args()
    if args.screenshots:
        args.screenshots.mkdir(parents=True, exist_ok=True)
    root = Path(__file__).resolve().parents[2]
    server = ThreadingHTTPServer(("127.0.0.1", 0), partial(QuietHandler, directory=str(root)))
    Thread(target=server.serve_forever, daemon=True).start()
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(
                headless=True, executable_path=os.environ.get("AGENTCHATTR_TEST_BROWSER"),
            )
            page = browser.new_page(viewport={"width": 1280, "height": 1250})
            url = f"http://127.0.0.1:{server.server_port}/static/index.html"
            check_panels(page, url, args.screenshots)
            check_themes(browser.new_page(viewport={"width": 1280, "height": 900}), url, args.screenshots)
            browser.close()
        print("PASS: five panel shells, Settings behavior, and all eight palettes with Auto and reconnect")
    finally:
        server.shutdown()
        server.server_close()


if __name__ == "__main__":
    main()

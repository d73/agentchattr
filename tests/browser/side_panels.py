
"""Verify the shared right-side panels in headless Chromium."""

import argparse
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


def check_panels(page, url, screenshots):
    errors = []
    sockets = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.route(
        "https://cdn.jsdelivr.net/**",
        lambda route: route.fulfill(body="window.marked = {setOptions() {}, parse: text => text};"),
    )
    page.route_web_socket(re.compile(r"/ws\?"), lambda socket: sockets.append(socket))
    page.route("**/api/**", lambda route: route.fulfill(
        json=[] if route.request.url.endswith(("/messages", "/roles")) else {},
    ))
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
        expect(section.locator("summary")).to_have_css("background-color", "rgb(28, 30, 50)")
        expect(section.locator("summary")).to_have_css("border-inline-start-width", "3px")
        expect(section.locator("summary")).to_have_css("border-inline-start-color", "rgb(124, 138, 255)")
        expect(section.locator("summary")).to_have_css("border-bottom-width", "1px")
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
    socket_frames = []
    assert sockets
    sockets[-1].on_message(lambda frame: socket_frames.append(frame))
    appearance.get_by_role("button", name="Increase font size").click()
    page.wait_for_function("document.getElementById('setting-font-size').value === '15'")
    assert any('"font_size":15' in frame for frame in socket_frames)
    sockets[-1].send('{"type":"settings","data":{"font_size":15}}')
    expect(page.locator("#input")).to_have_css("font-size", "15px")
    font_size.fill("99")
    font_size.press("Enter")
    expect(font_size).to_have_value("15")
    if screenshots:
        page.locator("#settings-toggle").focus()
        settings.screenshot(path=screenshots / "settings.png", animations="disabled")

    page.get_by_role("button", name="Close settings").click()
    page.locator("#settings-toggle").click()
    expect(profile.locator("summary")).to_be_focused()
    assert all(not section.evaluate("section => section.open") for section in sections)
    page.evaluate("localStorage.setItem('agentchattr-settings-open-section', 'appearance')")
    page.reload()
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
            check_panels(page, f"http://127.0.0.1:{server.server_port}/static/index.html", args.screenshots)
            browser.close()
        print("PASS: five panel shells, resizing, reduced motion, and Settings behavior")
    finally:
        server.shutdown()
        server.server_close()


if __name__ == "__main__":
    main()


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
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.route(
        "https://cdn.jsdelivr.net/**",
        lambda route: route.fulfill(body="window.marked = {setOptions() {}, parse: text => text};"),
    )
    page.route_web_socket(re.compile(r"/ws\?"), lambda _: None)
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
    assert profile.evaluate("section => section.open")
    for section in (appearance, alerts, agents):
        assert not section.evaluate("section => section.open")
        assert "▾" in section.locator("summary").evaluate("el => getComputedStyle(el, '::after').content")
        section.locator("summary").press("Enter")
        assert section.evaluate("section => section.open")
        assert "▴" in section.locator("summary").evaluate("el => getComputedStyle(el, '::after').content")

    expect(settings.get_by_role("group", name="Per-agent sounds").get_by_label("Codex sound")).to_be_visible()
    expect(settings.get_by_role("button", name="Reset color")).to_be_visible()
    expect(settings.get_by_role("button", name="Export history")).to_be_visible()
    expect(settings.get_by_role("button", name="Import history")).to_be_visible()
    if screenshots:
        page.locator("#settings-toggle").focus()
        settings.screenshot(path=screenshots / "settings.png", animations="disabled")

    profile.locator("summary").click()
    page.wait_for_function("localStorage.getItem('agentchattr-settings-section-profile') === 'false'")
    page.reload()
    page.evaluate("Hub.emit('jobs', {data: [{id: 42, title: 'Review', status: 'open', messages: []}]})")
    page.locator("#settings-toggle").click()
    expect(profile.locator("summary")).to_be_focused()
    assert not profile.evaluate("section => section.open")
    for section in (appearance, alerts, agents):
        assert section.evaluate("section => section.open")

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
        print("PASS: all five panel shells, keyboard and pointer resizing, reduced motion, and Settings persistence")
    finally:
        server.shutdown()
        server.server_close()


if __name__ == "__main__":
    main()

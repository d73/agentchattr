"""Exercise the toolbar order in Chromium using a disposable AgentChattr server."""

import argparse
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory

from playwright.sync_api import expect, sync_playwright

from agent_pill_order import free_ports, heartbeat, register, server


DEFAULT_ORDER = [
    "jobs-toggle", "schedules-toggle", "rules-toggle",
    "pins-toggle", "settings-toggle", "help-btn",
]


def wait_for_order(page, order):
    page.wait_for_function(
        """order => Array.from(document.querySelectorAll('#header-toolbar > button'), button => button.id)
                    .join(' ') === order.join(' ')""",
        arg=order,
    )


def drag_before(page, source, target, *, cancel=False, screenshot=None):
    start = page.locator("#" + source).bounding_box()
    end = page.locator("#" + target).bounding_box()
    assert start and end
    x, y = start["x"] + start["width"] / 2, start["y"] + start["height"] / 2
    page.mouse.move(x, y)
    page.mouse.down()
    page.mouse.move(end["x"] + 3, y, steps=12)
    expect(page.locator("#" + source)).to_have_class("settings-btn reorder-origin")
    expect(page.locator(".reorder-preview > svg")).to_be_visible()
    assert page.locator(".reorder-preview").evaluate("el => el.childNodes.length === 1")
    expect(page.locator(".reorder-drop-indicator")).to_have_css("width", "2px")
    if screenshot:
        page.screenshot(path=screenshot)
    if cancel:
        page.keyboard.press("Escape")
    page.mouse.up()
    expect(page.locator(".reorder-preview")).to_have_count(0)
    expect(page.locator(".reorder-drop-indicator")).to_have_count(0)


def check_browser(page, directory, screenshot):
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.route(
        "https://cdn.jsdelivr.net/**",
        lambda route: route.fulfill(body="window.marked = {setOptions() {}, parse: text => text};"),
    )
    ports = free_ports()
    settings_path = directory / "data" / "settings.json"
    toolbar = page.get_by_role("group", name="Header actions")
    with server(directory, ports) as url:
        agents = [register(url, name, name.title()) for name in ("zulu", "alpha")]
        for agent in agents:
            heartbeat(url, agent)
        agent_order = [agent["identity_id"] for agent in agents]
        page.goto(url)
        page.wait_for_function("ws?.readyState === WebSocket.OPEN")
        wait_for_order(page, DEFAULT_ORDER)
        page.evaluate("order => saveAppearanceSettings({ agent_pill_order: order })", agent_order)
        page.wait_for_function("!pendingAppearanceSettings.has('agent_pill_order')")

        jobs = toolbar.get_by_role("button", name="Jobs", exact=True)
        box = jobs.bounding_box()
        assert box
        x, y = box["x"] + box["width"] / 2, box["y"] + box["height"] / 2
        page.mouse.move(x, y)
        page.mouse.down()
        page.mouse.move(x + 4, y)
        page.mouse.up()
        expect(jobs).to_have_attribute("aria-expanded", "true")
        page.get_by_role("button", name="Close Jobs").click()

        drag_before(page, "help-btn", "jobs-toggle", screenshot=screenshot)
        after_drag = ["help-btn", *DEFAULT_ORDER[:-1]]
        wait_for_order(page, after_drag)
        expect(page.locator("#help-guide")).to_have_count(0)
        expect(jobs).to_have_attribute("aria-expanded", "false")

        drag_before(page, "settings-toggle", "help-btn", cancel=True)
        wait_for_order(page, after_drag)
        expect(toolbar.get_by_role("button", name="Settings")).to_have_attribute("aria-expanded", "false")
        page.wait_for_function("!pendingAppearanceSettings.has('toolbar_order')")

        jobs.press("Alt+Shift+ArrowLeft")
        expected = ["jobs-toggle", "help-btn", *DEFAULT_ORDER[1:-1]]
        wait_for_order(page, expected)
        expect(jobs).to_be_focused()
        expect(jobs).to_have_attribute("title", "Jobs")
        expect(jobs).to_have_attribute("aria-controls", "jobs-panel")
        expect(jobs.locator("#jobs-badge")).to_have_count(1)
        expect(page.locator("#header-toolbar-order-status")).to_have_text("Jobs moved to position 1 of 6.")
        page.wait_for_function("!pendingAppearanceSettings.has('toolbar_order')")

        # Hold one outgoing update to exercise a stale snapshot and reconnect retry.
        page.evaluate("""() => {
            const send = ws.send.bind(ws);
            ws.send = message => {
                if (!JSON.parse(message).data?.toolbar_order) send(message);
            };
        }""")
        jobs.press("Alt+Shift+ArrowRight")
        wait_for_order(page, after_drag)
        page.evaluate("""order => ws.dispatchEvent(new MessageEvent('message', {
            data: JSON.stringify({type: 'settings', data: {toolbar_order: order}}),
        }))""", expected)
        wait_for_order(page, after_drag)
        page.evaluate("window.reorderTestSocket = ws; ws.close()")
        page.wait_for_function("""ws !== window.reorderTestSocket && ws.readyState === WebSocket.OPEN
                                  && !pendingAppearanceSettings.has('toolbar_order')""")
        settings = json.loads(settings_path.read_text(encoding="utf-8"))
        assert settings["toolbar_order"] == after_drag
        assert settings["agent_pill_order"] == agent_order

        page.reload()
        wait_for_order(page, after_drag)
        page.set_viewport_size({"width": 360, "height": 760})
        assert toolbar.evaluate("""el => {
            const bounds = el.getBoundingClientRect();
            return bounds.left >= 0 && bounds.right <= innerWidth;
        }""")
        settings_button = toolbar.get_by_role("button", name="Settings")
        settings_button.click()
        expect(settings_button).to_have_attribute("aria-expanded", "true")
        page.get_by_role("button", name="Close settings").click()
        page.goto("about:blank")

    # Restart the server in a fresh process using the same settings file.
    with server(directory, ports) as url:
        page.goto(url)
        wait_for_order(page, after_drag)
        assert json.loads(settings_path.read_text(encoding="utf-8"))["agent_pill_order"] == agent_order
        toolbar.get_by_role("button", name="Help").click()
        expect(page.locator("#help-guide")).to_be_visible()
        page.keyboard.press("Escape")
        expect(page.locator("#help-guide")).to_have_count(0)

    assert not errors, errors


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--work-dir", type=Path)
    parser.add_argument("--screenshot", type=Path)
    args = parser.parse_args()
    with TemporaryDirectory(prefix="toolbar-reorder-", dir=args.work_dir) as temporary, sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            headless=True, executable_path=os.environ.get("AGENTCHATTR_TEST_BROWSER"),
        )
        page = browser.new_page(viewport={"width": 1400, "height": 900})
        check_browser(page, Path(temporary), args.screenshot)
        browser.close()
    print("PASS: toolbar pointer and keyboard behavior, narrow header, reconnect, reload, and restart")


if __name__ == "__main__":
    main()

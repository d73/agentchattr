"""Exercise the Job reply target in headless Chromium with mocked network responses."""

import argparse
import os
import re
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread

from playwright.sync_api import expect, sync_playwright


def check_reply_target(page, url, screenshot):
    sent = []
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.route(
        "https://cdn.jsdelivr.net/**",
        lambda route: route.fulfill(body="window.marked = {setOptions() {}, parse: text => text};"),
    )
    page.route_web_socket(re.compile(r"/ws\?"), lambda _: None)

    def api(route):
        if route.request.method == "POST" and route.request.url.endswith("/api/jobs/42/messages"):
            sent.append(route.request.post_data_json["text"])
        route.fulfill(json=[] if route.request.url.endswith(("/messages", "/roles")) else {})

    page.route("**/api/**", api)
    page.goto(url)
    page.evaluate("""() => {
        agentConfig = {
            claude: {label: 'Claude', color: '#da7756'},
            c1: {label: 'c1', color: '#60a5fa'},
            agy: {label: 'Agy', color: '#34d399'},
            unavailable: {label: 'Unavailable', state: 'pending'}
        };
        Hub.emit('jobs', {data: [{id: 42, title: 'Review', status: 'open', messages: []}]});
    }""")
    page.locator("#jobs-toggle").click()
    page.locator("#jobs-list").get_by_text("Review", exact=True).click()

    pill = page.locator("#job-reply-target-btn")
    composer = page.locator(".job-message-box")
    draft = composer.locator("textarea")
    menu = page.locator("#job-mention-menu")
    expect(pill).to_contain_text("none")
    expect(composer.locator("#job-reply-target-btn")).to_be_visible()
    assert page.locator("#job-reply-target-clear").count() == 0
    draft.fill("Please review the changes.")
    pill_bounds = pill.bounding_box()
    draft_bounds = draft.bounding_box()
    assert abs(pill_bounds["y"] - draft_bounds["y"]) <= 4
    assert draft_bounds["x"] >= pill_bounds["x"] + pill_bounds["width"]

    offset = 30
    pill.click(position={"x": offset, "y": 9})
    expect(menu).to_be_visible()
    expect(pill).to_have_attribute("aria-expanded", "true")
    bounds = menu.bounding_box()
    assert abs(bounds["x"] - pill_bounds["x"] - offset) <= 2
    assert abs(bounds["y"] + bounds["height"] - pill_bounds["y"] + 4) <= 2
    assert bounds["width"] <= 224
    assert menu.get_by_role("option").all_text_contents() == ["Agy", "c1", "Claude", "No recipient✓"]
    menu.press("ArrowDown")
    menu.press("Enter")
    expect(pill).to_contain_text("c1")
    expect(draft).to_have_value("Please review the changes.")
    expect(draft).to_be_focused()
    assert not sent

    pill.press("ArrowDown")
    expect(menu.get_by_role("option", name="c1")).to_have_attribute("aria-selected", "true")
    if screenshot:
        panel = page.locator("#jobs-panel").bounding_box()
        top = menu.bounding_box()["y"] - 12
        page.screenshot(path=screenshot, clip={
            "x": panel["x"], "y": top, "width": panel["width"], "height": panel["y"] + panel["height"] - top,
        })
    menu.press("Escape")
    expect(menu).to_be_hidden()
    expect(pill).to_be_focused()
    pill.click()
    menu.get_by_role("option", name="No recipient").click()
    expect(pill).to_contain_text("none")
    expect(draft).to_have_value("Please review the changes.")
    pill.click()
    menu.get_by_role("option", name="Claude").click()
    draft.press("Enter")
    expect(draft).to_have_value("")
    assert sent == ["@claude Please review the changes."]

    draft.fill("Review @c")
    expect(menu.get_by_role("option", name="c1")).to_be_visible()
    menu.get_by_role("option", name="c1").click()
    expect(draft).to_have_value("Review @c1 ")
    expect(pill).to_contain_text("Claude")
    assert len(sent) == 1

    page.set_viewport_size({"width": 520, "height": 660})
    pill.click()
    panel_bounds = page.locator("#jobs-panel").bounding_box()
    bounds = menu.bounding_box()
    assert bounds["x"] >= panel_bounds["x"]
    assert bounds["x"] + bounds["width"] <= panel_bounds["x"] + panel_bounds["width"]
    draft.click()
    expect(menu).to_be_hidden()
    assert not errors, errors


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--screenshot")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[2]
    server = ThreadingHTTPServer(("127.0.0.1", 0), partial(SimpleHTTPRequestHandler, directory=str(root)))
    Thread(target=server.serve_forever, daemon=True).start()
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(
                headless=True, executable_path=os.environ.get("AGENTCHATTR_TEST_BROWSER"),
            )
            page = browser.new_page(viewport={"width": 1280, "height": 760}, reduced_motion="reduce")
            check_reply_target(page, f"http://127.0.0.1:{server.server_port}/static/index.html", args.screenshot)
            browser.close()
        print("PASS: inline Job recipient picker, keyboard, mention autocomplete and reply routing")
    finally:
        server.shutdown()
        server.server_close()


if __name__ == "__main__":
    main()

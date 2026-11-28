"""Exercise the agent-pill order in Chromium against a disposable AgentChattr server."""

import argparse
from contextlib import closing, contextmanager, ExitStack
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import sys
from tempfile import TemporaryDirectory
import time
from urllib.error import URLError
from urllib.request import Request, urlopen

from playwright.sync_api import expect, sync_playwright


ROOT = Path(__file__).resolve().parents[2]


def free_ports():
    with ExitStack() as stack:
        listeners = [stack.enter_context(closing(socket.socket())) for _ in range(3)]
        for listener in listeners:
            listener.bind(("127.0.0.1", 0))
        return [listener.getsockname()[1] for listener in listeners]


@contextmanager
def server(directory, ports):
    web, http, sse = ports
    url = f"http://127.0.0.1:{web}"
    log_path = directory / "server.log"
    with log_path.open("w", encoding="utf-8") as log:
        process = subprocess.Popen([
            sys.executable, str(ROOT / "run.py"), "--port", str(web),
            "--mcp-http-port", str(http), "--mcp-sse-port", str(sse),
            "--data-dir", str(directory / "data"), "--upload-dir", str(directory / "uploads"),
        ], cwd=ROOT, stdout=log, stderr=subprocess.STDOUT,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        try:
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline and process.poll() is None:
                try:
                    with urlopen(url, timeout=1):
                        break
                except (URLError, TimeoutError):
                    time.sleep(0.1)
            else:
                raise RuntimeError(log_path.read_text(encoding="utf-8"))
            yield url
        finally:
            if process.poll() is None:
                if os.name == "nt":
                    # The venv launcher owns a child interpreter on Windows.
                    subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                   creationflags=subprocess.CREATE_NO_WINDOW, check=False)
                else:
                    process.terminate()
            try:
                process.wait(timeout=8)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=8)


def post(url, route, payload, headers=None):
    headers = {"Content-Type": "application/json", **(headers or {})}
    with urlopen(Request(url + route, data=json.dumps(payload).encode(), headers=headers), timeout=5) as response:
        return json.load(response)


def register(url, name, label):
    return post(url, "/api/register", {"base": "codex", "name": name, "label": label})


def heartbeat(url, agent, name=None):
    post(url, f"/api/heartbeat/{name or agent['name']}", {},
         {"Authorization": f"Bearer {agent['token']}"})


def check_order(page, lower, upper=None):
    page.wait_for_function("""([lower, upper]) => {
        const ids = selector => [...document.querySelectorAll(selector)].map(pill => pill.dataset.agent);
        return JSON.stringify(ids('#mention-toggles .mention-toggle')) === JSON.stringify(lower)
            && JSON.stringify(ids('#agent-status .status-pill')) === JSON.stringify(upper);
    }""", arg=[lower, lower if upper is None else upper])
    if lower:
        expect(page.locator("#mention-toggles > :first-child")).to_have_text("@all")


def drag_before(page, source, target, screenshot=None, cancel=False):
    pill = lambda agent: page.locator(f"#mention-toggles [data-agent='{agent}']")
    start = pill(source).bounding_box()
    end = pill(target).bounding_box()
    assert start and end
    y = start["y"] + start["height"] / 2
    page.mouse.move(start["x"] + start["width"] / 2, y)
    page.mouse.down()
    page.mouse.move(end["x"] + 4, y, steps=12)
    expect(page.locator(".reorder-origin")).to_have_count(1)
    expect(page.locator(".reorder-preview")).to_be_visible()
    expect(page.locator(".reorder-drop-indicator")).to_be_visible()
    if screenshot:
        page.screenshot(path=screenshot, clip={
            "x": 0, "y": y - 60, "width": min(670, page.viewport_size["width"]),
            "height": min(126, page.viewport_size["height"] - y + 60),
        })
    if cancel:
        page.keyboard.press("Escape")
    page.mouse.up()
    expect(page.locator(".reorder-preview")).to_have_count(0)


def check_browser(page, directory, screenshot):
    errors = []
    sockets = []
    held = []
    roster = {}
    discard_save = False
    delay_ack = False

    page.on("pageerror", lambda error: errors.append(str(error)))
    page.route("https://cdn.jsdelivr.net/**", lambda route: route.fulfill(
        body="window.marked = {setOptions() {}, parse: text => text};",
    ))

    def connect(client):
        upstream = client.connect_to_server()
        sockets.append((client, upstream))
        order_requests = set()

        def send(frame):
            event = json.loads(frame)
            if "agent_pill_order" in event.get("data", {}):
                order_requests.add(event["typography_request_id"])
                if discard_save:
                    return
            upstream.send(frame)

        def receive(frame):
            nonlocal roster
            event = json.loads(frame)
            if event["type"] == "agents":
                roster = event["data"]
            request_id = event.get("typography_request_id")
            if request_id in order_requests:
                order_requests.remove(request_id)
                if delay_ack:
                    held.append(frame)
                    return
            client.send(frame)

        client.on_message(send)
        upstream.on_message(receive)

    page.route_web_socket(re.compile(r"/ws[?]"), connect)
    ports = free_ports()
    names = ["alpha", "bravo", "charlie", "delta", "echo"]
    pill = lambda name: page.locator(f"#mention-toggles [data-agent='{name}']")
    settings_path = directory / "data" / "settings.json"

    with server(directory, ports) as url:
        agents = {name: register(url, name, name.capitalize()) for name in reversed(names)}
        page.goto(url)
        check_order(page, names)
        poll = lambda condition: page.wait_for_function(condition)

        pill("alpha").click()
        pill("bravo").dblclick()
        expect(page.locator("#mention-toggles .mention-toggle.active")).to_have_count(1)
        expect(pill("bravo")).to_have_class(re.compile("active"))
        top_bravo = page.locator("#status-bravo")
        top_bravo.click()
        expect(page.locator('.pill-popover[data-agent="bravo"]')).to_be_visible()
        top_bravo.click()

        drag_before(page, "echo", "bravo", cancel=True)
        check_order(page, names)
        expect(pill("bravo")).to_have_class(re.compile("active"))
        expect(pill("echo")).not_to_have_class(re.compile("active"))

        delay_ack = True
        drag_before(page, "echo", "bravo", screenshot)
        check_order(page, ["alpha", "echo", "bravo", "charlie", "delta"])
        expect(page.locator("#mention-toggles .mention-toggle.active")).to_have_count(1)
        expect(pill("bravo")).to_have_class(re.compile("active"))
        pill("echo").press("Alt+Shift+ArrowRight")
        check_order(page, ["alpha", "bravo", "echo", "charlie", "delta"])
        expect(pill("echo")).to_be_focused()
        expect(page.locator("#agent-pill-order-status")).to_contain_text("position 3 of 5")
        deadline = time.monotonic() + 5
        while len(held) < 2 and time.monotonic() < deadline:
            page.wait_for_timeout(30)
        assert len(held) == 2
        page.evaluate("""() => {
            ws.addEventListener('message', ({data}) => {
                const frame = JSON.parse(data);
                if (frame.typography_request_id) window.orderReply = frame.typography_request_id;
            });
        }""")
        first, last = held
        sockets[-1][0].send(first)
        page.wait_for_function("id => window.orderReply === id", arg=json.loads(first)["typography_request_id"])
        check_order(page, ["alpha", "bravo", "echo", "charlie", "delta"])
        sockets[-1][0].send(last)
        poll("!pendingAppearanceSettings.has('agent_pill_order')")
        delay_ack = False

        for agent in agents.values():
            heartbeat(url, agent)
        # A pending instance is visible in the top row only, and a missing identity stays saved.
        simulated = {name: cfg for name, cfg in roster.items() if name != "bravo"}
        simulated["pending"] = {"identity_id": "0" * 32, "label": "Pending", "state": "pending"}
        sockets[-1][0].send(json.dumps({"type": "agents", "data": simulated}))
        check_order(page, ["alpha", "echo", "charlie", "delta"], ["alpha", "echo", "charlie", "delta", "pending"])
        sockets[-1][0].send(json.dumps({"type": "agents", "data": roster}))
        check_order(page, ["alpha", "bravo", "echo", "charlie", "delta"])

        token = page.evaluate("window.__SESSION_TOKEN__")
        result = post(url, "/api/label/echo", {"label": "Zulu"}, {"X-Session-Token": token})
        assert result["new_name"] == "zulu"
        agents["zulu"] = agents.pop("echo")
        agents["new"] = register(url, "new", "A New Agent")
        check_order(page, ["alpha", "bravo", "zulu", "charlie", "delta", "new"])

        # Drop a local save before closing the socket to exercise retransmission.
        discard_save = True
        pill("delta").press("Alt+Shift+ArrowLeft")
        order = ["alpha", "bravo", "zulu", "delta", "charlie", "new"]
        check_order(page, order)
        current_client, current_upstream = sockets[-1]
        current_client.close(code=1011)
        current_upstream.close(code=1011)
        discard_save = False
        poll("ws.readyState === WebSocket.OPEN && !pendingAppearanceSettings.has('agent_pill_order')")
        check_order(page, order)
        # A lost send would leave the old order on disk and after reload.
        assert json.loads(settings_path.read_text("utf-8"))["agent_pill_order"] == [
            agents[name]["identity_id"] for name in order
        ]
        page.reload()
        check_order(page, order)

        page.set_viewport_size({"width": 380, "height": 800})
        row = page.locator("#mention-toggles")
        assert row.evaluate("el => el.scrollWidth > el.clientWidth")
        row.hover()
        page.mouse.wheel(200, 0)
        poll("document.getElementById('mention-toggles').scrollLeft > 0")
        row.evaluate("el => { el.scrollLeft = 0; }")
        start = pill("alpha").bounding_box()
        bounds = row.bounding_box()
        page.mouse.move(start["x"] + start["width"] / 2, start["y"] + start["height"] / 2)
        page.mouse.down()
        page.mouse.move(bounds["x"] + bounds["width"] - 3, start["y"] + start["height"] / 2, steps=8)
        poll("""() => {
            const row = document.getElementById('mention-toggles');
            return row.scrollLeft + row.clientWidth >= row.scrollWidth - 1;
        }""")
        page.mouse.up()
        order = ["bravo", "zulu", "delta", "charlie", "new", "alpha"]
        check_order(page, order)
        poll("!pendingAppearanceSettings.has('agent_pill_order')")
        assert json.loads(settings_path.read_text("utf-8"))["agent_pill_order"] == [
            agents[name]["identity_id"] for name in order
        ]
        page.goto("about:blank")

    # Restart the actual server. Reclaim by token, returning agents in reverse order.
    with server(directory, ports) as url:
        for name in reversed(order):
            if name != "bravo":
                heartbeat(url, agents[name], name)
        page.goto(url)
        check_order(page, [name for name in order if name != "bravo"])
        heartbeat(url, agents["bravo"])
        check_order(page, order)

    assert not errors, errors


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--work-dir", type=Path)
    parser.add_argument("--screenshot", type=Path)
    args = parser.parse_args()
    with TemporaryDirectory(prefix="agent-pill-order-", dir=args.work_dir) as temporary, sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            headless=True, executable_path=os.environ.get("AGENTCHATTR_TEST_BROWSER"),
        )
        page = browser.new_page(viewport={"width": 1400, "height": 900})
        check_browser(page, Path(temporary), args.screenshot)
        browser.close()
    print("PASS: shared pill order, drag and keyboard controls, reconnect, reload, and server restart")


if __name__ == "__main__":
    main()

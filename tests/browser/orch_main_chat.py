"""Headless browser regression for Orch main-chat input and read-only Job history."""

import argparse
import json
import re
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread

from playwright.sync_api import expect, sync_playwright


def check(page, url, screenshots, plugin):
    errors = []
    sent = []
    reopened = []
    actions = []
    channel = "orch-workflow"
    moved = "renamed-workflow"

    def message(mid, text, *, sender="orch", room=channel, kind=None, **metadata):
        if kind:
            metadata["orch_workflow"] = {"job_uid": "active", "kind": kind, "revision": 2, "prompt_id": mid}
        return {
            "id": mid, "text": text, "channel": room, "type": "decision" if "choices" in metadata else "chat",
            "sender": sender, "sender_kind": "human" if sender == "Derek" else "agent",
            "time": "21:58", "metadata": metadata,
        }

    active = {
        "id": 2, "uid": "active", "title": "Push notifications for Orch", "channel": channel,
        "body": "Replace polling with a scoped push subscription.", "status": "open",
        "lifecycle": {"managed_by": "orch", "stage_id": "orch.awaiting_approval"},
        "orch_workflow_active": True, "source_msg_id": 101, "messages": [],
    }
    manual = {"id": 3, "uid": "manual", "title": "Manual Job", "channel": channel, "status": "open", "messages": []}
    legacy = {
        **active, "id": 4, "uid": "legacy", "title": "Legacy Orch Job", "orch_workflow_active": False,
    }
    closed = {
        **active, "id": 5, "uid": "closed", "title": "Completed Job", "status": "archived",
        "lifecycle": {"managed_by": "orch", "stage_id": "orch.done", "version": 7},
    }
    jobs = [active, manual, legacy, closed]
    gate = message(105, "Brief revision 2 is ready for review.", choices=["Approve", "Revise", "Cancel"],
                   orch_gate={"job_uid": "active", "gate": "brief", "revision": 2})
    question = message(91, "What should the fallback polling limit be?", room=moved, sender="agy", kind="revise")
    reply = message(92, "Use a 60-second maximum.", room=moved, sender="Derek")
    closeout = message(106, "The work and verification are complete.")
    closeout["metadata"]["orch_workflow"] = {"job_uid": "closed", "kind": "closeout", "prompt_id": 106}
    entries = [
        {"kind": "brief", "revision": 1, "choice": "Revise", "actor": "Derek",
         "time": "21:55", "text": "Initial brief", "message_id": 99, "channel": channel},
        {"kind": "reply", "revision": 1, "actor": "Derek", "time": "21:56",
         "text": reply["text"], "message_id": 92, "channel": channel},
        {"kind": "brief", "revision": 2, "time": "21:58", "text": "Added the 60-second fallback cap.",
         "message_id": 105, "channel": channel},
    ]
    chat = [
        message(100, "Brief revision 1 is ready for review.", choices=["Approve", "Revise", "Cancel"],
                resolved=True, chosen="Revise"),
        message(103, question["text"], sender="agy", kind="revise"),
        message(104, reply["text"], sender="Derek"),
        gate,
    ]

    def api(route):
        path = route.request.url.removeprefix(url).split("?")[0]
        if path == "/api/jobs":
            return route.fulfill(json=jobs)
        if path == "/api/version_check":
            return route.fulfill(json={"state": "current"})
        if path == "/api/sessions/active":
            return route.fulfill(body="null", content_type="application/json")
        if path == "/api/jobs/orch-intake":
            return route.fulfill(json={"job": active, "created": False})
        if path == "/api/jobs/active/orch-history":
            return route.fulfill(json={
                "job_uid": "active", "workflow_active": True, "channel": channel,
                "source_message_id": 101, "current_action_message_id": 105, "entries": entries,
            })
        if path.startswith("/api/jobs/active/orch-history/"):
            mid = int(path.rsplit("/", 1)[1])
            if mid == 99:
                return route.fulfill(status=404, json={"error": "missing"})
            if mid == 92:
                return route.fulfill(json={"channel": moved, "message": reply, "messages": [question, reply]})
            return route.fulfill(json={"channel": channel, "message": gate, "messages": [gate]})
        if path == "/api/jobs/closed/orch-history/106":
            return route.fulfill(json={"channel": channel, "message": closeout, "messages": [closeout]})
        if path == "/api/jobs/5/reopen-requests":
            reopened.append(route.request.post_data_json)
            return route.fulfill(json={"request": {"status": "pending"}, "card_msg_id": 107})
        if path == "/api/jobs/active/orch-gates/105/resolve":
            actions[:] = [action for action in actions if action["message_id"] != 105]
            return route.fulfill(json={"choice": "Revise"})
        if path == "/api/plugins/orch/status":
            return route.fulfill(json={
                "identity": "orch", "daemon": {"state": "online"}, "channel": channel,
                "jobs": [{"id": 2, "uid": "active", "stage": "orch.drafting", "awaiting_local": True}],
                "workflow_actions": actions,
            })
        return route.fulfill(json=[] if path.endswith(("/messages", "/roles")) else {})

    page.on("pageerror", lambda error: errors.append(str(error)))
    page.route("https://cdn.jsdelivr.net/**", lambda route: route.fulfill(
        body="window.marked={setOptions(){},parse:text=>text};",
    ))
    def websocket(ws):
        ws.on_message(lambda raw: sent.append(raw))
        ws.send(json.dumps({"type": "history", "messages": chat}))
        ws.send(json.dumps({"type": "history_complete", "has_more": {}}))

    page.route_web_socket(re.compile(r"/ws\?"), websocket)
    page.route("**/api/**", api)
    if plugin:
        page.route("**/plugins/orch/status.js", lambda route: route.fulfill(
            body=plugin.read_text(encoding="utf-8"), content_type="text/javascript",
        ))
    page.goto(url + "/static/index.html")
    page.evaluate("""([jobs, channels, plugin]) => {
        agentConfig = {agy: {label: 'Agy', color: '#60a5fa'}, orch: {label: 'Orch', color: '#4ade80'}};
        channelList = channels;
        switchChannel('orch-workflow');
        Hub.emit('jobs', {data: jobs});
        if (plugin) {
            const script = document.createElement('script');
            script.src = '/plugins/orch/status.js';
            document.head.append(script);
        }
    }""", [jobs, [channel, moved], bool(plugin)])
    expect(page.locator("#message-105")).to_be_visible()

    sidebar = page.locator("#jobs-panel")
    composer = page.locator("#input")
    page.evaluate("createOrchJob(101)")
    expect(composer).to_be_focused()
    expect(page.locator("#jobs-toggle")).to_have_attribute("aria-expanded", "false")

    page.locator("#jobs-toggle").click()
    page.locator("#jobs-list .job-card[data-id='2']").press("Enter")
    expect(sidebar.get_by_text("Read-only history")).to_be_visible()
    expect(sidebar.get_by_text("Added the 60-second fallback cap.")).to_be_visible()
    expect(sidebar.locator(".jobs-conv-input")).to_be_hidden()
    expect(sidebar.locator("#jobs-status-toggles")).to_be_hidden()
    if screenshots:
        page.screenshot(path=screenshots / "orch-main-chat-history.png", animations="disabled")
    sidebar.get_by_role("button", name="Jump to #99 in #orch-workflow").click()
    expect(sidebar.get_by_role("status").filter(has_text="no longer available")).to_be_visible()
    assert page.evaluate("window.activeChannel") == channel

    sidebar.get_by_role("button", name="Jump to #92 in #orch-workflow").press("Enter")
    expect(page.locator("#message-92 .chat-bubble")).to_be_focused()
    assert page.evaluate("window.activeChannel") == moved
    assert page.locator("#message-92").count() == 1
    page.locator("#message-91 .reply-btn").focus()
    page.locator("#message-91 .reply-btn").press("Enter")
    expect(composer).to_be_focused()
    composer.fill("Use a 60-second cap.")
    composer.press("Enter")
    expect(composer).to_have_value("")
    page.wait_for_function("!document.getElementById('reply-preview')")
    messages = [json.loads(raw) for raw in sent]
    assert any(msg.get("reply_to") == 91 and msg.get("channel") == moved
               and msg.get("text") == "Use a 60-second cap." for msg in messages)

    sidebar.get_by_role("button", name="Back to Jobs").click()
    sidebar.locator(".job-card[data-id='3']").click()
    expect(sidebar.locator(".jobs-conv-input")).to_be_visible()
    expect(sidebar.locator("#jobs-status-toggles")).to_be_visible()
    sidebar.get_by_role("button", name="Back to Jobs").click()
    sidebar.locator(".job-card[data-id='4']").click()
    expect(sidebar.locator(".jobs-conv-input")).to_be_visible()
    expect(sidebar.get_by_text("Read-only history")).to_be_hidden()

    page.evaluate("switchChannel('orch-workflow')")
    page.evaluate("msg => appendMessage(msg, {history: true})", closeout)
    page.get_by_role("button", name="Request reopen").click()
    expect(page.get_by_role("textbox", name="Reason to reopen")).to_be_focused()
    page.get_by_role("textbox", name="Reason to reopen").fill("Correct the published outcome.")
    page.locator(".orch-reopen-form").get_by_role("button", name="Request reopen").click()
    expect(page.locator(".orch-reopen-form")).to_have_count(0)
    assert reopened == [{"job_uid": "closed", "expected_version": 7, "reason": "Correct the published outcome."}]

    if plugin:
        pill = page.locator("#agent-status :is(.plugin-workflow-pill, #status-orch)")
        expect(pill).to_have_attribute("aria-label", "Orch online · No actions waiting for you")
        assert not pill.get_attribute("data-orch-attention") == "true"

        def refresh():
            page.evaluate("Hub.emit('message_update', {message: {metadata: {orch_gate: {job_uid: 'active'}}}})")

        actions[:] = [{"job_uid": "active", "job_id": 2, "title": active["title"], "channel": channel,
                       "message_id": 105, "kind": "brief", "label": "brief approval"}]
        refresh()
        expect(pill).to_have_attribute("aria-label", "Jump to Job #2 brief approval in #orch-workflow (#105)")
        pill.press("Enter")
        expect(page.locator("#message-105 .chat-bubble")).to_be_focused()
        page.get_by_role("button", name="Revise", exact=True).click()
        expect(pill).to_have_attribute("aria-label", "Orch online · No actions waiting for you")

        actions[:] = [
            {"job_uid": "active", "job_id": 2, "title": active["title"], "channel": moved,
             "message_id": 92, "kind": "reply", "label": "revision feedback"},
            {"job_uid": "closed", "job_id": 5, "title": closed["title"], "channel": channel,
             "message_id": 106, "kind": "reopen", "label": "reopen decision"},
        ]
        refresh()
        expect(pill).to_have_attribute("aria-expanded", "false")
        pill.press("Enter")
        picker = page.get_by_role("group", name="Orch requests waiting for you")
        expect(picker.get_by_role("button").first).to_be_focused()
        if screenshots:
            page.evaluate("setSidePanelOpen('jobs', false)")
            page.screenshot(path=screenshots / "orch-attention-picker.png", animations="disabled")
            page.evaluate("setSidePanelOpen('jobs', true)")
        page.keyboard.press("Escape")
        expect(picker).to_have_count(0)
        expect(pill).to_be_focused()
        pill.press("Space")
        page.mouse.click(5, 200)
        expect(picker).to_have_count(0)
        pill.press("Enter")
        picker.get_by_role("button", name=re.compile("Job #2")).click()
        expect(page.locator("#message-92 .chat-bubble")).to_be_focused()

    page.set_viewport_size({"width": 390, "height": 700})
    sidebar.get_by_role("button", name="Back to Jobs").click()
    sidebar.locator(".job-card[data-id='2']").click()
    sidebar.get_by_role("button", name="Jump to #92 in #orch-workflow").click()
    expect(page.locator("#jobs-toggle")).to_have_attribute("aria-expanded", "false")
    expect(page.locator("#message-92 .chat-bubble")).to_be_focused()
    assert not errors, errors


class QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, *_):
        pass


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--screenshots", type=Path)
    parser.add_argument("--orch-plugin", type=Path)
    parser.add_argument("--browser", type=Path)
    args = parser.parse_args()
    if args.screenshots:
        args.screenshots.mkdir(parents=True, exist_ok=True)
    root = Path(__file__).resolve().parents[2]
    with ThreadingHTTPServer(("127.0.0.1", 0), partial(QuietHandler, directory=str(root))) as server:
        Thread(target=server.serve_forever, daemon=True).start()
        try:
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(headless=True, executable_path=args.browser)
                page = browser.new_page(viewport={"width": 1280, "height": 880})
                check(page, f"http://127.0.0.1:{server.server_port}", args.screenshots, args.orch_plugin)
                browser.close()
        finally:
            server.shutdown()
    print("PASS: Orch intake focus, read-only history and precise jumps, legacy and manual Jobs, bound replies, reopen and attention")


if __name__ == "__main__":
    main()

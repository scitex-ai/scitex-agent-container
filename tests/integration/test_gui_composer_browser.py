"""Browser regressions for dashboard composer navigation; no live messages."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

pytestmark = pytest.mark.integration

playwright = pytest.importorskip("playwright.sync_api")
LEAF = Path(__file__).resolve().parents[2] / "src/scitex_agent_container/_django"
SCRIPT = LEAF / "static/scitex_agent_container/js/fleet-details.js"


@pytest.fixture
def browser_page():
    with playwright.sync_playwright() as engine:
        executable = os.environ.get("SCITEX_TEST_CHROMIUM")
        browser = engine.chromium.launch(**({"executable_path": executable} if executable else {}))
        page = browser.new_page()
        yield page
        browser.close()


def mount_composer(page, kind, outcome, action="/alpha/message"):
    requests = []
    html = f'''<div class="agents-app" data-page="fleet" data-fleet-state="ok">
      <div data-role="fleet-chat-body">
      <form method="post" action="{action}" data-composer="{kind}">
        <input name="dispatch_id" value="stable-test-dispatch">
        <textarea name="message"></textarea><button type="submit">Send</button>
      </form></div><div data-role="fleet-detail-body"></div></div>'''

    def respond(route):
        request = route.request
        if request.method == "POST":
            requests.append({"url": request.url, "accept": request.headers.get("accept", ""), "body": request.post_data})
            if outcome == "transport":
                route.abort("failed")
            elif outcome == "refusal":
                route.fulfill(status=502, json={"ok": False, "message": "Endpoint unavailable."})
            else:
                route.fulfill(json={"ok": True})
        elif "partial=1" in request.url:
            route.fulfill(content_type="text/html", body='<div data-role="chat-thread">Updated thread</div><form class="chat-composer" data-composer="dm" action="/alpha/message"><textarea name="message"></textarea><button type="submit">Send</button></form>')
        else:
            route.fulfill(content_type="text/html", body=html)

    page.route("https://dashboard.example/**", respond)
    page.goto("https://dashboard.example/")
    page.add_script_tag(path=str(SCRIPT))
    return requests


@pytest.mark.parametrize("kind", ["fleet-dm", "dm"])
@pytest.mark.parametrize("outcome", ["refusal", "transport", "success"])
def test_enter_keeps_dashboard_and_preserves_unconfirmed_draft(browser_page, kind, outcome):
    # Arrange — real shipped event handlers, intercepted disposable transport.
    page = browser_page
    requests = mount_composer(page, kind, outcome)
    area = page.locator("textarea").first
    area.fill("Disposable browser fixture")
    # Act — native keyboard input, including requestSubmit dispatch.
    area.press("Enter")
    if outcome == "success":
        page.wait_for_function("document.querySelector('textarea').value === ''")
    else:
        page.locator("[data-composer-error]:visible").wait_for()
    # Assert — one JSON-requested send and no navigation on either result.
    assert (page.url, len(requests), requests[0]["accept"], area.input_value()) == (
        "https://dashboard.example/", 1, "application/json",
        "" if outcome == "success" else "Disposable browser fixture",
    )


def test_missing_target_never_posts_current_dashboard(browser_page):
    # Arrange — transient unselected/retargeting form must fail closed.
    page = browser_page
    requests = mount_composer(page, "fleet-dm", "success", action="")
    page.locator("textarea").first.fill("Keep my draft")
    # Act
    page.locator("textarea").first.press("Enter")
    page.locator("[data-composer-error]:visible").wait_for()
    # Assert
    assert (page.url, requests, page.locator("textarea").first.input_value()) == (
        "https://dashboard.example/", [], "Keep my draft",
    )


def test_shift_enter_inserts_newline_without_sending(browser_page):
    # Arrange
    page = browser_page
    requests = mount_composer(page, "fleet-dm", "success")
    area = page.locator("textarea").first
    area.fill("Line one")
    # Act
    area.press("Shift+Enter")
    # Assert
    assert (requests, area.input_value()) == ([], "Line one\n")


def test_uncertain_send_is_not_repeated_by_enter(browser_page):
    # Arrange — the network may fail after the server accepted a dispatch.
    page = browser_page
    requests = mount_composer(page, "fleet-dm", "transport")
    area = page.locator("textarea").first
    area.fill("Keep uncertain draft")
    # Act — retrying the identical draft must not repeat an ambiguous send.
    area.press("Enter")
    page.wait_for_function("document.querySelector('form').dataset.sending === 'false'")
    area.press("Enter")
    page.wait_for_function("document.querySelector('[data-composer-error]').textContent.includes('will not be sent again')")
    # Assert
    assert (len(requests), area.input_value(), page.url) == (1, "Keep uncertain draft", "https://dashboard.example/")


def test_detail_fragment_keeps_natural_height_inside_fleet(browser_page):
    # Arrange — live details are nested inside the viewport-fitted fleet page.
    page = browser_page
    css = (LEAF / "static/scitex_agent_container/agents.css").read_text()
    page.set_content(f'<style>{css}</style><div class="agents-app" data-page="fleet" style="height:500px"><div class="agents-app agents-app--fragment" data-page="detail">Details content</div></div>')
    # Act
    geometry = page.locator('[data-page="detail"]').evaluate("e => ({display: getComputedStyle(e).display, height: e.getBoundingClientRect().height})")
    # Assert — it must not inherit the outer full-height flex column contract.
    assert geometry["display"] == "block" and geometry["height"] < 100

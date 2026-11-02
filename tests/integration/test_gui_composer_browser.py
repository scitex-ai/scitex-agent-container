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
        browser = engine.chromium.launch(
            **({"executable_path": executable} if executable else {})
        )
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
            requests.append(
                {
                    "url": request.url,
                    "accept": request.headers.get("accept", ""),
                    "body": request.post_data,
                }
            )
            if outcome == "transport":
                route.abort("failed")
            elif outcome == "refusal":
                route.fulfill(
                    status=502, json={"ok": False, "message": "Endpoint unavailable."}
                )
            else:
                route.fulfill(json={"ok": True})
        elif "partial=1" in request.url:
            route.fulfill(
                content_type="text/html",
                body='<div data-role="chat-thread">Updated thread</div><form class="chat-composer" data-composer="dm" action="/alpha/message"><textarea name="message"></textarea><button type="submit">Send</button></form>',
            )
        else:
            route.fulfill(content_type="text/html", body=html)

    page.route("https://dashboard.example/**", respond)
    page.goto("https://dashboard.example/")
    page.add_script_tag(path=str(SCRIPT))
    return requests


@pytest.mark.parametrize("kind", ["fleet-dm", "dm"])
def test_confirmed_enter_clears_draft_without_leaving_dashboard(browser_page, kind):
    # Arrange — confirmed acceptance may clear the submitted draft.
    page = browser_page
    requests = mount_composer(page, kind, "success")
    area = page.locator("textarea").first
    area.fill("Disposable browser fixture")
    # Act — native keyboard dispatch, rather than calling the handler directly.
    area.press("Enter")
    page.wait_for_function("document.querySelector('textarea').value === ''")
    # Assert
    assert (page.url, len(requests), requests[0]["accept"], area.input_value()) == (
        "https://dashboard.example/",
        1,
        "application/json",
        "",
    )


@pytest.mark.parametrize("kind", ["fleet-dm", "dm"])
@pytest.mark.parametrize("outcome", ["refusal", "transport"])
def test_unconfirmed_enter_preserves_draft_and_dashboard(browser_page, kind, outcome):
    # Arrange — neither an explicit refusal nor an uncertain transport clears text.
    page = browser_page
    requests = mount_composer(page, kind, outcome)
    area = page.locator("textarea").first
    area.fill("Disposable browser fixture")
    # Act
    area.press("Enter")
    page.locator("[data-composer-error]:visible").wait_for()
    # Assert
    assert (page.url, len(requests), requests[0]["accept"], area.input_value()) == (
        "https://dashboard.example/",
        1,
        "application/json",
        "Disposable browser fixture",
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
        "https://dashboard.example/",
        [],
        "Keep my draft",
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
    page.wait_for_function(
        "document.querySelector('[data-composer-error]').textContent.includes('will not be sent again')"
    )
    # Assert
    assert (len(requests), area.input_value(), page.url) == (
        1,
        "Keep uncertain draft",
        "https://dashboard.example/",
    )


def test_detail_fragment_keeps_natural_height_inside_fleet(browser_page):
    # Arrange — live details are nested inside the viewport-fitted fleet page.
    page = browser_page
    css = (LEAF / "static/scitex_agent_container/agents.css").read_text()
    page.set_content(
        f'<style>{css}</style><div class="agents-app" data-page="fleet" style="height:500px"><div class="agents-app agents-app--fragment" data-page="detail">Details content</div></div>'
    )
    # Act
    geometry = page.locator('[data-page="detail"]').evaluate(
        "e => ({display: getComputedStyle(e).display, height: e.getBoundingClientRect().height})"
    )
    # Assert — it must not inherit the outer full-height flex column contract.
    assert geometry["display"] == "block" and geometry["height"] < 100


def mount_admission(page, outcomes, *, kind="fleet-dm"):
    posts = []
    gets = []
    exchange = "xch_20261102T000000Z_gui_fixture_abcdef"
    html = f'''<div class="agents-app" data-page="fleet" data-fleet-state="ok">
    <table data-fleet-table><tbody>
      <tr class="agent-row" data-agent-name="alpha" data-stx-selected="true"><td>Alpha<a href="/alpha/">Inspect</a></td></tr>
      <tr class="agent-row cross" data-agent-name="beta"><td>Beta<a href="/beta/">Inspect</a></td></tr>
    </tbody></table><div data-role="fleet-chat-body"><form class="chat-composer" action="/alpha/message" data-composer="{kind}">
    <input name="dispatch_id" value="fixture-dispatch"><textarea name="message"></textarea><button type="submit">Send</button>
    </form></div><div data-role="fleet-detail-body"></div></div>'''

    def respond(route):
        request = route.request
        if request.method == "POST":
            posts.append(request.url)
            route.fulfill(
                json={
                    "ok": True,
                    "state": "accepted",
                    "exchange_id": exchange,
                    "exchange_receipt": "signed-fixture-only",
                }
            )
        elif "/exchanges/" in request.url:
            gets.append(
                {
                    "url": request.url,
                    "receipt": request.headers.get("x-sac-exchange-receipt"),
                }
            )
            outcome = outcomes[min(len(gets) - 1, len(outcomes) - 1)]
            route.fulfill(json={"exchange_id": exchange, **outcome})
        elif "partial=1" in request.url:
            name = "beta" if "/beta" in request.url else "alpha"
            route.fulfill(
                content_type="text/html",
                body=f'<div data-role="chat-thread">Fixture thread</div><form class="chat-composer" data-composer="dm" action="/{name}/message"><textarea name="message"></textarea><button type="submit">Send</button></form>',
            )
        else:
            route.fulfill(content_type="text/html", body=html)

    page.route("https://dashboard.example/**", respond)
    page.goto("https://dashboard.example/")
    page.add_script_tag(path=str(SCRIPT))
    return posts, gets, exchange


@pytest.mark.parametrize("kind", ["fleet-dm", "dm"])
def test_accepted_receipt_survives_fragment_refresh_and_final_admission(
    browser_page, kind
):
    # Arrange: the second GET confirms native admission, not model completion.
    page = browser_page
    posts, gets, exchange = mount_admission(
        page,
        [
            {
                "state": "pending",
                "final": False,
                "message": "Admission pending.",
                "code": 102,
            },
            {
                "state": "received",
                "final": True,
                "message": "Received by agent. Model task completion is separate.",
                "code": 200,
            },
        ],
        kind=kind,
    )
    page.locator("textarea").first.fill("Disposable admission fixture")
    # Act
    page.locator("textarea").first.press("Enter")
    page.locator(
        '[data-role="fleet-chat-body"] [data-admission-receipt][data-admission-state="received"]'
    ).wait_for()
    node = page.locator('[data-role="fleet-chat-body"] [data-admission-receipt]').first
    # Assert
    assert (
        len(posts),
        len(gets),
        gets[0]["receipt"],
        node.get_attribute("data-exchange-id"),
        "Model task completion is separate" in node.inner_text(),
        page.url,
    ) == (1, 2, "signed-fixture-only", exchange, True, "https://dashboard.example/")


def test_terminal_admission_failure_keeps_receipt_without_reposting(browser_page):
    # Arrange
    page = browser_page
    posts, gets, exchange = mount_admission(
        page,
        [
            {
                "state": "failed",
                "final": True,
                "message": "Agent admission failed.",
                "native_message": "fixture refusal",
                "code": 403,
            }
        ],
    )
    page.locator("textarea").first.fill("Disposable refusal fixture")
    # Act
    page.locator("textarea").first.press("Enter")
    page.locator(
        '[data-role="fleet-chat-body"] [data-admission-state="failed"]'
    ).wait_for()
    # Assert
    assert (
        len(posts),
        len(gets),
        page.locator(
            '[data-role="fleet-chat-body"] [data-admission-receipt]'
        ).get_attribute("data-exchange-id"),
    ) == (1, 1, exchange)


def test_local_remote_local_selection_clears_old_target_without_losing_draft(
    browser_page,
):
    # Arrange
    page = browser_page
    posts, gets, _ = mount_admission(
        page,
        [
            {
                "state": "pending",
                "final": False,
                "message": "Admission pending.",
                "code": 202,
            }
        ],
    )
    area = page.locator("textarea").first
    area.fill("Keep unsubmitted draft")
    # Act: trusted row clicks select a different recipient, then return.
    page.locator('tr[data-agent-name="beta"] td').click(position={"x": 5, "y": 5})
    page.wait_for_function(
        "document.querySelector('form[data-composer]').getAttribute('action') === null"
    )
    remote = (
        page.locator('form[data-composer="fleet-dm"]').get_attribute("action"),
        area.is_disabled(),
        page.locator("button[type=submit]").first.is_disabled(),
        area.input_value(),
    )
    page.locator('tr[data-agent-name="alpha"] td').click(position={"x": 5, "y": 5})
    page.wait_for_function(
        "document.querySelector('form[data-composer]').getAttribute('action') === '/alpha/message'"
    )
    # Assert
    assert (
        remote,
        page.locator('form[data-composer="fleet-dm"]').get_attribute("action"),
        area.is_disabled(),
        area.input_value(),
        posts,
        gets,
    ) == (
        (None, True, True, "Keep unsubmitted draft"),
        "/alpha/message",
        False,
        "Keep unsubmitted draft",
        [],
        [],
    )


def test_selection_change_stops_pending_gets_and_preserves_receipt_for_return(
    browser_page,
):
    # Arrange
    page = browser_page
    posts, gets, exchange = mount_admission(
        page,
        [
            {
                "state": "pending",
                "final": False,
                "message": "Admission pending.",
                "code": 102,
            }
        ],
    )
    page.locator("textarea").first.fill("Disposable pending fixture")
    # Act
    page.locator("textarea").first.press("Enter")
    page.locator(
        '[data-role="fleet-chat-body"] [data-admission-state="pending"]'
    ).wait_for()
    page.locator('tr[data-agent-name="beta"] td').click(position={"x": 5, "y": 5})
    page.wait_for_timeout(1800)
    count = len(gets)
    page.locator('tr[data-agent-name="alpha"] td').click(position={"x": 5, "y": 5})
    page.locator('[data-role="fleet-chat-body"] [data-admission-receipt]').wait_for()
    # Assert
    assert (
        len(posts),
        len(gets) == count,
        page.locator(
            '[data-role="fleet-chat-body"] [data-admission-receipt]'
        ).get_attribute("data-exchange-id"),
    ) == (1, True, exchange)


def test_late_fragment_cannot_restore_previous_recipient(browser_page):
    # Arrange: retain alpha's fragment until after beta becomes the selection.
    page = browser_page
    posts, _, _ = mount_admission(page, [{"state": "pending", "final": False}])
    delayed = []

    def hold_alpha(route):
        delayed.append(route)

    page.route("https://dashboard.example/alpha/?partial=1", hold_alpha)
    page.locator('tr[data-agent-name="alpha"] td').click(position={"x": 5, "y": 5})
    page.wait_for_function(
        "document.querySelector('form').action.endsWith('/alpha/message')"
    )
    page.wait_for_timeout(100)
    # Act: a later cross-host selection must win over the stale response.
    page.locator('tr[data-agent-name="beta"] td').click(position={"x": 5, "y": 5})
    page.wait_for_function(
        "document.querySelector('form').getAttribute('action') === null"
    )
    for route in delayed:
        route.fulfill(
            content_type="text/html",
            body='<form data-composer="dm" action="/alpha/message"><textarea name="message"></textarea><button type="submit">Send</button></form>',
        )
    page.wait_for_timeout(100)
    # Assert: no previous action can be reinstated by an out-of-order fragment.
    form = page.locator('form[data-composer="fleet-dm"]')
    assert (
        form.get_attribute("action"),
        form.locator("textarea").is_disabled(),
        posts,
        len(delayed),
    ) == (None, True, [], 1)

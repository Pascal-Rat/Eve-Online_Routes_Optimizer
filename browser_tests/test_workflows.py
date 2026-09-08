from __future__ import annotations

from datetime import timedelta

import pytest
from playwright.sync_api import Page, expect

from browser_tests.conftest import WebSession
from tests.support.scenarios import make_contract, make_snapshot
from tests.support.web import SlowTransport, planning_payload, proposal_input, seed_snapshot


def configure(page: Page) -> None:
    page.locator("#start").fill("Alpha")
    page.locator("#start-options").get_by_role("option").first.click()
    page.locator("#cargo-m3").fill("1")
    page.locator("#collateral-isk").fill("2")
    page.locator("#collateral-unit").select_option("isk")
    page.locator("#duration-hours").fill("0")
    page.locator("#duration-minutes").fill("6")


def test_scan_rank_solve_reload_and_execute(page: Page, web_session: WebSession) -> None:
    errors: list[str] = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.on("dialog", lambda dialog: dialog.accept())
    page.goto(web_session.url)
    configure(page)
    page.locator("#all-regions").click()
    page.locator("#scan-button").click()
    expect(page.locator("#metric-observed")).to_have_text("1", timeout=15000)
    page.locator("#rank-button").click()
    expect(page.locator("#rank-body")).to_contain_text("9001", timeout=15000)
    page.locator("#solve-button").click()
    expect(page.locator("#start-execution")).to_be_enabled(timeout=15000)
    page.reload()
    expect(page.locator("#start-execution")).to_be_enabled()
    page.locator("#locked-confirm").check()
    page.locator("#start-execution").click()
    expect(page.locator("#exec-active")).to_have_text("1")
    assert web_session.app.trip is not None
    web_session.clock.value += timedelta(seconds=1)
    expect(page.locator("#scan-button")).to_be_disabled()
    page.get_by_role("button", name="Record pickup #9001", exact=True).click()
    expect(page.get_by_role("button", name="Record delivery #9001", exact=True)).to_be_visible()
    page.locator("#replan-button").click()
    expect(page.locator("#busy-layer")).to_be_hidden(timeout=15000)
    page.reload()
    page.get_by_role("button", name="Record delivery #9001", exact=True).click()
    expect(page.locator("#exec-active")).to_have_text("0")
    page.locator("#reset-execution").click()
    expect(page.locator("#scan-button")).to_be_enabled()
    assert errors == []


def test_infeasible_route_keeps_recovery_controls(page: Page, web_session: WebSession) -> None:
    app, clock = web_session.app, web_session.clock
    app.scan({"regions": [10]})
    app.solve(planning_payload())
    app.start_execution(proposal_input(app, {"confirm_locked_acceptance": True}))
    assert app.trip is not None
    clock.value = app.trip.session_deadline - timedelta(seconds=1)
    assert app.replan({"refresh": False})["plan"]["route"] == []
    clock.value += timedelta(minutes=1)
    page.on("dialog", lambda dialog: dialog.accept())
    page.goto(web_session.url)
    expect(page.locator("#route-wrap")).to_be_hidden()
    page.get_by_role("button", name="Record pickup #9001", exact=True).click()
    expect(page.get_by_role("button", name="Record delivery #9001", exact=True)).to_be_visible()
    page.locator("#extend-horizon").click()
    expect(page.locator("#notice")).to_contain_text("Planning horizon extended")
    page.reload()
    page.locator("#replan-button").click()
    expect(page.locator("#route-wrap")).to_be_visible(timeout=15000)
    page.get_by_role("button", name="Record delivery #9001", exact=True).click()
    expect(page.locator("#exec-active")).to_have_text("0")


def test_zero_denominator_rank_renders_without_json_errors(
    page: Page, web_session: WebSession
) -> None:
    app, clock = web_session.app, web_session.clock
    seed_snapshot(
        app, make_snapshot(clock.value, make_contract(clock.value, 1, 101, 101, collateral=0))
    )
    page.goto(web_session.url)
    configure(page)
    page.locator("details").filter(has=page.locator("#service-seconds")).locator("summary").click()
    page.locator("#service-seconds").fill("0")
    page.locator("#rank-button").click()
    expect(page.locator("#rank-body")).to_contain_text("#1", timeout=15000)
    expect(page.locator("#rank-body td").nth(6)).to_have_text("--")
    expect(page.locator("#rank-body td").nth(7)).to_have_text("--")


def test_reload_running_job_and_cancel(page: Page, web_session: WebSession) -> None:
    web_session.app.esi.transport = SlowTransport(web_session.clock.value)
    page.goto(web_session.url)
    configure(page)
    page.locator("#all-regions").click()
    page.locator("#scan-button").click()
    expect(page.locator("#cancel-job")).to_be_visible()
    page.reload()
    expect(page.locator("#cancel-job")).to_be_visible()
    page.locator("#cancel-job").click()
    expect(page.locator("#busy-layer")).to_be_hidden()
    expect(page.locator("#scan-button")).to_be_enabled()
    assert web_session.app.snapshot is None
    assert web_session.app.artifact("snapshot.json") is None


def test_autocomplete_ignores_outdated_and_dismissed_results(
    page: Page, web_session: WebSession
) -> None:
    from playwright.sync_api import Route

    requests: dict[str, Route] = {}

    def intercept(route: Route) -> None:
        query = route.request.url.rsplit("=", 1)[1]
        requests[query] = route
        if query == "Be":
            route.fulfill(
                json={
                    "items": [
                        {"id": 2, "name": "Beta", "security_status": 0.9},
                        {"id": 3, "name": "Beta Minor", "security_status": 0.8},
                    ]
                }
            )

    page.route("**/api/systems?q=*", intercept)
    page.goto(web_session.url)
    start = page.locator("#start")
    menu = page.locator("#start-options")
    with page.expect_request("**/api/systems?q=Al"):
        start.fill("Al")
    with page.expect_request("**/api/systems?q=Be"):
        start.fill("Be")
    expect(menu.get_by_role("option")).to_have_text(["Betasec 0.90", "Beta Minorsec 0.80"])
    requests["Al"].fulfill(json={"items": [{"id": 1, "name": "Alpha", "security_status": 1.0}]})
    page.evaluate("() => new Promise(requestAnimationFrame)")
    expect(menu.get_by_role("option")).to_have_text(["Betasec 0.90", "Beta Minorsec 0.80"])
    start.press("ArrowUp")
    expect(start).to_have_attribute("aria-activedescendant", "start-options-option-1")
    start.press("ArrowDown")
    expect(start).to_have_attribute("aria-activedescendant", "start-options-option-0")
    start.press("Enter")
    expect(start).to_have_value("Beta")
    expect(start).to_have_attribute("data-system-id", "2")

    with page.expect_request("**/api/systems?q=Ga"):
        start.fill("Ga")
    start.press("Escape")
    requests["Ga"].fulfill(json={"items": [{"id": 3, "name": "Gamma"}]})
    page.evaluate("() => new Promise(requestAnimationFrame)")
    expect(menu).to_be_hidden()


@pytest.mark.parametrize("width", [1024, 1440])
def test_desktop_keeps_controls_beside_the_route(
    page: Page, web_session: WebSession, width: int
) -> None:
    web_session.app.scan({"regions": [10]})
    web_session.app.solve(planning_payload())
    page.set_viewport_size({"width": width, "height": 900})
    page.goto(web_session.url)
    expect(page.locator("#route-wrap")).to_be_visible()
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    controls = page.locator(".controls").bounding_box()
    results = page.locator(".results").bounding_box()
    assert controls is not None and results is not None
    assert controls["x"] + controls["width"] <= results["x"]

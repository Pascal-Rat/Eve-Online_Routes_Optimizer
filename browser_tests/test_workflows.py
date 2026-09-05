from __future__ import annotations

from datetime import timedelta

from playwright.sync_api import Page, expect

from eve_courier_optimizer.snapshot import write_snapshot
from tests.conftest import make_contract, make_snapshot
from tests.test_jobs import SlowTransport
from tests.test_webapp import planning_payload

from .conftest import WebSession


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
    assert web_session.app.execution is not None
    web_session.clock.value = web_session.app.execution.current_time + timedelta(seconds=1)
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
    app.start_execution({"confirm_locked_acceptance": True})
    assert app.execution is not None
    clock.value = app.execution.session_deadline - timedelta(seconds=1)
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
    app.snapshot = make_snapshot(clock.value, make_contract(clock.value, 1, 101, 101, collateral=0))
    write_snapshot(app.snapshot_path, app.snapshot)
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
    assert not web_session.app.snapshot_path.exists()

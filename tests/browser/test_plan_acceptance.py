from __future__ import annotations

import re

from playwright.sync_api import Page, expect

from tests.browser.conftest import WebSession
from tests.support.scenarios import make_contract, make_snapshot
from tests.support.web import planning_payload, seed_snapshot


def test_two_tabs_cannot_apply_a_proposal_the_older_tab_never_reviewed(
    page: Page, web_session: WebSession
) -> None:
    app, clock = web_session.app, web_session.clock
    app.scan({"regions": [10]})
    app.solve(planning_payload())
    page.goto(web_session.url)
    expect(page.get_by_role("button", name="Arm this plan for execution")).to_be_enabled()
    second = page.context.new_page()
    try:
        seed_snapshot(
            app,
            make_snapshot(
                clock.value,
                make_contract(clock.value, 1, 101, 102),
                make_contract(clock.value, 2, 101, 102),
            ),
        )
        second.goto(web_session.url)
        second.get_by_label(re.compile("^Start system")).fill("Alpha")
        second.get_by_role("option", name=re.compile("Alpha")).first.click()
        second.get_by_role("button", name=re.compile("Optimize & prove"), exact=False).click()
        expect(second.get_by_role("button", name="Arm this plan for execution")).to_be_enabled(
            timeout=15000
        )
        page.locator("#locked-confirm").check()
        page.get_by_role("button", name="Arm this plan for execution").click()
        expect(page.locator("#notice")).to_contain_text("workspace changed")
        assert app.trip is None
        page.reload()
        page.locator("#locked-confirm").check()
        page.get_by_role("button", name="Arm this plan for execution").click()
        expect(page.locator("#exec-active")).to_have_text("2")
    finally:
        second.close()


def test_rolling_replan_requires_apply_before_using_the_refreshed_policy(
    page: Page, web_session: WebSession
) -> None:
    from tests.support.rolling_policy import prepare_rolling_policy_refresh

    app = web_session.app
    prepare_rolling_policy_refresh(app, web_session.clock.value)
    page.goto(web_session.url)
    expect(page.locator("#replan-button")).to_be_enabled()
    page.locator("#replan-button").click()
    apply = page.get_by_role("button", name="Apply revised plan to execution")
    expect(apply).to_be_visible(timeout=15000)
    expect(apply).to_be_enabled()
    expect(page.locator('#route-body button[data-action="pickup"]')).to_be_disabled()
    assert app.trip is not None and 2 in app.trip.security.threat_avoided_system_ids
    apply.click()
    pickup = page.locator('#route-body button[data-action="pickup"]')
    expect(pickup).to_be_enabled()
    assert app.trip is not None
    pickup.click()
    expect(page.locator("#exec-active")).to_have_text("1")
    assert app.trip is not None
    assert not app.trip.security.threat_avoided_system_ids
    assert app.trip.active_shipments[0].picked

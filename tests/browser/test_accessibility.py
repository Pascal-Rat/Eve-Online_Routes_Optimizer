from __future__ import annotations

import re

from playwright.sync_api import Page, expect

from tests.browser.conftest import WebSession


def test_planner_fields_have_meaningful_accessible_names(
    page: Page, web_session: WebSession
) -> None:
    page.goto(web_session.url)
    for name in (
        "Start system",
        "Collateral ISK",
        "Time budget hours",
        "Time budget minutes",
        "Avoid systems",
        "Required route systems",
        "Finish system",
    ):
        expect(page.get_by_label(re.compile("^" + name))).to_have_count(1)
    # Region selection is conditional, but its label must still resolve when the picker is shown.
    expect(page.get_by_label(re.compile("^Regions search"))).to_have_count(1)

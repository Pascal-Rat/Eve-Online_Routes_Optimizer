from __future__ import annotations

import json

import pytest
from playwright.sync_api import Page, expect

from eve_courier_optimizer.jsonio import json_object
from tests.browser.conftest import WebSession
from tests.support.web import planning_payload, proposal_input


@pytest.mark.parametrize("failure", ["network", "http", "json", "shape"])
def test_status_errors_distinguish_offline_from_server_and_render_failures(
    page: Page, web_session: WebSession, failure: str
) -> None:
    from playwright.sync_api import Route

    def status(route: Route) -> None:
        if failure == "network":
            route.abort("connectionrefused")
        elif failure == "http":
            route.fulfill(status=500, json={"error": "Workspace storage is unavailable"})
        elif failure == "json":
            route.fulfill(status=200, content_type="application/json", body="{broken")
        else:
            route.fulfill(json={"revision": 1, "sde": None})

    page.route("**/api/status", status)
    page.goto(web_session.url)
    notice = page.locator("#notice")
    if failure == "network":
        expect(notice).to_contain_text("Local backend not connected")
    else:
        expect(notice).not_to_contain_text("Local backend not connected")
        expected = {
            "http": "Workspace storage is unavailable",
            "json": "invalid JSON",
            "shape": "is required",
        }[failure]
        expect(notice).to_contain_text(expected)


@pytest.mark.parametrize("operation", ["solve", "replan"])
@pytest.mark.parametrize("invalid", [False, True])
def test_completed_plan_job_validates_its_result_before_display(
    page: Page, web_session: WebSession, operation: str, invalid: bool
) -> None:
    app = web_session.app
    app.scan({"regions": [10]})
    result = app.solve(planning_payload())
    if operation == "replan":
        app.start_execution(proposal_input(app, {"confirm_locked_acceptance": True}))
        result = app.replan({"refresh": False})
    payload = json_object(json.loads(json.dumps(result)), "result")
    if invalid:
        model = json_object(json_object(payload["plan"], "plan")["model"], "model")
        model["start_system_id"] = []
    page.goto(web_session.url)
    button = page.locator(f"#{operation}-button")
    expect(button).to_be_enabled()
    page.route(
        "**/api/jobs",
        lambda route: route.fulfill(
            json={
                "job": {
                    "id": "contract-test-job",
                    "operation": operation,
                    "status": "completed",
                    "progress": "Complete",
                    "elapsed_seconds": 0,
                    "result": payload,
                }
            }
        ),
    )
    button.click()
    notice = page.locator("#notice")
    if invalid:
        expect(notice).to_contain_text(
            "PlanResponse.plan.model.start_system_id has an invalid primitive type"
        )
    else:
        expect(notice).to_contain_text(
            "Replan is ready but not armed yet"
            if operation == "replan"
            else "Global optimum proven"
        )
        expect(page.locator("#route-wrap")).to_be_visible()

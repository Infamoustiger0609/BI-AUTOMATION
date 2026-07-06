from app.models import DashboardRequest, DashboardResponse, IntentResult


def test_dashboard_request_model():
    request = DashboardRequest(prompt="Build a sales dashboard")
    assert request.prompt == "Build a sales dashboard"
    assert request.include_sample_data is True


def test_dashboard_response_model():
    response = DashboardResponse(status="queued")
    assert response.status == "queued"
    assert response.intent is None


def test_intent_result_model():
    intent = IntentResult(dashboard_title="Sales Dashboard")
    assert intent.dashboard_title == "Sales Dashboard"


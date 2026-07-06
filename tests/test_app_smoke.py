from app.main import app


def test_app_has_required_routes():
    paths = {route.path for route in app.routes}
    assert "/api/health" in paths
    assert "/api/generate" in paths
    assert "/api/generate-with-data" in paths
    assert "/ws/status" in paths


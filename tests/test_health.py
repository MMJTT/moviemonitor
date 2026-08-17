def test_health_endpoint(client):
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_status_endpoint_returns_detailed_runtime_status(client, mocker):
    expected = {
        "status": "degraded",
        "database": "ok",
        "redis": "error",
        "worker": {"status": "stale", "started_at": None, "heartbeat_at": None},
        "mail": {"status": "unverified", "verified_at": None},
        "tasks": {
            "monitoring": 1,
            "error": 0,
            "detected": 0,
            "pending_notifications": 0,
        },
        "last_check_at": None,
        "backup": {"status": "missing", "last_at": None, "name": None},
        "disk": {
            "status": "ok",
            "total_bytes": 1000,
            "used_bytes": 400,
            "free_bytes": 600,
        },
    }
    mocker.patch("ticketwatch.health.collect_runtime_status", return_value=expected)

    response = client.get("/statusz")

    assert response.status_code == 200
    assert response.json() == expected

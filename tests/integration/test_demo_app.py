"""HTTP-level integration coverage for the deterministic local legacy portal."""

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from demo_app.app import app


@pytest.fixture
def client() -> Iterator[TestClient]:
    with TestClient(app, follow_redirects=True) as test_client:
        response = test_client.post("/__dev__/reset")
        assert response.status_code == 200
        yield test_client


def test_health_and_landing_page(client: TestClient) -> None:
    health = client.get("/health")
    assert health.status_code == 200
    assert health.json() == {
        "status": "ok",
        "service": "legacy-demo-bank",
        "data": "synthetic",
    }

    landing = client.get("/")
    assert landing.status_code == 200
    assert landing.url.path == "/members/search"
    assert '<label for="member-id">Member ID</label>' in landing.text
    assert "DEMONSTRATION ONLY" in landing.text
    assert "data-testid" not in landing.text


def test_successful_lookup_uses_member_detail_and_iframe(client: TestClient) -> None:
    detail = client.post("/members/search", data={"member_id": "12345"})

    assert detail.status_code == 200
    assert detail.url.path == "/members/12345"
    assert "Member servicing" in detail.text
    assert "Member ID (synthetic)" in detail.text
    assert "View savings account" in detail.text
    assert 'name="account-detail"' in detail.text
    assert 'title="Account details"' in detail.text

    account = client.get("/members/12345/accounts/savings")
    assert account.status_code == 200
    assert 'role="status" aria-label="Savings account balance"' in account.text
    assert "Current balance:" in account.text
    assert "$1234.56" in account.text


@pytest.mark.parametrize(
    ("member_id", "expected_status", "expected_message"),
    [
        ("99999", 404, "Member not found"),
        ("55555", 403, "Permission denied"),
    ],
)
def test_stable_member_outcomes(
    client: TestClient,
    member_id: str,
    expected_status: int,
    expected_message: str,
) -> None:
    response = client.post(
        "/members/search",
        data={"member_id": member_id},
        follow_redirects=False,
    )
    assert response.status_code == 303

    outcome = client.get(response.headers["location"])
    assert outcome.status_code == expected_status
    assert 'role="alert"' in outcome.text
    assert expected_message in outcome.text


@pytest.mark.parametrize("invalid_member_id", ["", "1234", "123456", "12A45", " 12345"])
def test_invalid_format_is_a_validation_outcome(
    client: TestClient,
    invalid_member_id: str,
) -> None:
    response = client.post("/members/search", data={"member_id": invalid_member_id})
    assert response.status_code == 422
    assert "Invalid member ID format" in response.text
    assert "Enter exactly five digits" in response.text


def test_fixture_77777_is_transient_once_then_succeeds(client: TestClient) -> None:
    first = client.get("/members/77777/accounts/savings")
    assert first.status_code == 503
    assert "Legacy host is still loading" in first.text
    assert "Current balance:" not in first.text

    retry = client.get("/members/77777/accounts/savings")
    assert retry.status_code == 200
    assert "Current balance:" in retry.text
    assert "$987.65" in retry.text

    reset = client.post("/__dev__/reset")
    assert reset.json() == {"status": "reset"}
    after_reset = client.get("/members/77777/accounts/savings")
    assert after_reset.status_code == 503


def test_open_sub_account_has_real_review_and_commit_boundary(client: TestClient) -> None:
    form = client.get("/members/12345/sub-accounts/new")
    assert form.status_code == 200
    assert '<label for="account-type">Sub-account type</label>' in form.text
    assert '<label for="nickname">Account nickname</label>' in form.text
    assert "Review sub-account" in form.text

    review = client.post(
        "/members/12345/sub-accounts/review",
        data={"account_type": "holiday_savings", "nickname": "Travel Fund"},
    )
    assert review.status_code == 200
    assert "Review sub-account" in review.text
    assert "Human confirmation required" in review.text
    assert "Commit sub-account" in review.text
    assert "Sub-account opened" not in review.text

    committed = client.post(
        "/members/12345/sub-accounts/commit",
        data={"account_type": "holiday_savings", "nickname": "Travel Fund"},
    )
    assert committed.status_code == 200
    assert 'role="status" aria-label="Sub-account opened"' in committed.text
    assert "DEMO-SA-0001" in committed.text
    assert "Travel Fund" in committed.text

    operator = client.get("/operator")
    assert operator.status_code == 200
    assert "Committed synthetic sub-accounts this process: 1" in operator.text


def test_sub_account_form_rejects_invalid_server_side_values(client: TestClient) -> None:
    response = client.post(
        "/members/12345/sub-accounts/review",
        data={"account_type": "unsupported", "nickname": "  "},
    )
    assert response.status_code == 422
    assert "Check the sub-account form" in response.text
    assert "Commit sub-account" not in response.text


def test_unknown_route_stays_human_readable(client: TestClient) -> None:
    response = client.get("/definitely-missing")
    assert response.status_code == 404
    assert "Page not found" in response.text
    assert 'role="alert"' in response.text

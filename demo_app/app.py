"""Intentionally old-fashioned, deterministic banking portal for local automation demos."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from threading import Lock
from typing import Annotated

from fastapi import FastAPI, Form, Request, status
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

BASE_DIR = Path(__file__).resolve().parent
MEMBER_ID_PATTERN = re.compile(r"^[0-9]{5}$")
ACCOUNT_TYPES = {
    "holiday_savings": "Holiday savings",
    "emergency_savings": "Emergency savings",
}


@dataclass(frozen=True, slots=True)
class MemberFixture:
    """Synthetic member information; no values represent a real person."""

    member_id: str
    display_name: str
    savings_balance: Decimal


@dataclass(frozen=True, slots=True)
class OpenedAccount:
    """A deterministic, in-memory record created at the demo commit boundary."""

    confirmation: str
    member_id: str
    account_type: str
    nickname: str


@dataclass(slots=True)
class FixtureState:
    """Mutable failure-injection state resettable between replay runs."""

    savings_panel_requests: dict[str, int] = field(default_factory=dict)
    opened_accounts: list[OpenedAccount] = field(default_factory=list)
    commit_sequence: int = 0
    _lock: Lock = field(default_factory=Lock, repr=False)

    def next_savings_request(self, member_id: str) -> int:
        with self._lock:
            count = self.savings_panel_requests.get(member_id, 0) + 1
            self.savings_panel_requests[member_id] = count
            return count

    def commit(self, member_id: str, account_type: str, nickname: str) -> OpenedAccount:
        with self._lock:
            self.commit_sequence += 1
            account = OpenedAccount(
                confirmation=f"DEMO-SA-{self.commit_sequence:04d}",
                member_id=member_id,
                account_type=account_type,
                nickname=nickname,
            )
            self.opened_accounts.append(account)
            return account

    def reset(self) -> None:
        with self._lock:
            self.savings_panel_requests.clear()
            self.opened_accounts.clear()
            self.commit_sequence = 0


MEMBERS = {
    "12345": MemberFixture(
        member_id="12345",
        display_name="Fixture Member Alpha",
        savings_balance=Decimal("1234.56"),
    ),
    "77777": MemberFixture(
        member_id="77777",
        display_name="Fixture Member Retry",
        savings_balance=Decimal("987.65"),
    ),
}


app = FastAPI(
    title="Legacy Demo Bank",
    description="Local-only synthetic fixture application for computer-use automation.",
    version="1.0.0",
)
app.state.fixture_state = FixtureState()
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
templates = Jinja2Templates(directory=BASE_DIR / "templates")


def render(
    request: Request,
    template_name: str,
    *,
    status_code: int = status.HTTP_200_OK,
    **context: object,
) -> HTMLResponse:
    """Render a page with the context common to every synthetic fixture screen."""

    return templates.TemplateResponse(
        request=request,
        name=template_name,
        context={"fixture_notice": True, **context},
        status_code=status_code,
    )


def member_or_outcome(member_id: str) -> tuple[MemberFixture | None, str | None, int]:
    """Resolve a member ID into a fixture or a stable UI outcome."""

    if not MEMBER_ID_PATTERN.fullmatch(member_id):
        return None, "invalid_member_id", status.HTTP_422_UNPROCESSABLE_ENTITY
    if member_id == "55555":
        return None, "permission_denied", status.HTTP_403_FORBIDDEN
    member = MEMBERS.get(member_id)
    if member is None:
        return None, "member_not_found", status.HTTP_404_NOT_FOUND
    return member, None, status.HTTP_200_OK


@app.get("/", include_in_schema=False)
async def index() -> RedirectResponse:
    """Send operators to the legacy member-search landing page."""

    return RedirectResponse(url="/members/search", status_code=status.HTTP_303_SEE_OTHER)


@app.get("/health")
async def health() -> dict[str, str]:
    """Report readiness without exposing fixture or session state."""

    return {"status": "ok", "service": "legacy-demo-bank", "data": "synthetic"}


@app.post("/__dev__/reset")
async def reset_fixtures() -> dict[str, str]:
    """Reset deterministic counters and fake committed records between demo runs."""

    app.state.fixture_state.reset()
    return {"status": "reset"}


@app.get("/members/search", response_class=HTMLResponse)
async def member_search(request: Request) -> HTMLResponse:
    """Render the member lookup form."""

    return render(request, "member_search.html")


@app.post("/members/search", response_class=HTMLResponse)
async def submit_member_search(
    request: Request,
    member_id: Annotated[str, Form()] = "",
) -> Response:
    """Validate format in the UI, then navigate to the requested fixture member."""

    if not MEMBER_ID_PATTERN.fullmatch(member_id):
        return render(
            request,
            "member_search.html",
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            outcome="invalid_member_id",
            entered_member_id=member_id,
        )
    return RedirectResponse(
        url=f"/members/{member_id}",
        status_code=status.HTTP_303_SEE_OTHER,
    )


@app.get("/members/{member_id}", response_class=HTMLResponse)
async def member_detail(request: Request, member_id: str) -> HTMLResponse:
    """Render member details or one of the stable exceptional states."""

    member, outcome, response_status = member_or_outcome(member_id)
    if outcome is not None:
        return render(
            request,
            "member_outcome.html",
            status_code=response_status,
            outcome=outcome,
        )
    assert member is not None
    return render(request, "member_detail.html", member=member)


@app.get("/account-panel/empty", response_class=HTMLResponse)
async def empty_account_panel(request: Request) -> HTMLResponse:
    """Render the empty account-detail iframe before an account is selected."""

    return render(request, "account_empty.html")


@app.get("/members/{member_id}/accounts/savings", response_class=HTMLResponse)
async def savings_account_panel(request: Request, member_id: str) -> HTMLResponse:
    """Render a balance, with one deterministic transient for fixture 77777."""

    member, outcome, response_status = member_or_outcome(member_id)
    if outcome is not None:
        return render(
            request,
            "account_outcome.html",
            status_code=response_status,
            outcome=outcome,
        )
    assert member is not None
    request_number = app.state.fixture_state.next_savings_request(member_id)
    if member_id == "77777" and request_number == 1:
        return render(
            request,
            "account_transient.html",
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        )
    return render(request, "account_savings.html", member=member)


@app.get("/members/{member_id}/sub-accounts/new", response_class=HTMLResponse)
async def new_sub_account(request: Request, member_id: str) -> HTMLResponse:
    """Render the safe, automatable portion of the sub-account form."""

    member, outcome, response_status = member_or_outcome(member_id)
    if outcome is not None:
        return render(
            request,
            "member_outcome.html",
            status_code=response_status,
            outcome=outcome,
        )
    assert member is not None
    return render(
        request,
        "sub_account_form.html",
        member=member,
        account_types=ACCOUNT_TYPES,
    )


@app.post("/members/{member_id}/sub-accounts/review", response_class=HTMLResponse)
async def review_sub_account(
    request: Request,
    member_id: str,
    account_type: Annotated[str, Form()],
    nickname: Annotated[str, Form()],
) -> HTMLResponse:
    """Validate submitted values and render the pre-commit review boundary."""

    member, outcome, response_status = member_or_outcome(member_id)
    if outcome is not None:
        return render(
            request,
            "member_outcome.html",
            status_code=response_status,
            outcome=outcome,
        )
    assert member is not None
    normalized_nickname = " ".join(nickname.split())
    if account_type not in ACCOUNT_TYPES or not (1 <= len(normalized_nickname) <= 40):
        return render(
            request,
            "sub_account_form.html",
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            member=member,
            account_types=ACCOUNT_TYPES,
            outcome="invalid_sub_account_form",
            entered_account_type=account_type,
            entered_nickname=normalized_nickname,
        )
    return render(
        request,
        "sub_account_review.html",
        member=member,
        account_type=account_type,
        account_type_label=ACCOUNT_TYPES[account_type],
        nickname=normalized_nickname,
    )


@app.post("/members/{member_id}/sub-accounts/commit", response_class=HTMLResponse)
async def commit_sub_account(
    request: Request,
    member_id: str,
    account_type: Annotated[str, Form()],
    nickname: Annotated[str, Form()],
) -> HTMLResponse:
    """Cross the irreversible demo boundary; callers must enforce human ownership."""

    member, outcome, response_status = member_or_outcome(member_id)
    if outcome is not None:
        return render(
            request,
            "member_outcome.html",
            status_code=response_status,
            outcome=outcome,
        )
    assert member is not None
    normalized_nickname = " ".join(nickname.split())
    if account_type not in ACCOUNT_TYPES or not (1 <= len(normalized_nickname) <= 40):
        return render(
            request,
            "sub_account_form.html",
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            member=member,
            account_types=ACCOUNT_TYPES,
            outcome="invalid_sub_account_form",
            entered_account_type=account_type,
            entered_nickname=normalized_nickname,
        )
    opened_account = app.state.fixture_state.commit(
        member_id=member_id,
        account_type=account_type,
        nickname=normalized_nickname,
    )
    return render(
        request,
        "sub_account_confirmation.html",
        member=member,
        opened_account=opened_account,
        account_type_label=ACCOUNT_TYPES[account_type],
    )


@app.get("/operator", response_class=HTMLResponse)
async def operator_surface(request: Request) -> HTMLResponse:
    """Explain the deliberately thin local same-session operator workflow."""

    return render(
        request,
        "operator.html",
        committed_count=len(app.state.fixture_state.opened_accounts),
    )


@app.exception_handler(404)
async def not_found(request: Request, _exc: Exception) -> Response:
    """Keep browser-facing unknown routes understandable while preserving API JSON."""

    if request.url.path.startswith("/api/"):
        return JSONResponse({"detail": "Not found"}, status_code=status.HTTP_404_NOT_FOUND)
    return render(
        request,
        "not_found.html",
        status_code=status.HTTP_404_NOT_FOUND,
    )


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("demo_app.app:app", host="127.0.0.1", port=8000, reload=False)

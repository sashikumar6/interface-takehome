import pytest
from playwright.async_api import Error as PlaywrightError

from computer_use.domain.errors import SurfaceExecutionError
from computer_use.safety.redaction import DEFAULT_REDACTOR
from computer_use.surfaces.playwright import PlaywrightSurfaceDriver


class FakePage:
    def is_closed(self) -> bool:
        return False


class FakeDialog:
    message = "Confirm transfer?"

    def __init__(self) -> None:
        self.dismissed = False

    async def dismiss(self) -> None:
        self.dismissed = True


def bare_driver() -> PlaywrightSurfaceDriver:
    driver = object.__new__(PlaywrightSurfaceDriver)
    driver._page = FakePage()  # type: ignore[assignment]
    driver._redactor = DEFAULT_REDACTOR
    driver._unexpected_dialogs = []
    return driver


def test_unexpected_dialog_is_not_auto_dismissed() -> None:
    driver = bare_driver()
    dialog = FakeDialog()

    driver._on_dialog(dialog)  # type: ignore[arg-type]

    assert dialog.dismissed is False
    with pytest.raises(SurfaceExecutionError) as captured:
        driver._check_session()
    assert captured.value.code == "UNEXPECTED_DIALOG"
    assert captured.value.details == {"messages": ("Confirm transfer?",)}


def test_detached_element_is_not_classified_as_session_expiry() -> None:
    driver = bare_driver()
    error = driver._execution_error(
        PlaywrightError("Element is not attached to the DOM"), operation="click"
    )
    assert error.code == "TARGET_DETACHED"

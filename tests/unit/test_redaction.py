from computer_use.safety.redaction import REDACTED, redact, redact_text


def test_structured_secrets_and_member_ids_are_redacted() -> None:
    safe = redact(
        {
            "api_key": "unit-test-api-key",
            "password": "correct horse",
            "member_id": "12345",
            "nested": {"authorization": "Bearer abcdefghijklmnop"},
        }
    )
    assert safe["api_key"] == REDACTED
    assert safe["password"] == REDACTED
    assert safe["member_id"] == "***45"
    assert safe["nested"]["authorization"] == REDACTED


def test_free_text_and_member_urls_are_redacted() -> None:
    text = redact_text(
        "Authorization: Bearer fake-token member_id=12345 /members/77777 token=secret"
    )
    assert "fake-token" not in text
    assert "12345" not in text
    assert "77777" not in text
    assert "/members/***77" in text
    assert redact_text("Synthetic member ID 12345") == "Synthetic member ID ***45"

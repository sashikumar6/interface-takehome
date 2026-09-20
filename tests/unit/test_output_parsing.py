from decimal import Decimal

import pytest

from computer_use.domain.models import OutputSpec, ValueType
from computer_use.replay.engine import ReplayEngine


def test_currency_decimal_output_parser() -> None:
    spec = OutputSpec(
        name="balance",
        type=ValueType.DECIMAL,
        description="balance",
        source="balance_text",
        parser="currency_decimal",
    )
    assert ReplayEngine._parse_output(spec, "Current balance: $1,234.56") == Decimal("1234.56")
    with pytest.raises(ValueError, match="currency decimal"):
        ReplayEngine._parse_output(spec, "not available")

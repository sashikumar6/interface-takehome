from pathlib import Path

import pytest

from computer_use.replay.catalog import FileCapabilityCatalog


def test_catalog_resolves_capability_by_identity_and_version() -> None:
    catalog = FileCapabilityCatalog(Path("artifacts/examples"))
    latest = catalog.get("lookup_member_balance")
    exact = catalog.get("lookup_member_balance", "1.0.0")
    assert latest == exact


def test_catalog_reports_unknown_capability() -> None:
    catalog = FileCapabilityCatalog(Path("artifacts/examples"))
    with pytest.raises(LookupError, match="was not found"):
        catalog.get("does_not_exist")

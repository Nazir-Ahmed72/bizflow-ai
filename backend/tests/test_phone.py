import pytest

from app.phone import normalize_phone


@pytest.mark.parametrize(
    "raw",
    ["+92 300 1234567", "+923001234567", "0300-1234567", "03001234567", "923001234567", "0092 300 1234567", " +92-300-123-4567 "],
)
def test_normalizes_to_e164(raw):
    assert normalize_phone(raw) == "+923001234567"


def test_unknown_number_still_normalizes():
    assert normalize_phone("+92 333 9876543") == "+923339876543"


@pytest.mark.parametrize("raw", ["", "abc", "12345", "300 1234567"])
def test_rejects_garbage(raw):
    with pytest.raises(ValueError):
        normalize_phone(raw)

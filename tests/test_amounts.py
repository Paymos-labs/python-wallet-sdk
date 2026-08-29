import pytest
from paymos._amounts import parse_units, format_units

@pytest.mark.parametrize("human,dec,raw", [("100","6","100000000"),("0.5","6","500000"),("1","18","1000000000000000000"),("0","6","0")])
def test_parse_units(human, dec, raw):
    assert parse_units(human, int(dec)) == raw

def test_parse_units_rejects_excess_fractional():
    with pytest.raises(ValueError):
        parse_units("1.1234567", 6)   # 7 fractional digits > 6

def test_parse_units_rejects_garbage():
    for bad in ["", "abc", "-1", "1.2.3", "1e3"]:
        with pytest.raises(ValueError):
            parse_units(bad, 6)

def test_format_units_roundtrips():
    assert format_units("100000000", 6) == "100"
    assert format_units("500000", 6) == "0.5"

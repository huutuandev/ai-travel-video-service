import pytest

from app.services.text_normalization import normalize_vietnamese


@pytest.mark.parametrize(("original", "spoken"), [
    ("2.990.000 VNĐ", "hai triệu chín trăm chín mươi nghìn đồng"),
    ("Giảm 20%", "Giảm hai mươi phần trăm"),
    ("Tour 3N2Đ", "Tour ba ngày hai đêm"),
    ("TP. Đà Nẵng", "thành phố Đà Nẵng"),
    ("20/12/2026", "ngày hai mươi tháng mười hai năm hai nghìn không trăm hai mươi sáu"),
    (
        "Đi ngày 20/12/2026.",
        "Đi ngày hai mươi tháng mười hai năm hai nghìn không trăm hai mươi sáu.",
    ),
    ("5 km", "năm ki-lô-mét"),
    ("2,05 kg", "hai phẩy không năm ki-lô-gam"),
    ("Mã HN-205", "Mã HN-205"),
    ("Gọi 0901234567", "Gọi không chín không một hai ba bốn năm sáu bảy"),
    ("  Đa\u0300  Nẵng ", "Đà Nẵng"),
])
def test_vietnamese_normalization(original, spoken):
    assert normalize_vietnamese(original) == spoken


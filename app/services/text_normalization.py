import re
from datetime import date
from unicodedata import normalize

DIGITS = "không một hai ba bốn năm sáu bảy tám chín".split()
SCALES = ("", "nghìn", "triệu", "tỷ", "nghìn tỷ")
TOKENS = re.compile(
    r"(?P<date>\b\d{1,2}/\d{1,2}/\d{4}\b)"
    r"|(?P<tour>\b\d{1,2}N\d{1,2}(?:Đ|D)\b)"
    r"|(?P<phone>(?<![\w\d])(?:\+84|0)(?:[ .-]?\d){8,10}(?!\w))"
    r"|(?P<code>\b(?=[\w-]*[a-z])(?=[\w-]*\d)[\w]+(?:-[\w]+)*\b)"
    r"|(?P<number>[+-]?(?:\d{1,3}(?:\.\d{3})+(?:,\d+)?|\d+(?:[.,]\d+)?))"
    r"(?:\s*(?P<unit>VNĐ|VND|đồng|₫|đ(?!\w)|%|km\b|kg\b|m\b))?",
    re.IGNORECASE,
)
UNITS = {"vnđ": "đồng", "vnd": "đồng", "đ": "đồng", "₫": "đồng", "đồng": "đồng",
         "%": "phần trăm", "km": "ki-lô-mét", "kg": "ki-lô-gam", "m": "mét"}


def digit_words(value):
    return " ".join(DIGITS[int(char)] for char in value if char.isdigit())


def integer_words(value):
    if not value or len(value) > 15 or value.startswith("0") and len(value) > 1:
        return digit_words(value)
    number = int(value)
    if not number:
        return DIGITS[0]
    groups = []
    while number:
        number, part = divmod(number, 1000)
        groups.append(part)
    result = []
    for index in reversed(range(len(groups))):
        part = groups[index]
        if not part:
            continue
        hundreds, remainder = divmod(part, 100)
        tens, units = divmod(remainder, 10)
        if hundreds or index < len(groups) - 1:
            result.extend((DIGITS[hundreds], "trăm"))
            if not tens and units:
                result.append("lẻ")
        if tens:
            result.extend(("mười",) if tens == 1 else (DIGITS[tens], "mươi"))
        if units:
            word = (
                "mốt" if units == 1 and tens > 1 else "lăm" if units == 5 and tens
                else DIGITS[units]
            )
            result.append(word)
        if index:
            result.append(SCALES[index])
    return " ".join(result)


def spoken_number(value):
    sign = {"-": "âm ", "+": "cộng "}.get(value[:1], "")
    value = value.lstrip("+-")
    if re.fullmatch(r"\d{1,3}(?:\.\d{3})+(?:,\d+)?", value):
        integer, _, fraction = value.replace(".", "").partition(",")
    else:
        integer, _, fraction = value.replace(".", ",", 1).partition(",")
    return sign + integer_words(integer) + (f" phẩy {digit_words(fraction)}" if fraction else "")


def normalize_vietnamese(text):
    text = normalize("NFC", text)
    text = re.sub(r"\bTP\.(?=\s|[A-ZÀ-Ỹ])\s*", "thành phố ", text, flags=re.IGNORECASE)

    def convert(match):
        if match["date"]:
            day, month, year = match["date"].split("/")
            try:
                date(int(year), int(month), int(day))
            except ValueError:
                return match[0]
            spoken = "{} tháng {} năm {}".format(
                *(integer_words(str(int(value))) for value in (day, month, year))
            )
            prefix = text[:match.start()].rstrip().casefold()
            return spoken if prefix.split()[-1:] == ["ngày"] else f"ngày {spoken}"
        if match["tour"]:
            days, nights = re.findall(r"\d+", match["tour"])
            return f"{integer_words(days)} ngày {integer_words(nights)} đêm"
        if match["phone"]:
            return ("cộng " if match[0].startswith("+") else "") + digit_words(match[0])
        if match["code"]:
            return match[0]
        unit = UNITS.get((match["unit"] or "").lower(), "")
        return " ".join(filter(None, (spoken_number(match["number"]), unit)))

    return " ".join(TOKENS.sub(convert, text).split())


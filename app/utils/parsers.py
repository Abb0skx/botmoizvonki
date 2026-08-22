import re
from urllib.parse import unquote


_URL_RE = re.compile(r"https?://[^\s<>]+", re.I)
_PHONE_LABEL_RE = re.compile(
    r"(?im)^(?:телефон|номер(?:\s+клиента)?|phone|tel)\s*[:\-]?\s*(.+)$"
)
_PHONE_RE = re.compile(
    r"(?<!\d)(?:"
    r"\+?998[\s()\-]*(?:\d[\s()\-]*){9}"
    r"|(?:20|33|50|55|70|71|72|73|74|75|76|77|78|79|80|81|88|89|90|91|93|94|95|97|98|99)"
    r"[\s()\-]*(?:\d[\s()\-]*){7}"
    r")(?!\d)"
)


def normalize_phone(value: str) -> str:
    digits = re.sub(r"\D", "", value)
    if len(digits) == 9:
        digits = "998" + digits
    if len(digits) != 12 or not digits.startswith("998"):
        raise ValueError("Введите узбекский номер из 9 цифр или с кодом +998")
    return "+" + digits


def display_phone(value: str) -> str:
    digits = re.sub(r"\D", "", value)
    return f"+{digits[:3]} {digits[3:5]} {digits[5:8]} {digits[8:10]} {digits[10:12]}"


def parse_amount(value: str) -> tuple[int | None, int | None]:
    clean = value.strip().lower()
    if not clean or len(clean) > 100:
        raise ValueError("Введите сумму, например: 100$ 1920000")
    if re.search(r"-\s*\d", clean):
        raise ValueError("Сумма не может быть отрицательной")
    usd_values: list[int] = []
    uzs_values: list[int] = []
    amount_pattern = r"\d(?:[\d.,]|[ \t](?=\d))*"

    def take_usd(match: re.Match) -> str:
        usd_values.append(int(re.sub(r"\D", "", match.group(1))))
        return " "

    def take_uzs(match: re.Match) -> str:
        uzs_values.append(int(re.sub(r"\D", "", match.group(1))))
        return " "

    # A currency marker always wins. Horizontal whitespace is allowed inside
    # an amount, but a newline never joins a model number with the price.
    clean = re.sub(rf"({amount_pattern})[ \t]*\$", take_usd, clean)
    clean = re.sub(
        rf"({amount_pattern})[ \t]*(?:сум|so['’]?m|uzs)",
        take_uzs,
        clean,
        flags=re.I,
    )

    candidates: list[int] = []
    for raw in re.findall(amount_pattern, clean):
        groups = re.findall(r"\d+", raw)
        if len(groups) > 1 and all(len(group) == 3 for group in groups[1:]):
            candidates.append(int("".join(groups)))
        else:
            candidates.extend(int(group) for group in groups)

    usd = usd_values[-1] if usd_values else None
    uzs = uzs_values[-1] if uzs_values else None
    for number in candidates:
        if number > 9_000:
            if not uzs_values:
                uzs = number
        elif not usd_values:
            usd = number
    if (usd is None or usd <= 0) and (uzs is None or uzs <= 0):
        raise ValueError("Введите положительную сумму, например: 100$ 1920000")
    if usd is not None and usd <= 0:
        raise ValueError("Сумма в долларах должна быть больше нуля")
    if uzs is not None and uzs <= 0:
        raise ValueError("Сумма в сумах должна быть больше нуля")
    return usd, uzs


def parse_order_details(value: str) -> dict[str, str | int | None]:
    """Extract phone, amount and a map URL from one free-form message."""
    if not value.strip() or len(value) > 4096:
        raise ValueError("Сообщение пустое или слишком длинное")

    result: dict[str, str | int | None] = {}
    remaining = value

    url_match = _URL_RE.search(remaining)
    if url_match:
        location_url = url_match.group(0).rstrip(".,;:!?)>]}")
        result["location_url"] = location_url
        remaining = remaining[:url_match.start()] + " " + remaining[url_match.end():]

    phone_match = None
    phone_span = None
    labelled = _PHONE_LABEL_RE.search(remaining)
    if labelled:
        phone_match = _PHONE_RE.search(labelled.group(1))
        if phone_match:
            phone_span = (
                labelled.start(1) + phone_match.start(),
                labelled.start(1) + phone_match.end(),
            )
    if phone_match is None:
        phone_match = _PHONE_RE.search(remaining)
        if phone_match:
            phone_span = phone_match.span()
    if phone_match:
        try:
            result["client_phone"] = normalize_phone(phone_match.group(0))
        except ValueError:
            pass
        else:
            start, end = phone_span
            remaining = remaining[:start] + " " + remaining[end:]

    if re.search(r"\d", remaining):
        try:
            usd, uzs = parse_amount(remaining)
        except ValueError:
            pass
        else:
            result["amount_usd"] = usd
            result["amount_uzs"] = uzs

    return result


def parse_location_url(url: str) -> tuple[float | None, float | None, str]:
    url = url.strip()
    if len(url) > 2048 or not re.match(r"https?://[^\s]+$", url, re.I):
        raise ValueError("Отправьте геолокацию или ссылку на карту")
    decoded = unquote(url)
    patterns = [
        (r"[?&]ll=([\d.-]+),([\d.-]+)", "lonlat"),
        (r"[?&]pt=([\d.-]+),([\d.-]+)", "lonlat"),
        (r"[?&]whatshere\[point\]=([\d.-]+),([\d.-]+)", "lonlat"),
        (r"[?&]q=([\d.-]+),([\d.-]+)", "latlon"),
        (r"[?&]rtext=(?:[^&~]*~)?([\d.-]+),([\d.-]+)", "latlon"),
        (r"/@([\d.-]+),([\d.-]+)", "latlon"),
        (r"!3d([\d.-]+)!4d([\d.-]+)", "latlon"),
    ]
    for pattern, orientation in patterns:
        match = re.search(pattern, decoded, re.I)
        if match:
            first, second = map(float, match.groups())
            latitude, longitude = (second, first) if orientation == "lonlat" else (first, second)
            if -90 <= latitude <= 90 and -180 <= longitude <= 180:
                return latitude, longitude, url
    return None, None, url

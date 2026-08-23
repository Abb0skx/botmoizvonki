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
_AMOUNT_PATTERN = r"(?:\d{1,3}(?:[ \t.,]\d{3})+|\d+)"
_USD_MARKER = r"(?:\$|usd\b|доллар(?:а|ов)?\b|дол(?:л)?\.?(?!\w))"
_UZS_MARKER = r"(?:uzs\b|с[уў]м(?:а|ов)?\b|so['ʻ’`]?m\b)"


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

    def take_usd(match: re.Match) -> str:
        usd_values.append(int(re.sub(r"\D", "", match.group(1))))
        return " "

    def take_uzs(match: re.Match) -> str:
        uzs_values.append(int(re.sub(r"\D", "", match.group(1))))
        return " "

    # A currency marker always wins. The start boundary prevents digits that
    # belong to a model name (e.g. ``A56 375$``) from joining the price.
    # A grouped amount only accepts proper three-digit groups, so
    # ``100 1 920 000 сум`` is parsed as USD 100 + UZS 1,920,000.
    explicit_patterns = (
        (rf"(?<!\w)({_AMOUNT_PATTERN})[ \t]*{_USD_MARKER}", take_usd),
        (rf"(?<!\w){_USD_MARKER}[ \t:]*({_AMOUNT_PATTERN})(?!\w)", take_usd),
        (rf"(?<!\w)({_AMOUNT_PATTERN})[ \t]*{_UZS_MARKER}", take_uzs),
        (rf"(?<!\w){_UZS_MARKER}[ \t:]*({_AMOUNT_PATTERN})(?!\w)", take_uzs),
    )
    for pattern, callback in explicit_patterns:
        clean = re.sub(pattern, callback, clean, flags=re.I)

    candidates: list[int] = []
    for match in re.finditer(rf"(?<!\w){_AMOUNT_PATTERN}(?!\w)", clean):
        candidates.append(int(re.sub(r"\D", "", match.group(0))))

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


def parse_order_details(value: str) -> dict[str, str | int | None | list[str]]:
    """Extract phones, amount and up to two map URLs from free-form text.

    ``client_phone`` remains the primary phone for backwards compatibility;
    ``client_phones`` contains every distinct recognized phone in priority
    order (labelled phone lines first, then other occurrences).
    """
    if not value.strip() or len(value) > 4096:
        raise ValueError("Сообщение пустое или слишком длинное")

    result: dict[str, str | int | None | list[str]] = {}
    remaining = value

    location_urls: list[str] = []
    url_spans: list[tuple[int, int]] = []
    for url_match in _URL_RE.finditer(remaining):
        location_url = url_match.group(0).rstrip(".,;:!?)>]}")
        if location_url not in location_urls:
            location_urls.append(location_url)
        url_spans.append((url_match.start(), url_match.end()))
    if location_urls:
        result["location_url"] = location_urls[0]
        result["location_urls"] = location_urls[:2]
        for start, end in reversed(url_spans):
            remaining = remaining[:start] + " " + remaining[end:]

    phone_spans: set[tuple[int, int]] = set()
    phones: list[str] = []

    def remember_phone(match: re.Match, offset: int = 0) -> None:
        # ``_PHONE_RE`` permits whitespace between digits and can therefore
        # include a trailing newline. Do not let that make the labelled and
        # global scans look like two overlapping phone occurrences.
        raw_phone = match.group(0)
        trimmed_phone = raw_phone.rstrip()
        span = (offset + match.start(), offset + match.start() + len(trimmed_phone))
        phone_spans.add(span)
        try:
            phone = normalize_phone(trimmed_phone)
        except ValueError:
            return
        if phone not in phones:
            phones.append(phone)

    # Preserve the old preference for explicitly labelled phone lines while
    # allowing more than one phone on the same or on separate lines.
    for labelled in _PHONE_LABEL_RE.finditer(remaining):
        for phone_match in _PHONE_RE.finditer(labelled.group(1)):
            remember_phone(phone_match, labelled.start(1))
    for phone_match in _PHONE_RE.finditer(remaining):
        if phone_match.span() not in phone_spans:
            remember_phone(phone_match)

    if phones:
        result["client_phone"] = phones[0]
        result["client_phones"] = phones
        for start, end in sorted(phone_spans, reverse=True):
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

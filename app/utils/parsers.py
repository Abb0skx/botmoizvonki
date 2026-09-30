import re
from dataclasses import dataclass
from html import unescape
from urllib.parse import parse_qs, unquote, urlsplit


_URL_RE = re.compile(r"https?://[^\s<>]+", re.I)
_COORDINATE_PAIR_RE = re.compile(
    r"(?<![\d.])([+\-]?\d{1,3}(?:\.\d{3,8}))\s*[,;]\s*"
    r"([+\-]?\d{1,3}(?:\.\d{3,8}))(?![\d.])"
)
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
_EXPLICIT_UZS_RE = re.compile(
    rf"(?<!\w)(?:{_AMOUNT_PATTERN})[ \t]*{_UZS_MARKER}"
    rf"|(?<!\w){_UZS_MARKER}[ \t:]*(?:{_AMOUNT_PATTERN})(?!\w)",
    re.I,
)
MAX_STORED_AMOUNT = (1 << 63) - 1
MAX_ORDER_USD = 100_000
MAX_ORDER_UZS = 2_000_000_000


def extract_text_coordinates(value: str) -> list[tuple[float, float]]:
    """Extract up to two decimal coordinate pairs outside HTTP links.

    Managers normally paste ``latitude, longitude``. A reversed
    ``longitude, latitude`` pair is also normalized when its ordering is
    unambiguous. Geographic bounds are checked by the order handler so it can
    return the existing Uzbekistan-specific error to the manager.
    """
    without_urls = _URL_RE.sub(" ", value or "")
    result: list[tuple[float, float]] = []
    for match in _COORDINATE_PAIR_RE.finditer(without_urls):
        first, second = float(match.group(1)), float(match.group(2))
        first_is_uzbek_longitude = 55 <= first <= 74
        second_is_uzbek_latitude = 37 <= second <= 46
        first_is_uzbek_latitude = 37 <= first <= 46
        second_is_uzbek_longitude = 55 <= second <= 74
        if (
            first_is_uzbek_longitude
            and second_is_uzbek_latitude
            and not (first_is_uzbek_latitude and second_is_uzbek_longitude)
        ) or (abs(first) > 90 and abs(second) <= 90):
            latitude, longitude = second, first
        else:
            latitude, longitude = first, second
        if not (-90 <= latitude <= 90 and -180 <= longitude <= 180):
            continue
        coordinates = (latitude, longitude)
        if coordinates not in result:
            result.append(coordinates)
        if len(result) == 2:
            break
    return result


@dataclass(frozen=True, slots=True)
class CourierCashAmounts:
    usd: int
    uzs: int
    is_handover: bool


_CASH_KEYWORD_RE = re.compile(r"(?<!\w)(?:касса|kassa|cash)(?!\w)", re.I)
_SIGNED_AMOUNT_PATTERN = rf"[+\-]?(?:\d{{1,3}}(?:[ \t.,]\d{{3}})+|\d+)"


def contains_cash_keyword(value: str) -> bool:
    return bool(_CASH_KEYWORD_RE.search(value or ""))


def has_courier_cash_context(value: str) -> bool:
    """Return whether a group message is intentionally about courier cash.

    Plain numbers are common in delivery groups (phones, order numbers and
    model names), so they must not become ledger entries by accident.  A cash
    message needs a keyword, a currency marker, or an explicit signed change
    amount such as ``40 -25000``.
    """
    clean = (value or "").replace("−", "-").replace("–", "-").replace("—", "-")
    if contains_cash_keyword(clean):
        return True
    if re.search(rf"{_USD_MARKER}|{_UZS_MARKER}", clean, flags=re.I):
        return True
    amounts = re.findall(rf"(?<!\w){_SIGNED_AMOUNT_PATTERN}(?!\w)", clean)
    return len(amounts) >= 2 and any(item.lstrip().startswith(("-", "+")) for item in amounts)


def looks_like_phone(value: str) -> bool:
    """Recognize a message whose entire useful content is one Uzbek phone."""
    clean = (value or "").strip()
    if not clean or re.search(r"[^\d+\s()\-]", clean):
        return False
    try:
        normalize_phone(clean)
    except ValueError:
        return False
    return True


def parse_courier_cash(value: str) -> CourierCashAmounts:
    """Parse a courier cash delta without weakening normal order-price rules.

    Explicit currency markers win. Unmarked absolute values above 9,000 are
    UZS; all smaller values are USD. For a normal receipt, negative values are
    change paid by the courier. A ``касса``/``kassa`` request accepts only
    positive amounts because its negative ledger delta is applied after an
    administrator confirms receipt.
    """
    clean = value.strip().lower().replace("−", "-").replace("–", "-").replace("—", "-")
    if not clean or len(clean) > 256:
        raise ValueError("Напишите сумму коротко. Например: 40 -25000")
    is_handover = bool(_CASH_KEYWORD_RE.search(clean))
    clean = _CASH_KEYWORD_RE.sub(" ", clean)
    # A separated minus is still a sign, never harmless punctuation. Also
    # split compact mixed input such as ``40-25000`` into two signed amounts.
    clean = re.sub(r"(?<=\d)([+\-])(?=\s*\d)", r" \1", clean)
    clean = re.sub(r"(?<!\w)([+\-])\s+(?=\d)", r"\1", clean)
    if re.search(r"\d+[.,]\d{1,2}(?!\d)", clean):
        raise ValueError("Копейки не поддерживаются. Введите целые суммы.")

    values: dict[str, list[int]] = {"usd": [], "uzs": []}

    def number(raw: str) -> int:
        sign = -1 if raw.lstrip().startswith("-") else 1
        return sign * int(re.sub(r"\D", "", raw))

    # Normalize the useful ``-$5`` shorthand before consuming explicit values.
    clean = re.sub(r"(?<!\w)([+\-])\s*\$", r"$\1", clean)
    explicit_patterns = (
        ("usd", rf"(?<!\w)(?P<amount>{_SIGNED_AMOUNT_PATTERN})[ \t]*{_USD_MARKER}"),
        ("usd", rf"(?<!\w){_USD_MARKER}[ \t:]*(?P<amount>{_SIGNED_AMOUNT_PATTERN})(?!\w)"),
        ("uzs", rf"(?<!\w)(?P<amount>{_SIGNED_AMOUNT_PATTERN})[ \t]*{_UZS_MARKER}"),
        ("uzs", rf"(?<!\w){_UZS_MARKER}[ \t:]*(?P<amount>{_SIGNED_AMOUNT_PATTERN})(?!\w)"),
    )
    for currency, pattern in explicit_patterns:
        def take(match: re.Match, *, target: str = currency) -> str:
            values[target].append(number(match.group("amount")))
            return " "

        clean = re.sub(pattern, take, clean, flags=re.I)

    unmarked: list[tuple[int, int, int]] = []
    for match in re.finditer(rf"(?<!\w){_SIGNED_AMOUNT_PATTERN}(?!\w)", clean):
        parsed = number(match.group(0))
        unmarked.append((match.start(), match.end(), parsed))
    for start, end, _ in reversed(unmarked):
        clean = clean[:start] + " " * (end - start) + clean[end:]
    for _, _, parsed in unmarked:
        currency = "uzs" if abs(parsed) > 9_000 else "usd"
        values[currency].append(parsed)

    # Labels are accepted, but never used to infer a missing sign.
    clean = re.sub(
        r"(?<!\w)(?:получил(?:а)?|получено|сдача|клиент|доставка)(?!\w)",
        " ",
        clean,
        flags=re.I,
    )
    if re.search(r"[\w$]", clean, flags=re.UNICODE):
        raise ValueError("Оставьте только суммы и валюту. Например: 40$ -25000 сум")
    if any(len(items) > 1 for items in values.values()):
        raise ValueError("Укажите не больше одной суммы в долларах и одной в сумах.")

    usd = values["usd"][0] if values["usd"] else 0
    uzs = values["uzs"][0] if values["uzs"] else 0
    if usd == 0 and uzs == 0:
        raise ValueError("Сумма не найдена. Например: 40 -25000")
    if abs(usd) > MAX_STORED_AMOUNT or abs(uzs) > MAX_STORED_AMOUNT:
        raise ValueError("Сумма слишком большая.")
    if is_handover and (usd < 0 or uzs < 0):
        raise ValueError("Для сдачи в кассу укажите положительную сумму.")
    return CourierCashAmounts(usd=usd, uzs=uzs, is_handover=is_handover)


_MAP_PROVIDER_ROOTS: dict[str, tuple[str, ...]] = {
    "google": (
        "google.com",
        "google.co.uz",
        "google.ru",
        "goo.gl",
        "share.google",
    ),
    "yandex": ("yandex.com", "yandex.ru", "yandex.uz", "yandex.kz"),
    "2gis": ("2gis.com", "2gis.ru", "2gis.uz", "2gis.kz"),
    "osm": ("openstreetmap.org", "osm.org"),
    "waze": ("waze.com",),
    "here": ("here.com",),
    "apple": ("maps.apple",),
}
_MAP_PROVIDER_EXACT_HOSTS = {
    "maps.app.goo.gl": "google",
    "maps.apple.com": "apple",
    "maps.apple": "apple",
}


def map_url_provider(url: str) -> str | None:
    """Return the supported provider for a safe HTTP(S) map URL."""
    try:
        parts = urlsplit(url.strip())
        port = parts.port
    except ValueError:
        return None
    if parts.scheme.casefold() not in {"http", "https"}:
        return None
    if parts.username is not None or parts.password is not None:
        return None
    if port not in {None, 80, 443}:
        return None
    host = (parts.hostname or "").rstrip(".").casefold()
    if not host:
        return None
    if host in _MAP_PROVIDER_EXACT_HOSTS:
        return _MAP_PROVIDER_EXACT_HOSTS[host]
    for provider, roots in _MAP_PROVIDER_ROOTS.items():
        if any(host == root or host.endswith(f".{root}") for root in roots):
            return provider
    return None


def extract_http_urls(value: str, maximum: int = 20) -> list[str]:
    """Extract distinct visible HTTP(S) links without interpreting their purpose."""
    result: list[str] = []
    for match in _URL_RE.finditer(value):
        url = match.group(0).rstrip(".,;:!?)>]}")
        if url and url not in result:
            result.append(url)
        if len(result) >= maximum:
            break
    return result


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
    clean = value.strip().lower().replace("−", "-").replace("–", "-").replace("—", "-")
    # Telegram keycap emoji (for example ``7️⃣`` used as a list number) are
    # presentation, not money.
    clean = re.sub(r"\d\ufe0f?\u20e3", " ", clean)
    if not clean or len(clean) > 100:
        raise ValueError("Введите сумму, например: 100$ 1920000")
    if re.search(r"-\s*\d", clean):
        raise ValueError("Сумма не может быть отрицательной")
    decimal_amount = (
        rf"(?:\d+[.,]\d{{1,2}}[ \t]*(?:{_USD_MARKER}|{_UZS_MARKER}))"
        rf"|(?:(?:{_USD_MARKER}|{_UZS_MARKER})[ \t:]*\d+[.,]\d{{1,2}})"
    )
    if re.search(decimal_amount, clean, flags=re.I):
        raise ValueError("Копейки не поддерживаются. Введите целую сумму")
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

    if len(usd_values) > 1 or len(uzs_values) > 1:
        raise ValueError("Укажите не больше одной суммы в долларах и одной в сумах")

    candidates: list[int] = []
    for match in re.finditer(rf"(?<!\w){_AMOUNT_PATTERN}(?!\w)", clean):
        candidates.append(int(re.sub(r"\D", "", match.group(0))))

    usd = usd_values[0] if usd_values else None
    uzs = uzs_values[0] if uzs_values else None
    for number in candidates:
        if number > 9_000:
            if uzs is not None:
                raise ValueError("Укажите не больше одной суммы в сумах")
            uzs = number
        else:
            if usd is not None:
                raise ValueError("Укажите не больше одной суммы в долларах")
            usd = number
    if (usd is None or usd <= 0) and (uzs is None or uzs <= 0):
        raise ValueError("Введите положительную сумму, например: 100$ 1920000")
    if usd is not None and usd <= 0:
        raise ValueError("Сумма в долларах должна быть больше нуля")
    if uzs is not None and uzs <= 0:
        raise ValueError("Сумма в сумах должна быть больше нуля")
    if usd is not None and usd > min(MAX_STORED_AMOUNT, MAX_ORDER_USD):
        raise ValueError(
            f"Сумма в долларах слишком большая. Максимум: {MAX_ORDER_USD:,}$".replace(",", " ")
        )
    if uzs is not None and uzs > min(MAX_STORED_AMOUNT, MAX_ORDER_UZS):
        raise ValueError(
            f"Сумма в сумах слишком большая. Максимум: {MAX_ORDER_UZS:,} сум".replace(",", " ")
        )
    return usd, uzs


def parse_order_details(
    value: str,
) -> dict[str, str | int | None | list[str] | list[tuple[float, float]]]:
    """Extract phones, amount and up to two map URLs from free-form text.

    ``client_phone`` remains the primary phone for backwards compatibility;
    ``client_phones`` contains every distinct recognized phone in priority
    order (labelled phone lines first, then other occurrences).
    """
    if not value.strip() or len(value) > 4096:
        raise ValueError("Сообщение пустое или слишком длинное")

    result: dict[
        str,
        str | int | None | list[str] | list[tuple[float, float]],
    ] = {}
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

    coordinate_matches = list(_COORDINATE_PAIR_RE.finditer(remaining))
    coordinates = extract_text_coordinates(remaining)
    if coordinates:
        result["location_coordinates"] = coordinates
        for match in reversed(coordinate_matches):
            remaining = remaining[:match.start()] + " " + remaining[match.end():]

    # An explicitly marked amount always wins over the visually ambiguous
    # nine-digit Uzbek phone form. Keep character offsets intact so phone spans
    # can still be removed from ``remaining`` below.
    phone_source = list(remaining)
    for amount_match in _EXPLICIT_UZS_RE.finditer(remaining):
        for index in range(amount_match.start(), amount_match.end()):
            if phone_source[index] not in "\r\n":
                phone_source[index] = " "
    phone_search = "".join(phone_source)

    phone_spans: set[tuple[int, int]] = set()
    labelled_phone_spans: set[tuple[int, int]] = set()
    phones: list[str] = []

    def phone_span(match: re.Match, offset: int = 0) -> tuple[int, int]:
        # ``_PHONE_RE`` permits whitespace between digits and can therefore
        # include a trailing newline. Do not let that make the labelled and
        # global scans look like two overlapping phone occurrences.
        raw_phone = match.group(0)
        trimmed_phone = raw_phone.rstrip()
        return (offset + match.start(), offset + match.start() + len(trimmed_phone))

    def remember_phone(match: re.Match, offset: int = 0) -> None:
        trimmed_phone = match.group(0).rstrip()
        span = phone_span(match, offset)
        phone_spans.add(span)
        try:
            phone = normalize_phone(trimmed_phone)
        except ValueError:
            return
        if phone not in phones:
            phones.append(phone)

    # Preserve the old preference for explicitly labelled phone lines while
    # allowing more than one phone on the same or on separate lines.
    for labelled in _PHONE_LABEL_RE.finditer(phone_search):
        for phone_match in _PHONE_RE.finditer(labelled.group(1)):
            remember_phone(phone_match, labelled.start(1))
            labelled_phone_spans.add(phone_span(phone_match, labelled.start(1)))
    # A full 998 number is unambiguously a phone. Bare nine-digit values are
    # ambiguous because they may also be an unmarked UZS amount. The order
    # supports at most two phones: when a labelled/full phone already exists,
    # keep the last remaining bare value for the amount; with three bare
    # values, keep the first two as phones and the last one as the amount.
    # Explicit ``Телефон:`` / currency markers always take precedence.
    definite_phone_ends = [end for _start, end in labelled_phone_spans]
    deferred_bare_phones: list[re.Match] = []
    for phone_match in _PHONE_RE.finditer(phone_search):
        span = phone_span(phone_match)
        digits = re.sub(r"\D", "", phone_match.group(0))
        has_country_code = len(digits) == 12 and digits.startswith("998")
        if span in phone_spans:
            if has_country_code and span[1] not in definite_phone_ends:
                definite_phone_ends.append(span[1])
            continue
        if not has_country_code:
            deferred_bare_phones.append(phone_match)
            continue
        remember_phone(phone_match)
        definite_phone_ends.append(span[1])

    has_explicit_amount = bool(
        re.search(rf"{_USD_MARKER}|{_UZS_MARKER}", remaining, flags=re.I)
    )
    if has_explicit_amount:
        bare_phone_count = len(deferred_bare_phones)
    elif definite_phone_ends:
        # A seven/eight-digit amount cannot be mistaken for an Uzbek phone.
        # If such a separate number is present, all remaining nine-digit
        # candidates are phones. Otherwise reserve the last ambiguous
        # nine-digit value for an unmarked UZS amount.
        non_ambiguous_source = list(phone_search)
        for start, end in phone_spans:
            for index in range(start, end):
                if non_ambiguous_source[index] not in "\r\n":
                    non_ambiguous_source[index] = " "
        for phone_match in deferred_bare_phones:
            start, end = phone_span(phone_match)
            for index in range(start, end):
                if non_ambiguous_source[index] not in "\r\n":
                    non_ambiguous_source[index] = " "
        has_other_amount = bool(
            re.search(
                rf"(?<!\w){_AMOUNT_PATTERN}(?!\w)",
                "".join(non_ambiguous_source),
            )
        )
        available = (
            len(deferred_bare_phones)
            if has_other_amount
            else max(0, len(deferred_bare_phones) - 1)
        )
        bare_phone_count = available
    else:
        bare_phone_count = min(max(0, 2 - len(phones)), 2, len(deferred_bare_phones))
    for phone_match in deferred_bare_phones[:bare_phone_count]:
        remember_phone(phone_match)

    if not labelled_phone_spans:
        ordered_phones: list[str] = []
        for start, end in sorted(phone_spans):
            try:
                phone = normalize_phone(phone_search[start:end])
            except ValueError:
                continue
            if phone not in ordered_phones:
                ordered_phones.append(phone)
        phones = ordered_phones

    if len(phones) > 2:
        raise ValueError("Укажите не больше двух номеров клиента")

    if phones:
        result["client_phone"] = phones[0]
        result["client_phones"] = phones
        for start, end in sorted(phone_spans, reverse=True):
            remaining = remaining[:start] + " " + remaining[end:]

    if re.search(r"\d", remaining):
        try:
            usd, uzs = parse_amount(remaining)
        except ValueError:
            if re.search(
                rf"{_USD_MARKER}|{_UZS_MARKER}|(?<!\w){_AMOUNT_PATTERN}(?!\w)",
                remaining,
                flags=re.I,
            ):
                raise
        else:
            result["amount_usd"] = usd
            result["amount_uzs"] = uzs

    return result


def parse_location_url(url: str) -> tuple[float | None, float | None, str]:
    url = url.strip()
    if len(url) > 8192 or not re.match(r"https?://[^\s]+$", url, re.I):
        raise ValueError("Отправьте геолокацию или ссылку на карту")
    provider = map_url_provider(url)
    if provider is None:
        return None, None, url

    decoded = unescape(url)
    # Redirect targets are sometimes nested in a percent-encoded query value.
    for _ in range(2):
        next_value = unquote(decoded)
        if next_value == decoded:
            break
        decoded = next_value
    parts = urlsplit(decoded)
    query = {
        key.casefold(): values
        for key, values in parse_qs(parts.query, keep_blank_values=True).items()
    }

    def pair(value: str, orientation: str) -> tuple[float, float] | None:
        match = re.search(
            r"(?<![\d.])-?\d{1,3}(?:\.\d+)?\s*[,;]\s*-?\d{1,3}(?:\.\d+)?(?![\d.])",
            value,
        )
        if not match:
            return None
        first_text, second_text = re.split(r"\s*[,;]\s*", match.group(0), maxsplit=1)
        first, second = float(first_text), float(second_text)
        latitude, longitude = (second, first) if orientation == "lonlat" else (first, second)
        if -90 <= latitude <= 90 and -180 <= longitude <= 180:
            return latitude, longitude
        return None

    def value_for(*names: str) -> list[str]:
        values: list[str] = []
        for name in names:
            values.extend(query.get(name.casefold(), []))
        return values

    def first_pair(values: list[str], orientation: str) -> tuple[float, float] | None:
        for value in values:
            result = pair(value, orientation)
            if result is not None:
                return result
        return None

    coordinates: tuple[float, float] | None = None
    if provider == "google":
        coordinates = first_pair(value_for("destination", "daddr", "query", "q"), "latlon")
        is_directions = "/maps/dir/" in parts.path.casefold()
        if coordinates is None and is_directions:
            route_path = parts.path.split("/@", 1)[0].split("/data=", 1)[0]
            route_pairs = re.findall(
                r"-?\d{1,2}(?:\.\d+)?\s*,\s*-?\d{1,3}(?:\.\d+)?",
                route_path,
            )
            if route_pairs:
                coordinates = pair(route_pairs[-1], "latlon")
        if coordinates is None:
            # Exact POI coordinates win over the /@ camera centre.
            exact_matches = re.findall(
                r"!3d(-?\d{1,2}(?:\.\d+)?)!4d(-?\d{1,3}(?:\.\d+)?)",
                decoded,
                re.I,
            )
            if exact_matches:
                exact = exact_matches[-1] if is_directions else exact_matches[0]
                coordinates = pair(",".join(exact), "latlon")
        if coordinates is None and is_directions:
            legacy_route = re.findall(
                r"!2d(-?\d{1,3}(?:\.\d+)?)!3d(-?\d{1,2}(?:\.\d+)?)",
                decoded,
                re.I,
            )
            if legacy_route:
                longitude_text, latitude_text = legacy_route[-1]
                coordinates = pair(f"{latitude_text},{longitude_text}", "latlon")
        if coordinates is None:
            camera = re.search(
                r"/@(-?\d{1,2}(?:\.\d+)?),(-?\d{1,3}(?:\.\d+)?)",
                decoded,
                re.I,
            )
            if camera:
                coordinates = pair(",".join(camera.groups()), "latlon")
        if coordinates is None:
            coordinates = first_pair(value_for("center", "ll"), "latlon")

    elif provider == "yandex":
        coordinates = first_pair(value_for("whatshere[point]", "pt"), "lonlat")
        if coordinates is None:
            # rtext is origin~destination and uses latitude,longitude.
            for route in value_for("rtext"):
                route_pairs = re.findall(
                    r"-?\d{1,2}(?:\.\d+)?\s*,\s*-?\d{1,3}(?:\.\d+)?",
                    route,
                )
                if route_pairs:
                    coordinates = pair(route_pairs[-1], "latlon")
                    if coordinates is not None:
                        break
        if coordinates is None:
            coordinates = first_pair(value_for("ll"), "lonlat")

    elif provider == "apple":
        coordinates = first_pair(
            value_for("coordinate", "destination", "daddr", "ll", "sll", "near", "center", "q"),
            "latlon",
        )

    elif provider == "waze":
        coordinates = first_pair(value_for("ll"), "latlon")
        if coordinates is None:
            match = re.search(
                r"(?:^|[?&])to=ll\.(-?\d{1,2}(?:\.\d+)?),(-?\d{1,3}(?:\.\d+)?)",
                decoded,
                re.I,
            )
            if match:
                coordinates = pair(",".join(match.groups()), "latlon")

    elif provider == "osm":
        lat_values, lon_values = value_for("mlat"), value_for("mlon")
        if lat_values and lon_values:
            coordinates = pair(f"{lat_values[0]},{lon_values[0]}", "latlon")
        if coordinates is None:
            for route in value_for("route"):
                route_pairs = re.findall(
                    r"-?\d{1,2}(?:\.\d+)?\s*[,;]\s*-?\d{1,3}(?:\.\d+)?",
                    route,
                )
                if len(route_pairs) >= 2:
                    coordinates = pair(route_pairs[-1], "latlon")
                    if coordinates is not None:
                        break
        if coordinates is None:
            viewport = re.search(
                r"(?:#|[?&])map=\d+(?:\.\d+)?/(-?\d{1,2}(?:\.\d+)?)/(-?\d{1,3}(?:\.\d+)?)",
                decoded,
                re.I,
            )
            if viewport:
                coordinates = pair(",".join(viewport.groups()), "latlon")

    elif provider == "2gis":
        # Object/path coordinates are more precise than viewport parameter m.
        route_path = re.search(r"/directions/points/(.+)$", parts.path, re.I)
        if route_path:
            route_pairs = re.findall(
                r"-?\d{1,3}(?:\.\d+)?\s*,\s*-?\d{1,2}(?:\.\d+)?",
                route_path.group(1),
            )
            if route_pairs:
                coordinates = pair(route_pairs[-1], "lonlat")
        path_match = None
        if coordinates is None:
            path_match = re.search(
                r"/(?:geo|firm)/[^/?#]*/(-?\d{1,3}(?:\.\d+)?),(-?\d{1,2}(?:\.\d+)?)(?:[/?#]|$)",
                parts.path,
                re.I,
            )
        if coordinates is None and path_match is None:
            path_match = re.search(
                r"/geo/(-?\d{1,3}(?:\.\d+)?),(-?\d{1,2}(?:\.\d+)?)(?:[/?#]|$)",
                parts.path,
                re.I,
            )
        if path_match:
            coordinates = pair(",".join(path_match.groups()), "lonlat")
        if coordinates is None:
            state = re.search(
                r"(?:^|[=/])center/(-?\d{1,3}(?:\.\d+)?),(-?\d{1,2}(?:\.\d+)?)(?:/|$)",
                decoded,
                re.I,
            )
            if state:
                coordinates = pair(",".join(state.groups()), "lonlat")
        if coordinates is None:
            coordinates = first_pair(value_for("m"), "lonlat")

    elif provider == "here":
        coordinates = first_pair(value_for("map", "destination"), "latlon")
        if coordinates is None:
            path_match = re.search(
                r"/l/(-?\d{1,2}(?:\.\d+)?),(-?\d{1,3}(?:\.\d+)?)",
                parts.path,
                re.I,
            )
            if path_match:
                coordinates = pair(",".join(path_match.groups()), "latlon")

    if coordinates is not None:
        return coordinates[0], coordinates[1], url
    return None, None, url

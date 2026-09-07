from telegram_business.request_inputs import (
    location_from_message,
    masked_phone,
    missing_request_fields,
    normalize_phone,
    phone_from_message,
    phones_from_message,
    request_summary,
)


def request(**changes):
    value = {
        "exact_model": "Apple Watch Series 10",
        "model_url": "https://t.me/Texnikach_Watch/100",
        "option_kind": "size",
        "option_value": "46 mm",
        "color": "Black",
        "color_any": 0,
        "fulfillment_method": "delivery",
        "phone": "+998901234567",
        "location_url": "https://www.google.com/maps?q=41.300000,69.200000",
        "address": "Чиланзар 10",
        "database_price": "5000000",
        "selection_fields": '{"attribute_required":true,"color_required":true}',
    }
    value.update(changes)
    return value


def test_phone_is_normalized_but_plain_card_like_number_is_not():
    assert normalize_phone("90 123 45 67") == "+998901234567"
    assert normalize_phone("+998 (90) 123-45-67") == "+998901234567"
    assert normalize_phone("8600123456789012") is None
    assert masked_phone("+998901234567").endswith("67")


def test_native_contact_is_accepted_only_when_it_belongs_to_sender():
    own = {
        "from": {"id": 42},
        "contact": {"user_id": 42, "phone_number": "+998 90 123 45 67"},
    }
    foreign = {
        "from": {"id": 42},
        "contact": {"user_id": 99, "phone_number": "+998 90 123 45 67"},
    }
    unverifiable = {
        "from": {"id": 42},
        "contact": {"phone_number": "+998 90 123 45 67"},
    }
    assert phone_from_message(own) == ("+998901234567", "telegram_contact")
    assert phone_from_message(foreign) == (None, None)
    assert phone_from_message(unverifiable) == (None, None)


def test_multiple_explicit_phones_are_bounded_deduplicated_evidence():
    detected = phones_from_message(
        {"from": {"id": 42}},
        "Телефоны: +998 90 123 45 67 и +998-93-765-43-21; "
        "дубль +998901234567",
    )
    assert [(item.phone, item.source, item.owner_verified) for item in detected] == [
        ("+998901234567", "typed", False),
        ("+998937654321", "typed", False),
    ]

    local = phones_from_message(
        {}, "Телефоны: 90 123 45 67, 93 765 43 21",
    )
    assert [item.phone for item in local] == [
        "+998901234567", "+998937654321",
    ]


def test_phone_evidence_verifies_only_senders_own_native_contact():
    own = phones_from_message({
        "from": {"id": 42},
        "contact": {"user_id": 42, "phone_number": "+998 90 123 45 67"},
    })
    foreign = phones_from_message({
        "from": {"id": 42},
        "contact": {"user_id": 99, "phone_number": "+998 90 123 45 67"},
    })
    assert len(own) == 1 and own[0].owner_verified is True
    assert foreign == ()


def test_card_like_text_never_becomes_phone_evidence():
    assert phones_from_message({}, "карта: 8600 1234 5678 9012") == ()
    assert phones_from_message({}, "номер карты +8600123456789012") == ()


def test_explicit_address_step_accepts_short_address_without_opening_links():
    parsed = location_from_message({}, "Чиланзар 10", expected=True)
    assert parsed is not None
    assert parsed.address == "Чиланзар 10"
    assert parsed.url.startswith("https://www.google.com/maps/search/")
    assert location_from_message({}, "https://evil.example/maps", expected=True) is None


def test_native_location_is_saved_as_safe_link_and_outside_is_only_a_hint():
    parsed = location_from_message(
        {"location": {"latitude": 39.65, "longitude": 66.96}}, "", expected=True,
    )
    assert parsed is not None
    assert parsed.outside_tashkent
    assert parsed.url == "https://www.google.com/maps?q=39.650000,66.960000"


def test_pickup_requires_neither_phone_nor_location_and_delivery_only_location():
    pickup = request(
        fulfillment_method="pickup", phone=None, location_url=None, address=None,
    )
    assert missing_request_fields(pickup) == ()
    delivery = request(phone=None, location_url=None, address=None)
    assert missing_request_fields(delivery) == ("location",)
    delivery_with_location = request(
        phone=None, location_url=None, address="Tashkent, Chilanzar 10",
    )
    assert missing_request_fields(delivery_with_location) == ()


def test_summary_uses_size_and_never_calls_request_an_order():
    ru = request_summary(request(), "ru")
    uz = request_summary(request(), "uz")
    assert "Размер: 46 mm" in ru
    assert "Самовывоз" not in ru
    assert "O‘lcham: 46 mm" in uz
    assert "+998901234567" not in ru

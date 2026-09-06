from decimal import Decimal
import time

import pytest

from telegram_business.product_wizard import (
    build_attribute_step,
    build_color_step,
    build_delivery_location_step,
    build_delivery_phone_step,
    build_edit_menu,
    build_first_product_step,
    build_fulfillment_step,
    build_grouped_model_step,
    build_model_step,
    build_pickup_contact_step,
    build_review_step,
    detect_attribute_kind,
    variants_for_choice,
    ButtonSpec,
    WizardReviewData,
)
from telegram_business.products import (
    ExistingGoogleProductRepository,
    ProductMatch,
    ProductVariant,
)


def variant(
    model: str,
    memory: str = "",
    color: str = "",
    *,
    product_id: int = 1,
    url: str | None = None,
) -> ProductVariant:
    return ProductVariant(
        model=model,
        memory=memory,
        color=color,
        price_uzs=Decimal("1000000"),
        product_id=product_id,
        url=url,
    )


def test_ambiguous_model_step_is_capped_linked_and_callback_safe():
    models = (
        "Phone <A>", "Phone B", "Phone C", "Phone D", "Phone E", "Phone F",
    )
    match = ProductMatch(
        "ambiguous",
        models,
        (),
        (
            (models[0], "https://t.me/Texnikach_Phone/100"),
            (models[1], "https://evil.test/product"),
        ),
    )

    step = build_model_step(match, "ru")

    assert step is not None
    assert step.code == "model"
    assert [choice.value for choice in step.choices] == list(models[:5])
    assert "Phone &lt;A&gt;" in step.text
    assert '<a href="https://t.me/Texnikach_Phone/100">' in step.text
    assert "evil.test" not in step.text
    assert all(len(row) == 1 for row in step.keyboard[:5])
    assert all(row[0].action == "select_model" for row in step.keyboard[:5])
    assert all(row[0].url is None for row in step.keyboard[:5])
    assert "<A>" in step.keyboard[0][0].text

    markup = step.inline_keyboard(
        lambda action, choice_id: f"w:{action}:{choice_id or '-'}"
    )
    callbacks = [
        button["callback_data"]
        for row in markup["inline_keyboard"]
        for button in row
        if "callback_data" in button
    ]
    assert callbacks
    assert all(len(value.encode("utf-8")) <= 64 for value in callbacks)


def test_memory_step_uses_only_real_distinct_values_and_filters_exactly():
    variants = (
        variant("Phone", "8/256Gb", "Black", product_id=1),
        variant("Phone", "8 / 256 GB", "White", product_id=2),
        variant("Phone", "12/512Gb", "Black", product_id=3),
    )

    step = build_attribute_step(variants, "bi")

    assert step is not None
    assert step.code == "memory"
    assert [choice.value for choice in step.choices] == ["8/256Gb", "12/512Gb"]
    assert "Выберите память" in step.text
    assert "xotirani tanlang" in step.text.casefold()
    selected = variants_for_choice(variants, step, step.choices[0].choice_id)
    assert {item.product_id for item in selected} == {1, 2}


@pytest.mark.parametrize(
    "values",
    [
        ("41mm", "46 mm"),
        ("S (50mm)", "L (53mm)"),
    ],
)
def test_mm_values_are_rendered_as_size_not_memory(values):
    variants = tuple(
        variant("Watch", value, "Black", product_id=index)
        for index, value in enumerate(values, 1)
    )

    assert detect_attribute_kind(variants) == "size"
    step = build_attribute_step(variants, "uz")
    assert step is not None
    assert step.code == "size"
    assert "o‘lchamni tanlang" in step.text.casefold()
    assert all(
        button.action != "select_memory"
        for row in step.keyboard
        for button in row
    )


def test_blank_or_unrelated_memory_is_skipped_without_guessing():
    blank = (variant("Accessory", "", "Black"),)
    subscription = (variant("Whoop", "1 - Год подписки", ""),)

    assert detect_attribute_kind(blank) is None
    assert detect_attribute_kind(subscription) is None
    assert build_attribute_step(blank, "ru") is None
    assert build_attribute_step(subscription, "ru") is None


def test_color_step_skips_empty_values_and_keeps_not_important_separate():
    variants = (
        variant("Phone", "256Gb", "", product_id=1),
        variant("Phone", "256Gb", "Black", product_id=2),
        variant("Phone", "256Gb", "black", product_id=3),
        variant("Phone", "256Gb", "White", product_id=4),
    )

    step = build_color_step(variants, "ru")

    assert step is not None
    assert [choice.value for choice in step.choices] == ["Black", "White"]
    assert all(choice.value for choice in step.choices)
    assert any(
        button.action == "any_color"
        for row in step.keyboard
        for button in row
    )
    selected = variants_for_choice(variants, step, step.choices[0].choice_id)
    assert {item.product_id for item in selected} == {2, 3}


def test_product_with_no_memory_or_color_has_no_selection_step():
    variants = (variant("Apple Adapter 20W", "", ""),)
    match = ProductMatch("found", ("Apple Adapter 20W",), variants)

    assert build_attribute_step(variants, "ru") is None
    assert build_color_step(variants, "ru") is None
    assert build_first_product_step(match, "ru") is None


def test_first_step_uses_attribute_then_selected_subset_color():
    variants = (
        variant("iPhone", "256Gb", "Black", product_id=1),
        variant("iPhone", "256Gb", "White", product_id=2),
        variant("iPhone", "512Gb", "Black", product_id=3),
    )
    match = ProductMatch("found", ("iPhone",), variants)

    memory = build_first_product_step(match, "ru")
    assert memory is not None and memory.code == "memory"
    selected = variants_for_choice(variants, memory, memory.choices[0].choice_id)
    colors = build_color_step(selected, "ru")
    assert colors is not None
    assert [choice.value for choice in colors.choices] == ["Black", "White"]


def test_all_real_colors_are_kept_and_button_labels_are_bounded():
    variants = tuple(
        variant(
            "Controller",
            "",
            f"Very long approved catalogue color {index} " + "x" * 80,
            product_id=index,
        )
        for index in range(1, 22)
    )

    step = build_color_step(variants, "bi")

    assert step is not None
    assert len(step.choices) == 21
    choice_buttons = [
        button
        for row in step.keyboard
        for button in row
        if button.action == "select_color"
    ]
    assert len(choice_buttons) == 21
    assert all(len(button.text) <= 64 for button in choice_buttons)
    assert all("\n" not in button.text for button in choice_buttons)


def test_variant_steps_reject_mixed_models_instead_of_combining_them():
    variants = (
        variant("Phone A", "256Gb", "Black"),
        variant("Phone B", "512Gb", "White", product_id=2),
    )

    with pytest.raises(ValueError, match="exactly one model"):
        build_attribute_step(variants, "ru")


def test_grouped_catalog_family_uses_match_model_for_variant_steps():
    variants = (
        variant("AirPods Pro 2 Lightning", "", "White"),
        variant("AirPods Pro 2 USB-C", "", "White", product_id=2),
    )
    match = ProductMatch(
        status="found",
        models=("AirPods Pro 2",),
        variants=variants,
        all_variants=variants,
    )

    step = build_first_product_step(match, "ru")

    assert step is not None
    assert step.code == "model"
    assert "AirPods Pro 2" in step.text
    assert "Lightning" in step.text
    assert "USB-C" in step.text

    grouped = build_grouped_model_step(match, "uz")
    assert grouped is not None
    assert grouped.code == "model"


def test_grouped_catalog_exact_technical_choice_can_be_searched_again():
    variants = [
        variant("AirPods Pro 2 Lightning", "", "White"),
        variant("AirPods Pro 2 USB-C", "", "White", product_id=2),
    ]
    repository = ExistingGoogleProductRepository()
    repository._variants = variants
    repository._loaded = time.time()

    family = repository.search("AirPods Pro 2")
    exact = repository.search("AirPods Pro 2 USB-C")

    assert build_grouped_model_step(family, "ru") is not None
    assert exact.status == "found"
    assert exact.models == ("AirPods Pro 2 USB-C",)
    assert {item.model for item in exact.variants} == {"AirPods Pro 2 USB-C"}


def test_inline_keyboard_rejects_oversized_callback_data():
    step = build_color_step((variant("Phone", "", "Black"),), "ru")
    assert step is not None

    with pytest.raises(ValueError, match="1-64"):
        step.inline_keyboard(lambda _action, _choice: "x" * 65)


def actions(step):
    return [
        button.action
        for row in step.keyboard
        for button in row
        if button.action
    ]


def test_fulfillment_step_uses_only_button_specs_and_navigation():
    step = build_fulfillment_step("bi")

    assert step.code == "fulfillment"
    assert "доставка или самовывоз" in step.text.casefold()
    assert "yetkazib berish" in step.text.casefold()
    assert actions(step) == [
        "select_delivery",
        "select_pickup",
        "back",
        "cancel",
    ]
    assert all(
        isinstance(button, ButtonSpec)
        for row in step.keyboard
        for button in row
    )
    assert "заказ" not in " ".join(
        button.text.casefold() for row in step.keyboard for button in row
    )


def test_delivery_phone_is_manual_and_pickup_phone_is_optional():
    delivery = build_delivery_phone_step("ru")
    pickup = build_pickup_contact_step("uz")

    assert delivery.code == "delivery_phone"
    assert "номер или контакт" in delivery.text
    assert actions(delivery) == ["back", "cancel"]
    assert pickup.code == "pickup_contact"
    assert "shart emas" in pickup.text
    assert actions(pickup) == [
        "use_telegram_contact",
        "add_phone",
        "back",
        "cancel",
    ]


def test_delivery_location_accepts_supported_manual_formats():
    step = build_delivery_location_step("bi")

    assert step.code == "delivery_location"
    for expected in ("геолокацию", "ссылку на карту", "адрес"):
        assert expected in step.text
    assert "Geolokatsiya" in step.text
    assert actions(step) == ["back", "cancel"]


def test_delivery_review_requires_location_but_phone_is_optional():
    base = dict(model="Phone X", fulfillment="delivery")

    with pytest.raises(ValueError, match="requires a location"):
        build_review_step(WizardReviewData(**base), "ru")
    step = build_review_step(
        WizardReviewData(**base, location="Tashkent, Chilanzar 10"),
        "ru",
    )
    assert "Связь:</b> этот Telegram-чат" in step.text


def test_delivery_review_links_only_safe_model_and_has_disclaimers():
    data = WizardReviewData(
        model="Phone <X>",
        model_url="https://t.me/Texnikach_Phone/123",
        attribute_kind="memory",
        attribute_value="256 GB",
        color="Black & Gold",
        fulfillment="delivery",
        phone="+998 <90>",
        location="Tashkent & Yunusabad",
    )

    step = build_review_step(data, "ru")

    assert step.code == "review"
    assert '<a href="https://t.me/Texnikach_Phone/123">Phone &lt;X&gt;</a>' in step.text
    assert "Black &amp; Gold" in step.text
    assert "+998 &lt;90&gt;" in step.text
    assert "Заказ не оформлен" in step.text
    assert "не зарезервирован" in step.text
    assert "подтвердит менеджер" in step.text.casefold()
    assert actions(step) == ["submit", "edit", "back", "cancel"]
    assert "Подтвердить заказ" not in step.text


def test_review_drops_unsafe_model_link_and_escapes_catalogue_name():
    step = build_review_step(
        WizardReviewData(
            model='<script>alert("x")</script>',
            model_url="https://evil.test/product/123",
            fulfillment="pickup",
        ),
        "ru",
    )

    assert "evil.test" not in step.text
    assert "<script>" not in step.text
    assert "&lt;script&gt;" in step.text


def test_pickup_review_needs_neither_phone_nor_location():
    step = build_review_step(
        WizardReviewData(
            model="Watch",
            attribute_kind="size",
            attribute_value="46mm",
            any_color=True,
            fulfillment="pickup",
        ),
        "bi",
    )

    assert "Размер" in step.text and "O‘lcham" in step.text
    assert "не важен" in step.text and "farqi yo‘q" in step.text
    assert "этот Telegram-чат" in step.text
    assert "shu Telegram chati" in step.text
    assert "Локация" not in step.text


def test_edit_menu_exposes_only_fields_valid_for_fulfillment_path():
    delivery = build_edit_menu(
        WizardReviewData(
            model="Phone",
            attribute_kind="memory",
            attribute_value="256 GB",
            any_color=True,
            fulfillment="delivery",
            phone="+998901234567",
            location="Tashkent",
        ),
        "ru",
    )
    pickup = build_edit_menu(
        WizardReviewData(model="Adapter", fulfillment="pickup"),
        "uz",
    )

    assert actions(delivery) == [
        "edit_model",
        "edit_attribute",
        "edit_color",
        "edit_fulfillment",
        "edit_phone",
        "edit_location",
        "back",
        "cancel",
    ]
    assert actions(pickup) == [
        "edit_model",
        "edit_fulfillment",
        "edit_pickup_contact",
        "back",
        "cancel",
    ]


def test_review_rejects_inconsistent_attribute_and_color_preferences():
    with pytest.raises(ValueError, match="supplied together"):
        build_review_step(
            WizardReviewData(
                model="Phone",
                attribute_kind="memory",
                fulfillment="pickup",
            )
        )
    with pytest.raises(ValueError, match="mutually exclusive"):
        build_review_step(
            WizardReviewData(
                model="Phone",
                color="Black",
                any_color=True,
                fulfillment="pickup",
            )
        )
    with pytest.raises(ValueError, match="unsupported attribute"):
        build_review_step(
            WizardReviewData(
                model="Phone",
                attribute_kind="weight",  # type: ignore[arg-type]
                attribute_value="100g",
                fulfillment="pickup",
            )
        )


def test_bilingual_review_is_bounded_and_blank_pickup_phone_uses_telegram():
    step = build_review_step(
        WizardReviewData(
            model="<&>" * 300,
            attribute_kind="memory",
            attribute_value="<&>" * 300,
            color="<&>" * 300,
            fulfillment="pickup",
            phone="   ",
        ),
        "bi",
    )

    assert len(step.text) <= 4096
    assert "этот Telegram-чат" in step.text
    assert "shu Telegram chati" in step.text

from __future__ import annotations

import html
import json
import logging
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any, Iterable, Mapping, Sequence

from .product_wizard import (
    ButtonSpec,
    WizardReviewData,
    WizardStep,
    build_attribute_step,
    build_color_step,
    build_delivery_location_step,
    build_delivery_phone_step,
    build_edit_menu,
    build_fulfillment_step,
    build_grouped_model_step,
    build_model_step,
    build_pickup_contact_step,
    build_review_step,
    variants_for_choice,
)
from .products import ProductMatch, ProductVariant, normalize_model, safe_product_url
from .request_inputs import (
    format_price,
    location_from_message,
    localized_missing,
    masked_phone,
    missing_request_fields,
    phone_from_message,
    selection_fields,
)
from .telegram_api import make_callback_data, parse_callback_data
from .timeutils import is_night, next_night_end


LOG = logging.getLogger("telegram_business.requests")


def _value(row: Any, key: str, default: Any = None) -> Any:
    try:
        value = row[key]
    except (KeyError, TypeError, IndexError):
        value = getattr(row, key, default)
    return default if value is None else value


def _localized(language: str, ru: str, uz: str) -> str:
    if language == "ru":
        return ru
    if language == "uz":
        return uz
    return f"{ru}\n\n———\n\n{uz}"


def _choice(step: WizardStep, raw_value: str | None):
    normalized = normalize_model(raw_value or "")
    return next(
        (
            choice for choice in step.choices
            if normalize_model(choice.value) == normalized
        ),
        None,
    )


def _minimum_price(variants: Sequence[ProductVariant]) -> str | None:
    values = [
        variant.price_uzs
        for variant in variants
        if isinstance(variant.price_uzs, Decimal)
        and variant.price_uzs.is_finite()
        and variant.price_uzs > 0
    ]
    return str(min(values)) if values else None


@dataclass(frozen=True)
class PreparedRequestScreen:
    request: Any
    step: WizardStep
    reply_markup: dict[str, Any]


class NightRequestCoordinator:
    """Durable, callback-driven night request flow.

    This coordinator never creates an order, reservation or delivery promise.
    It stores a structured request for the manager and edits one inline-keyboard
    message while the customer narrows the approved catalogue variants.
    """

    def __init__(self, service: Any):
        self.service = service
        self.repo = service.repo
        self.api = service.api
        self.products = service.products

    def active(self, chat_id: str, session_id: str | None = None):
        method = getattr(self.repo, "active_business_request", None)
        return method(chat_id, session_id) if method else None

    def _schedule_expiry(
        self,
        request: Any,
        connection_id: str,
        event_at: datetime,
        now: datetime,
    ) -> None:
        policy = self.service._runtime_policy(event_at)
        end = next_night_end(event_at, policy.night_end, policy.night_start)
        self.repo.schedule(
            f"request-expire:{_value(request, 'request_id')}",
            str(_value(request, "chat_id")),
            _value(request, "session_id"),
            "request_expire",
            end,
            {
                "connection_id": connection_id,
                "request_id": _value(request, "request_id"),
            },
            now,
        )

    def _callback_ttl(self, now: datetime) -> int:
        policy = self.service._runtime_policy(now)
        if not is_night(now, policy.night_start, policy.night_end):
            return 60
        end = next_night_end(now, policy.night_end, policy.night_start)
        return max(60, int((end - now).total_seconds()) + 60)

    def markup(self, request: Any, step: WizardStep, now: datetime) -> dict[str, Any]:
        request_id = str(_value(request, "request_id"))
        revision = int(_value(request, "revision", 0))
        ttl = self._callback_ttl(now)

        def encode(action: str, choice_id: str | None) -> str:
            payload: dict[str, Any] = {"step": step.code}
            if choice_id:
                choice = step.choice(choice_id)
                payload.update(choice_id=choice_id, value=choice.value)
            token = self.repo.issue_business_callback(
                request_id,
                revision,
                action,
                payload,
                now,
                ttl_seconds=ttl,
            )
            if not token:
                raise RuntimeError("request callback could not be issued")
            return make_callback_data(token)

        return step.inline_keyboard(encode)

    @staticmethod
    def _enter_model_step(language: str) -> WizardStep:
        text = _localized(
            language,
            "Напишите точное название модели. Например: iPhone 16 Pro Max.",
            "Modelning aniq nomini yozing. Masalan: iPhone 16 Pro Max.",
        )
        keyboard = (
            (
                ButtonSpec(
                    text=(
                        "Отменить"
                        if language == "ru"
                        else "Bekor qilish"
                        if language == "uz"
                        else "Отменить / Bekor qilish"
                    ),
                    action="cancel",
                ),
            ),
        )
        return WizardStep("model", text, (), keyboard)

    @staticmethod
    def _pickup_phone_step(language: str) -> WizardStep:
        base = build_delivery_phone_step(language)
        text = _localized(
            language,
            "Отправьте номер или нажмите «Назад» и оставьте Telegram.",
            "Raqamni yuboring yoki «Orqaga» bosib Telegramni qoldiring.",
        )
        return WizardStep("delivery_phone", text, (), base.keyboard)

    @staticmethod
    def _pickup_contact_step(request: Any, language: str) -> WizardStep:
        return build_pickup_contact_step(
            language,
            has_saved_phone=bool(str(_value(request, "phone", "") or "").strip()),
        )

    def _review_step(self, request: Any, language: str) -> WizardStep:
        fulfillment = str(_value(request, "fulfillment_method", ""))
        data = WizardReviewData(
            model=str(_value(request, "exact_model", "")),
            model_url=safe_product_url(_value(request, "model_url", "")),
            attribute_kind=(
                _value(request, "option_kind")
                if _value(request, "option_kind") in {"memory", "size"}
                else None
            ),
            attribute_value=_value(request, "option_value"),
            color=_value(request, "color"),
            any_color=bool(_value(request, "color_any", 0)),
            fulfillment=fulfillment,  # validated by builder
            phone=masked_phone(_value(request, "phone")) or None,
            location=(
                _value(request, "address") or _value(request, "location_url")
            ),
        )
        step = build_review_step(data, language)
        price = _value(request, "database_price")
        if price:
            if language == "ru":
                suffix = f"\n\nЦена: {format_price(price, 'ru')}"
            elif language == "uz":
                suffix = f"\n\nNarx: {format_price(price, 'uz')}"
            else:
                suffix = f"\n\nЦена / Narx: {format_price(price, 'uz')}"
            step = WizardStep(step.code, step.text + suffix, step.choices, step.keyboard)
        return step

    def _edit_step(self, request: Any, language: str) -> WizardStep:
        return build_edit_menu(
            WizardReviewData(
                model=str(_value(request, "exact_model", "")),
                model_url=safe_product_url(_value(request, "model_url", "")),
                attribute_kind=(
                    _value(request, "option_kind")
                    if _value(request, "option_kind") in {"memory", "size"}
                    else None
                ),
                attribute_value=_value(request, "option_value"),
                color=_value(request, "color"),
                any_color=bool(_value(request, "color_any", 0)),
                fulfillment=str(_value(request, "fulfillment_method", "")),
                phone=masked_phone(_value(request, "phone")) or None,
                location=_value(request, "address") or _value(request, "location_url"),
            ),
            language,
        )

    def _full_match(self, model: str, *, memory: str | None = None, color: str | None = None) -> ProductMatch:
        try:
            return self.products.search(model, memory=memory, color=color)
        except TypeError:
            return self.products.search(model)

    def _variants_for_request(self, request: Any, *, include_color: bool = True) -> tuple[ProductVariant, ...]:
        model = str(_value(request, "exact_model", ""))
        option = _value(request, "option_value")
        color = (
            _value(request, "color")
            if include_color and not bool(_value(request, "color_any", 0))
            else None
        )
        match = self._full_match(model, memory=option, color=color)
        if match.status != "found" or not match.variants:
            raise RuntimeError("selected product variants are unavailable")
        return tuple(match.variants)

    def _product_screen_for_request(self, request: Any, language: str) -> WizardStep:
        state = str(_value(request, "wizard_state", "model"))
        if state == "model":
            return self._enter_model_step(language)
        if state in {"memory", "size"}:
            variants = self._full_match(str(_value(request, "exact_model", ""))).variants
            return build_attribute_step(
                variants,
                language,
                model_name=str(_value(request, "exact_model", "")),
            ) or build_fulfillment_step(language)
        if state == "color":
            variants = self._variants_for_request(request, include_color=False)
            return build_color_step(
                variants,
                language,
                model_name=str(_value(request, "exact_model", "")),
            ) or build_fulfillment_step(language)
        if state == "fulfillment":
            return build_fulfillment_step(language)
        if state == "delivery_phone":
            return build_delivery_phone_step(language)
        if state == "pickup_contact":
            return self._pickup_contact_step(request, language)
        if state == "pickup_phone":
            return self._pickup_phone_step(language)
        if state == "delivery_location":
            return build_delivery_location_step(language)
        if state == "edit":
            return self._edit_step(request, language)
        if state == "review":
            return self._review_step(request, language)
        return build_fulfillment_step(language)

    def _transition_found(
        self,
        request: Any,
        match: ProductMatch,
        connection_id: str,
        now: datetime,
        event_at: datetime,
        *,
        language: str,
        event_key: str,
        update_id: int | None = None,
        message_id: int | None = None,
        requested_memory: str | None = None,
        requested_color: str | None = None,
        wizard_message_id: int | None = None,
        early_phone: str | None = None,
        early_phone_source: str | None = None,
        early_location: Any | None = None,
    ) -> PreparedRequestScreen | None:
        if match.status != "found" or not match.models or not match.variants:
            return None
        model = match.models[0]
        variants = tuple(match.all_variants or match.variants)
        attribute_step = build_attribute_step(
            variants, language, model_name=model,
        )
        option_kind: str | None = None
        option_value: str | None = None
        selected_variants = variants
        if attribute_step:
            chosen = _choice(attribute_step, requested_memory)
            if chosen is None and len(attribute_step.choices) == 1:
                chosen = attribute_step.choices[0]
            if chosen is not None:
                option_kind = attribute_step.code
                option_value = chosen.value
                selected_variants = variants_for_choice(
                    variants, attribute_step, chosen.choice_id,
                )

        color_step = build_color_step(
            selected_variants, language, model_name=model,
        )
        selected_color: str | None = None
        if color_step and requested_color:
            chosen_color = _choice(color_step, requested_color)
            if chosen_color:
                selected_color = chosen_color.value
                selected_variants = variants_for_choice(
                    selected_variants, color_step, chosen_color.choice_id,
                )

        if attribute_step and option_value is None:
            state, step = attribute_step.code, attribute_step
        elif color_step and selected_color is None:
            state, step = "color", color_step
        else:
            state, step = "fulfillment", build_fulfillment_step(language)

        selections: dict[str, Any] = {
            "model_query": model,
            "attribute_required": bool(attribute_step),
            "color_required": bool(color_step),
        }
        if early_location is not None:
            selections["outside_tashkent"] = bool(
                early_location.outside_tashkent
            )
        if wizard_message_id:
            selections["wizard_message_id"] = int(wizard_message_id)
        updated = self.repo.update_business_request(
            str(_value(request, "request_id")),
            int(_value(request, "revision")),
            now,
            state=state,
            status="collecting",
            language=language,
            exact_model=model,
            model_key=normalize_model(model),
            model_url=match.url_for(model),
            option_kind=option_kind,
            option_value=option_value,
            color=selected_color,
            color_any=0,
            database_price=_minimum_price(selected_variants),
            source_updated_at=now.isoformat(),
            phone=early_phone or _value(request, "phone"),
            contact_method=(
                early_phone_source or _value(request, "contact_method")
            ),
            location_url=(
                early_location.url
                if early_location is not None
                else _value(request, "location_url")
            ),
            address=(
                early_location.address
                if early_location is not None
                else _value(request, "address")
            ),
            selections=selections,
            clear_selections=("outside_tashkent",),
            event_type="product_selected",
            event_key=event_key,
            telegram_update_id=update_id,
            telegram_message_id=message_id,
            client_at=event_at,
        )
        if not updated:
            return None
        self._schedule_expiry(updated, connection_id, event_at, now)
        return PreparedRequestScreen(updated, step, self.markup(updated, step, now))

    def prepare_match(
        self,
        match: ProductMatch,
        *,
        connection_id: str,
        chat_id: str,
        session_id: str,
        language: str,
        now: datetime,
        event_at: datetime,
        message_id: int,
        update_id: int | None,
        model_query: str,
        requested_memory: str | None = None,
        requested_color: str | None = None,
        rows: Sequence[Any] = (),
        text: str = "",
    ) -> PreparedRequestScreen | None:
        raw_message = self._raw_message_for_rows(rows)
        early_phone, early_phone_source = phone_from_message(raw_message, text)
        early_location = location_from_message(
            raw_message, text, expected=False,
        )
        request = self.repo.get_or_create_business_request(
            chat_id,
            session_id,
            now,
            business_connection_id=connection_id,
            language=language,
            event_at=event_at,
            message_id=message_id,
            telegram_update_id=update_id,
        )
        if _value(request, "status") == "submitted":
            return None
        event_key = f"message:{message_id}:update:{update_id or 0}:product"
        grouped_model_step = (
            build_grouped_model_step(match, language)
            if match.status == "found"
            else None
        )
        if match.status == "ambiguous" or grouped_model_step is not None:
            step = grouped_model_step or build_model_step(match, language)
            if not step:
                return None
            ambiguous_selections: dict[str, Any] = {
                "model_query": model_query,
            }
            if early_location is not None:
                ambiguous_selections["outside_tashkent"] = bool(
                    early_location.outside_tashkent
                )
            updated = self.repo.update_business_request(
                str(_value(request, "request_id")),
                int(_value(request, "revision")),
                now,
                state="model",
                status="collecting",
                language=language,
                exact_model=None,
                model_key=None,
                model_url=None,
                option_kind=None,
                option_value=None,
                color=None,
                color_any=0,
                fulfillment_method=None,
                selections=ambiguous_selections,
                clear_selections=(
                    "attribute_required", "color_required", "outside_tashkent",
                ),
                event_type="model_candidates",
                event_key=event_key,
                telegram_update_id=update_id,
                telegram_message_id=message_id,
                client_at=event_at,
                phone=early_phone or _value(request, "phone"),
                contact_method=(
                    early_phone_source or _value(request, "contact_method")
                ),
                location_url=(
                    early_location.url
                    if early_location is not None
                    else _value(request, "location_url")
                ),
                address=(
                    early_location.address
                    if early_location is not None
                    else _value(request, "address")
                ),
            )
            if not updated:
                return None
            self._schedule_expiry(updated, connection_id, event_at, now)
            return PreparedRequestScreen(
                updated, step, self.markup(updated, step, now),
            )
        return self._transition_found(
            request,
            match,
            connection_id,
            now,
            event_at,
            language=language,
            event_key=event_key,
            update_id=update_id,
            message_id=message_id,
            requested_memory=requested_memory,
            requested_color=requested_color,
            early_phone=early_phone,
            early_phone_source=early_phone_source,
            early_location=early_location,
        )

    def begin_model(
        self,
        *,
        connection_id: str,
        chat_id: str,
        session_id: str,
        language: str,
        now: datetime,
        event_at: datetime,
        message_id: int,
        update_id: int | None,
        rows: Sequence[Any] = (),
        text: str = "",
    ) -> PreparedRequestScreen | None:
        """Open a durable draft before the customer has named a model.

        A phone or an explicit location sent early is retained, but never used
        to skip the catalogue-backed model/variant selection.
        """

        current = self.active(chat_id, session_id)
        if current and (
            _value(current, "status") == "submitted"
            or str(_value(current, "exact_model", "") or "").strip()
        ):
            return None
        request = self.repo.get_or_create_business_request(
            chat_id,
            session_id,
            now,
            business_connection_id=connection_id,
            language=language,
            event_at=event_at,
            message_id=message_id,
            telegram_update_id=update_id,
        )
        if _value(request, "status") == "submitted":
            return None

        raw_message = self._raw_message_for_rows(rows)
        phone, phone_source = phone_from_message(raw_message, text)
        parsed_location = location_from_message(raw_message, text, expected=False)
        changes: dict[str, Any] = {
            "state": "model",
            "status": "collecting",
            "language": language,
        }
        selections: dict[str, Any] = {}
        if phone:
            changes.update(phone=phone, contact_method=phone_source or "typed")
        if parsed_location:
            changes.update(
                location_url=parsed_location.url,
                address=parsed_location.address,
            )
            selections["outside_tashkent"] = parsed_location.outside_tashkent
        updated = self.repo.update_business_request(
            str(_value(request, "request_id")),
            int(_value(request, "revision")),
            now,
            changes,
            selections=selections or None,
            event_type="request_started",
            event_key=f"message:{message_id}:update:{update_id or 0}:start",
            telegram_update_id=update_id,
            telegram_message_id=message_id,
            client_at=event_at,
        )
        if not updated:
            return None
        self._schedule_expiry(updated, connection_id, event_at, now)
        step = self._enter_model_step(language)
        return PreparedRequestScreen(updated, step, self.markup(updated, step, now))

    def _answer_callback(self, callback_id: str, text: str | None = None) -> None:
        try:
            self.api.answer_callback_query(callback_id, text=text)
        except Exception as exc:
            LOG.warning(
                "business_callback_answer_failed type=%s", type(exc).__name__,
            )

    def _stale_text(self, language: str) -> str:
        return _localized(
            language,
            "Этот экран устарел. Используйте последние кнопки.",
            "Bu ekran eskirgan. Eng so‘nggi tugmalardan foydalaning.",
        )[:200]

    def _edit_screen(
        self,
        request: Any,
        step: WizardStep,
        connection_id: str,
        chat_id: str,
        message_id: int,
        now: datetime,
    ) -> None:
        session_id = str(_value(request, "session_id", ""))
        policy = self.service._runtime_policy(now)
        if (
            not self.service._connection_allows_reply(connection_id)
            or not self.repo.may_automate(chat_id, now)
            or (
                hasattr(self.repo, "session_may_automate")
                and not self.repo.session_may_automate(session_id)
            )
            or self.service._manager_fence_active(chat_id, now, policy)
        ):
            return
        reply_markup = self.markup(request, step, now)
        editor = getattr(self.api, "edit_message_text", None)
        if editor:
            editor(
                connection_id,
                chat_id,
                message_id,
                step.text,
                parse_mode=step.parse_mode,
                reply_markup=reply_markup,
            )
            return
        # Compatibility fallback for a minimal Telegram adapter: send the new
        # screen and bind it to the same request revision. Production uses edit.
        sent = self.service.send(
            connection_id,
            chat_id,
            session_id,
            step.text,
            "request_prompt",
            now,
            parse_mode=step.parse_mode,
            reply_markup=reply_markup,
            delivery_key=(
                f"request:{_value(request, 'request_id')}:"
                f"revision:{_value(request, 'revision')}"
            ),
            return_message_id=True,
        )
        binder = getattr(self.repo, "bind_business_request_message", None)
        if binder and isinstance(sent, int) and not isinstance(sent, bool):
            binder(
                str(_value(request, "request_id")),
                int(_value(request, "revision", 0)),
                sent,
                now,
            )

    def _terminal_text(self, code: str, request: Any, language: str, now: datetime) -> str:
        policy = self.service._runtime_policy(now)
        ru, uz = self.service._manager_phrases(now, policy)
        missing = missing_request_fields(request)
        return self.service._render_message(
            code,
            language,
            now,
            manager_time_phrase_ru=ru,
            manager_time_phrase_uz=uz,
            missing_fields=localized_missing(missing, language if language in {"ru", "uz"} else "ru"),
        ) or ""

    def _update_state(
        self,
        request: Any,
        now: datetime,
        callback_id: str,
        message_id: int,
        state: str,
        **fields: Any,
    ):
        selections = dict(fields.pop("selections", {}) or {})
        selections["wizard_message_id"] = int(message_id)
        return self.repo.update_business_request(
            str(_value(request, "request_id")),
            int(_value(request, "revision")),
            now,
            state=state,
            selections=selections,
            event_type="callback_transition",
            event_key=f"callback:{callback_id}",
            **fields,
        )

    def _apply_callback(
        self,
        request: Any,
        action: str,
        payload: Mapping[str, Any],
        callback_id: str,
        connection_id: str,
        message_id: int,
        now: datetime,
        language: str,
    ) -> tuple[Any, WizardStep | None, str | None]:
        if action == "select_model":
            model = str(payload.get("value") or "")
            try:
                match = self._full_match(model)
            except Exception as exc:
                self.service._record_error(
                    "request_model_selection", now,
                    str(_value(request, "chat_id")),
                    str(_value(request, "session_id", "")), exc,
                )
                updated = self._update_state(
                    request, now, callback_id, message_id, "fulfillment",
                    exact_model=model,
                    model_key=normalize_model(model),
                    model_url=None,
                    option_kind=None,
                    option_value=None,
                    color=None,
                    color_any=0,
                    database_price=None,
                    selections={"attribute_required": False, "color_required": False},
                )
                return updated, build_fulfillment_step(language), None
            prepared = self._transition_found(
                request,
                match,
                connection_id,
                now,
                now,
                language=language,
                event_key=f"callback:{callback_id}",
                wizard_message_id=message_id,
            )
            return (
                (prepared.request, prepared.step, None)
                if prepared
                else (request, None, None)
            )

        if action in {"select_memory", "select_size"}:
            variants = tuple(self._full_match(str(_value(request, "exact_model", ""))).variants)
            step = build_attribute_step(
                variants,
                language,
                model_name=str(_value(request, "exact_model", "")),
            )
            choice_id = str(payload.get("choice_id") or "")
            if not step or step.code not in {"memory", "size"}:
                return request, None, None
            selected = variants_for_choice(variants, step, choice_id)
            if not selected:
                return request, None, None
            chosen = step.choice(choice_id)
            color_step = build_color_step(
                selected,
                language,
                model_name=str(_value(request, "exact_model", "")),
            )
            state = "color" if color_step else "fulfillment"
            updated = self._update_state(
                request,
                now,
                callback_id,
                message_id,
                state,
                option_kind=step.code,
                option_value=chosen.value,
                color=None,
                color_any=0,
                database_price=_minimum_price(selected),
                selections={"attribute_required": True, "color_required": bool(color_step)},
            )
            return updated, color_step or build_fulfillment_step(language), None

        if action in {"select_color", "any_color"}:
            variants = self._variants_for_request(request, include_color=False)
            step = build_color_step(
                variants,
                language,
                model_name=str(_value(request, "exact_model", "")),
            )
            color = None
            selected = variants
            if action == "select_color":
                choice_id = str(payload.get("choice_id") or "")
                if not step:
                    return request, None, None
                selected = variants_for_choice(variants, step, choice_id)
                if not selected:
                    return request, None, None
                color = step.choice(choice_id).value
            updated = self._update_state(
                request,
                now,
                callback_id,
                message_id,
                "fulfillment",
                color=color,
                color_any=int(action == "any_color"),
                database_price=_minimum_price(selected),
                selections={"color_required": bool(step)},
            )
            return updated, build_fulfillment_step(language), None

        if action == "select_delivery":
            has_phone = bool(str(_value(request, "phone", "") or "").strip())
            has_location = bool(
                str(_value(request, "location_url", "") or "").strip()
                or str(_value(request, "address", "") or "").strip()
            )
            state = (
                "review"
                if has_phone and has_location
                else "delivery_location"
                if has_phone
                else "delivery_phone"
            )
            updated = self._update_state(
                request, now, callback_id, message_id, state,
                status="ready" if state == "review" else "collecting",
                fulfillment_method="delivery", contact_method="phone",
            )
            step = (
                self._review_step(updated, language)
                if state == "review"
                else build_delivery_location_step(language)
                if state == "delivery_location"
                else build_delivery_phone_step(language)
            )
            return updated, step, None

        if action == "select_pickup":
            updated = self._update_state(
                request,
                now,
                callback_id,
                message_id,
                "pickup_contact",
                fulfillment_method="pickup",
                contact_method=None,
                location_url=None,
                address=None,
                clear_selections=("outside_tashkent",),
            )
            return updated, self._pickup_contact_step(updated, language), None

        if action == "use_telegram_contact":
            updated = self._update_state(
                request, now, callback_id, message_id, "review",
                status="ready", contact_method="telegram", phone=None,
            )
            return updated, self._review_step(updated, language), None

        if action == "use_saved_phone":
            if not str(_value(request, "phone", "") or "").strip():
                return request, self._pickup_contact_step(request, language), None
            updated = self._update_state(
                request, now, callback_id, message_id, "review",
                status="ready", contact_method="phone",
            )
            return updated, self._review_step(updated, language), None

        if action == "add_phone":
            updated = self._update_state(
                request, now, callback_id, message_id, "pickup_phone",
                contact_method="phone",
            )
            return updated, self._pickup_phone_step(language), None

        if action == "edit":
            updated = self._update_state(
                request, now, callback_id, message_id, "edit",
            )
            return updated, self._edit_step(updated, language), None

        if action == "submit":
            missing = missing_request_fields(request)
            if missing:
                return request, self._product_screen_for_request(request, language), None
            fulfillment = str(_value(request, "fulfillment_method"))
            completed = self.repo.complete_business_request(
                str(_value(request, "request_id")),
                f"complete_{fulfillment}",
                int(_value(request, "revision")),
                now,
                event_key=f"callback:{callback_id}",
            )
            if not completed:
                return request, None, None
            self.repo.cancel(f"request-expire:{_value(request, 'request_id')}")
            schedule_notification = getattr(
                self.service, "schedule_order_notification", None
            )
            if schedule_notification:
                schedule_notification(completed, now)
            code = "request_saved_delivery" if fulfillment == "delivery" else "request_saved_pickup"
            return completed, None, self._terminal_text(code, completed, language, now)

        if action == "cancel":
            cancelled = self.repo.cancel_business_request(
                str(_value(request, "request_id")),
                int(_value(request, "revision")),
                now,
                event_key=f"callback:{callback_id}",
            )
            if not cancelled:
                return request, None, None
            self.repo.cancel(f"request-expire:{_value(request, 'request_id')}")
            return cancelled, None, self._terminal_text("request_cancelled", cancelled, language, now)

        if action in {
            "enter_model", "edit_model", "edit_attribute", "edit_color",
            "edit_fulfillment", "edit_phone", "edit_location",
            "edit_pickup_contact", "back",
        }:
            return self._navigate(
                request, action, callback_id, message_id, now, language,
            )
        return request, None, None

    def _navigate(
        self,
        request: Any,
        action: str,
        callback_id: str,
        message_id: int,
        now: datetime,
        language: str,
    ) -> tuple[Any, WizardStep | None, str | None]:
        state = str(_value(request, "wizard_state", "model"))
        fields = selection_fields(request)
        target = "model"
        changes: dict[str, Any] = {}
        clear: tuple[str, ...] = ()
        if action in {"enter_model", "edit_model"}:
            target = "model"
            changes.update(
                exact_model=None, model_key=None, model_url=None,
                option_kind=None, option_value=None, color=None, color_any=0,
                database_price=None, status="collecting",
            )
            clear = ("attribute_required", "color_required")
        elif action == "edit_attribute":
            target = str(_value(request, "option_kind") or "memory")
            changes.update(option_value=None, color=None, color_any=0, status="collecting")
        elif action == "edit_color":
            target = "color"
            changes.update(color=None, color_any=0, status="collecting")
        elif action == "edit_fulfillment":
            target = "fulfillment"
            changes.update(status="collecting")
        elif action == "edit_phone":
            target = "delivery_phone"
            changes.update(status="collecting")
        elif action == "edit_location":
            target = "delivery_location"
            changes.update(status="collecting")
        elif action == "edit_pickup_contact":
            target = "pickup_contact"
            changes.update(status="collecting")
        elif action == "back":
            if state == "color" and fields.get("attribute_required"):
                target = str(_value(request, "option_kind") or "memory")
                changes.update(option_value=None, color=None, color_any=0)
            elif state in {"memory", "size"}:
                target = "model"
                changes.update(
                    exact_model=None, model_key=None, model_url=None,
                    option_kind=None, option_value=None, color=None, color_any=0,
                )
                clear = ("attribute_required", "color_required")
            elif state == "fulfillment":
                if fields.get("color_required"):
                    target = "color"
                    changes.update(color=None, color_any=0)
                elif fields.get("attribute_required"):
                    target = str(_value(request, "option_kind") or "memory")
                    changes.update(option_value=None)
                else:
                    target = "model"
            elif state in {"delivery_phone", "pickup_contact"}:
                target = "fulfillment"
            elif state == "pickup_phone":
                target = "pickup_contact"
            elif state == "delivery_location":
                target = "delivery_phone"
            elif state == "edit":
                target = "review"
            elif state == "review":
                target = "edit"

        selections = {"wizard_message_id": int(message_id)}
        updated = self.repo.update_business_request(
            str(_value(request, "request_id")),
            int(_value(request, "revision")),
            now,
            state=target,
            selections=selections,
            clear_selections=clear,
            event_type="navigation",
            event_key=f"callback:{callback_id}",
            **changes,
        )
        if not updated:
            return request, None, None
        return updated, self._product_screen_for_request(updated, language), None

    def handle_callback(self, update: Mapping[str, Any], now: datetime) -> bool:
        query = update.get("callback_query")
        if not isinstance(query, Mapping):
            return False
        callback_id = str(query.get("id") or "")
        token = parse_callback_data(query.get("data"))
        message = query.get("message") if isinstance(query.get("message"), Mapping) else {}
        chat = message.get("chat") if isinstance(message.get("chat"), Mapping) else {}
        chat_id = str(chat.get("id") or "")
        message_id = message.get("message_id")
        if not callback_id or not token or not chat_id or not isinstance(message_id, int):
            if callback_id:
                self._answer_callback(callback_id, self._stale_text("bi"))
            return True

        receipt = self.repo.consume_business_callback(
            token, callback_id, chat_id, now,
        )
        request = (
            self.repo.business_request(_value(receipt, "request_id"))
            if _value(receipt, "request_id") else None
        )
        language = str(_value(request, "language", "bi") or "bi")
        if _value(receipt, "status") == "applied":
            self._answer_callback(callback_id)
            return True
        if _value(receipt, "outcome") != "accepted" or not request:
            self._answer_callback(callback_id, self._stale_text(language))
            return True

        connection_id = str(
            message.get("business_connection_id")
            or _value(request, "business_connection_id", "")
        )
        session_id = str(_value(request, "session_id", ""))
        policy = self.service._runtime_policy(now)
        from_id = str((query.get("from") or {}).get("id") or "")
        authorized = (
            connection_id == str(self.service.settings.allowed_connection_id)
            and str(_value(request, "chat_id")) == chat_id
            and (not from_id or from_id == chat_id)
            and is_night(now, policy.night_start, policy.night_end)
            and self.service._connection_allows_reply(connection_id)
            and self.repo.may_automate(chat_id, now)
            and (
                not hasattr(self.repo, "session_may_automate")
                or self.repo.session_may_automate(session_id)
            )
            and not self.service._manager_fence_active(chat_id, now, policy)
        )
        if not authorized:
            self.repo.finish_business_callback(
                callback_id, now, applied=False, outcome="automation_blocked",
            )
            self._answer_callback(callback_id, self._stale_text(language))
            return True

        try:
            # Once the customer actively uses the request wizard, the legacy
            # five-minute price reminder must not interrupt the current step.
            self.repo.cancel(f"final:{session_id}")
            payload = json.loads(str(_value(receipt, "payload", "{}") or "{}"))
            if not isinstance(payload, dict):
                payload = {}
            updated, step, terminal = self._apply_callback(
                request,
                str(_value(receipt, "action", "")),
                payload,
                callback_id,
                connection_id,
                message_id,
                now,
                language,
            )
            if terminal is not None:
                self.api.edit_message_text(
                    connection_id, chat_id, message_id, terminal,
                    reply_markup={"inline_keyboard": []},
                )
            elif step is not None and updated is not None:
                self._edit_screen(
                    updated, step, connection_id, chat_id, message_id, now,
                )
            if updated is None or (step is None and terminal is None):
                self.repo.finish_business_callback(
                    callback_id, now, applied=False, outcome="invalid_transition",
                )
                self._answer_callback(callback_id, self._stale_text(language))
                return True
            self.repo.finish_business_callback(
                callback_id,
                now,
                applied=True,
                outcome="applied",
                result_revision=int(_value(updated, "revision", 0)),
                result={"wizard_state": _value(updated, "wizard_state")},
            )
            self._answer_callback(callback_id)
            LOG.info(
                "business_request_callback_applied chat_id=%s session_id=%s action=%s state=%s",
                chat_id, session_id, _value(receipt, "action"),
                _value(updated, "wizard_state"),
            )
            return True
        except Exception as exc:
            LOG.error(
                "business_request_callback_failed chat_id=%s type=%s",
                chat_id, type(exc).__name__,
            )
            raise

    def _raw_message_for_rows(self, rows: Iterable[Any]) -> Mapping[str, Any]:
        fallback: Mapping[str, Any] = {}
        native_contact: Mapping[str, Any] | None = None
        native_location: Mapping[str, Any] | None = None
        for row in reversed(tuple(rows)):
            update_id = _value(row, "update_id")
            if update_id is None:
                continue
            update = self.repo.update(int(update_id))
            if not update:
                continue
            try:
                payload = json.loads(update["raw_payload"])
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
            message = payload.get("business_message") if isinstance(payload, dict) else None
            if isinstance(message, dict):
                if not fallback:
                    fallback = message
                # Contact and Location may arrive as two consecutive Telegram
                # messages inside the same debounce burst. Preserve each kind
                # independently while keeping the latest text as fallback.
                if native_location is None and isinstance(
                    message.get("location"), dict
                ):
                    native_location = message["location"]
                if native_contact is None and isinstance(
                    message.get("contact"), dict
                ):
                    native_contact = message["contact"]
        if not fallback:
            return fallback
        combined = dict(fallback)
        if native_location is not None:
            combined["location"] = dict(native_location)
        if native_contact is not None:
            combined["contact"] = dict(native_contact)
        return combined

    def handle_expected_input(
        self,
        *,
        connection_id: str,
        chat_id: str,
        session_id: str,
        rows: Sequence[Any],
        text: str,
        language: str,
        now: datetime,
        event_at: datetime,
        update_id: int | None,
        message_id: int,
    ) -> bool:
        request = self.active(chat_id, session_id)
        if not request:
            return False
        if _value(request, "status") == "submitted":
            return True
        state = str(_value(request, "wizard_state", ""))
        raw_message = self._raw_message_for_rows(rows)
        fields = selection_fields(request)
        wizard_message_id = fields.get("wizard_message_id")
        try:
            wizard_message_id = int(wizard_message_id) if wizard_message_id else None
        except (TypeError, ValueError):
            wizard_message_id = None

        event_lookup = getattr(self.repo, "business_request_event", None)
        replay_keys = (
            f"message:{message_id}:phone",
            f"message:{message_id}:location",
            f"message:{message_id}:early-details",
            f"message:{message_id}:update:{update_id or 0}:start",
        )
        if event_lookup and any(
            event_lookup(str(_value(request, "request_id")), key)
            for key in replay_keys
        ):
            current = self.repo.business_request(_value(request, "request_id"))
            if not current or _value(current, "status") == "submitted":
                return True
            step = self._product_screen_for_request(current, language)
            self.repo.cancel(f"final:{session_id}")
            if wizard_message_id:
                self._edit_screen(
                    current, step, connection_id, chat_id,
                    wizard_message_id, now,
                )
            else:
                sent = self.service.send(
                    connection_id,
                    chat_id,
                    session_id,
                    step.text,
                    "request_prompt",
                    now,
                    parse_mode=step.parse_mode,
                    reply_markup=self.markup(current, step, now),
                    delivery_key=(
                        f"request:{_value(current, 'request_id')}:"
                        f"revision:{_value(current, 'revision')}:replay"
                    ),
                    return_message_id=True,
                )
                binder = getattr(
                    self.repo, "bind_business_request_message", None,
                )
                if binder and isinstance(sent, int) and not isinstance(sent, bool):
                    binder(
                        str(_value(current, "request_id")),
                        int(_value(current, "revision", 0)),
                        sent,
                        now,
                    )
            return True

        expected_states = {"delivery_phone", "pickup_phone", "delivery_location"}
        if state not in expected_states:
            # Customers often send contact details before choosing delivery or
            # pickup. Keep explicit, safely parsed values without advancing or
            # guessing any catalogue choice.
            phone, source = phone_from_message(raw_message, text)
            parsed = location_from_message(raw_message, text, expected=False)
            if not phone and not parsed:
                return False
            changes: dict[str, Any] = {"state": state or "model"}
            selections: dict[str, Any] = {}
            if phone:
                changes.update(phone=phone, contact_method=source or "typed")
            if parsed:
                changes.update(location_url=parsed.url, address=parsed.address)
                selections["outside_tashkent"] = parsed.outside_tashkent
            updated = self.repo.update_business_request(
                str(_value(request, "request_id")),
                int(_value(request, "revision")),
                now,
                changes,
                selections=selections or None,
                event_type="early_details_received",
                event_key=f"message:{message_id}:early-details",
                telegram_update_id=update_id,
                telegram_message_id=message_id,
                client_at=event_at,
            )
            if not updated:
                return True
            self.repo.cancel(f"final:{session_id}")
            step = self._product_screen_for_request(updated, language)
            acknowledgement = self.service._render_message(
                "data_added", language, now,
            )
            if acknowledgement:
                step = WizardStep(
                    step.code,
                    acknowledgement + "\n\n" + step.text,
                    step.choices,
                    step.keyboard,
                    step.parse_mode,
                )
            if wizard_message_id:
                self._edit_screen(
                    updated, step, connection_id, chat_id,
                    wizard_message_id, now,
                )
            else:
                self.service.send(
                    connection_id,
                    chat_id,
                    session_id,
                    step.text,
                    "data_added",
                    now,
                    parse_mode=step.parse_mode,
                    reply_markup=self.markup(updated, step, now),
                    delivery_key=f"client-message:{message_id}:request-details",
                )
            return True

        if state in {"delivery_phone", "pickup_phone"}:
            phone, source = phone_from_message(raw_message, text)
            if not phone:
                step = (
                    build_delivery_phone_step(language)
                    if state == "delivery_phone"
                    else self._pickup_phone_step(language)
                )
                invalid = self.service._render_message(
                    "request_invalid_phone", language, now,
                )
                if invalid:
                    step = WizardStep(
                        step.code, invalid + "\n\n" + step.text,
                        step.choices, step.keyboard,
                    )
                if wizard_message_id:
                    self._edit_screen(
                        request, step, connection_id, chat_id,
                        wizard_message_id, now,
                    )
                else:
                    self.service.send(
                        connection_id, chat_id, session_id, step.text,
                        "request_invalid_phone", now,
                        parse_mode=step.parse_mode,
                        reply_markup=self.markup(request, step, now),
                        delivery_key=f"client-message:{message_id}:invalid-phone",
                    )
                return True
            has_location = bool(
                _value(request, "location_url") or _value(request, "address")
            )
            next_state = (
                "review"
                if state == "pickup_phone" or has_location
                else "delivery_location"
            )
            updated = self.repo.update_business_request(
                str(_value(request, "request_id")),
                int(_value(request, "revision")),
                now,
                state=next_state,
                status="ready" if next_state == "review" else "collecting",
                phone=phone,
                contact_method=source or "typed",
                event_type="phone_received",
                event_key=f"message:{message_id}:phone",
                telegram_update_id=update_id,
                telegram_message_id=message_id,
                client_at=event_at,
            )
            if not updated:
                return True
            step = (
                build_delivery_location_step(language)
                if next_state == "delivery_location"
                else self._review_step(updated, language)
            )
        else:
            parsed = location_from_message(raw_message, text, expected=True)
            if not parsed:
                step = build_delivery_location_step(language)
                invalid = self.service._render_message(
                    "request_invalid_location", language, now,
                )
                if invalid:
                    step = WizardStep(
                        step.code, invalid + "\n\n" + step.text,
                        step.choices, step.keyboard,
                    )
                if wizard_message_id:
                    self._edit_screen(
                        request, step, connection_id, chat_id,
                        wizard_message_id, now,
                    )
                else:
                    self.service.send(
                        connection_id, chat_id, session_id, step.text,
                        "request_invalid_location", now,
                        parse_mode=step.parse_mode,
                        reply_markup=self.markup(request, step, now),
                        delivery_key=f"client-message:{message_id}:invalid-location",
                    )
                return True
            updated = self.repo.update_business_request(
                str(_value(request, "request_id")),
                int(_value(request, "revision")),
                now,
                state="review",
                status="ready",
                location_url=parsed.url,
                address=parsed.address,
                selections={"outside_tashkent": parsed.outside_tashkent},
                event_type="location_received",
                event_key=f"message:{message_id}:location",
                telegram_update_id=update_id,
                telegram_message_id=message_id,
                client_at=event_at,
            )
            if not updated:
                return True
            step = self._review_step(updated, language)

        self.repo.cancel(f"final:{session_id}")
        if wizard_message_id:
            self._edit_screen(
                updated, step, connection_id, chat_id, wizard_message_id, now,
            )
        else:
            template_code = {
                "delivery_location": "request_delivery_location",
                "review": "request_review",
            }.get(step.code, "request_prompt")
            self.service.send(
                connection_id,
                chat_id,
                session_id,
                step.text,
                template_code,
                now,
                parse_mode="HTML",
                reply_markup=self.markup(updated, step, now),
                delivery_key=f"request:{_value(updated, 'request_id')}:rev:{_value(updated, 'revision')}",
            )
        return True

    def expire(self, request_id: str, connection_id: str, now: datetime) -> None:
        request = self.repo.business_request(request_id)
        if not request:
            return
        if _value(request, "status") in {"collecting", "ready"}:
            expired = self.repo.expire_business_request(
                request_id,
                int(_value(request, "revision")),
                now,
                event_key=f"night-end:{request_id}",
            )
        elif (
            _value(request, "status") == "expired"
            and _value(request, "close_reason") == "night_ended"
        ):
            # The DB transition can commit before a transient Telegram edit
            # fails. Replaying the durable action must still remove the stale
            # keyboard without changing request state a second time.
            expired = request
        else:
            return
        if not expired:
            return
        language = str(_value(expired, "language", "bi") or "bi")
        text = self._terminal_text(
            "request_partial_saved", expired, language, now,
        )
        fields = selection_fields(expired)
        message_id = fields.get("wizard_message_id")
        chat_id = str(_value(expired, "chat_id"))
        session_id = str(_value(expired, "session_id", ""))
        policy = self.service._runtime_policy(now)
        within_reply_window = getattr(
            self.service, "_within_reply_window", None,
        )
        may_edit = (
            connection_id == str(self.service.settings.allowed_connection_id)
            and self.service._connection_allows_reply(connection_id)
            and self.repo.may_automate(chat_id, now)
            and (
                not hasattr(self.repo, "session_may_automate")
                or self.repo.session_may_automate(session_id)
            )
            and not self.service._manager_fence_active(chat_id, now, policy)
            and (
                within_reply_window is None
                or within_reply_window(chat_id, session_id, now)
            )
        )
        if message_id and may_edit:
            self.api.edit_message_text(
                connection_id,
                chat_id,
                int(message_id),
                text,
                reply_markup={"inline_keyboard": []},
            )

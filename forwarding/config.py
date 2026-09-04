import os

from dataclasses import dataclass, field
from typing import Mapping


@dataclass(frozen=True)
class OperatorConfig:
    code: str
    name: str
    enable_template: str
    disable_number: str


@dataclass(frozen=True)
class DeviceConfig:
    code: str
    name: str
    moizvonki_user: str
    operator_code: str
    sim_slot: int
    sim_number: str
    controls_enabled: bool


@dataclass(frozen=True)
class RouteConfig:
    source_code: str
    target_code: str
    target_number: str


@dataclass(frozen=True)
class ForwardingSettings:
    enabled: bool
    post_hour: int
    post_minute: int
    poll_seconds: float
    command_cooldown_seconds: int
    confirmation_timeout_seconds: int
    correlation_window_seconds: int
    admin_ids: frozenset[int]
    device_controller_ids: Mapping[str, frozenset[int]] = field(
        default_factory=dict
    )

    def can_control(self, actor_id: int, source_code: str) -> bool:
        return (
            actor_id in self.admin_ids
            or actor_id
            in self.device_controller_ids.get(source_code, frozenset())
        )


OPERATOR = OperatorConfig(
    code="configured_sim",
    name="Настроенный оператор SIM",
    enable_template="**21*{target}#",
    disable_number="##21#",
)


DEVICES = {
    "redmi": DeviceConfig(
        code="redmi",
        name="Redmi",
        moizvonki_user="aashshdjdjdjsj@gmail.com",
        operator_code=OPERATOR.code,
        sim_slot=0,
        sim_number="+998908534466",
        controls_enabled=True,
    ),
    "tecno": DeviceConfig(
        code="tecno",
        name="Tecno",
        moizvonki_user="texnikacholx@gmail.com",
        operator_code=OPERATOR.code,
        sim_slot=0,
        sim_number="+998908456162",
        controls_enabled=True,
    ),
    "poco": DeviceConfig(
        code="poco",
        name="Poco",
        moizvonki_user="texnikach@gmail.com",
        operator_code=OPERATOR.code,
        # Poco is a Dual-SIM phone. Public calls.make_call documentation
        # exposes no SIM selector, so it intentionally has no controls yet.
        sim_slot=1,
        sim_number="+998901313999",
        controls_enabled=False,
    ),
}


ROUTES = {
    ("redmi", "poco"): RouteConfig(
        source_code="redmi",
        target_code="poco",
        target_number="+998901313999",
    ),
    ("redmi", "tecno"): RouteConfig(
        source_code="redmi",
        target_code="tecno",
        target_number="+998908456162",
    ),
    ("tecno", "poco"): RouteConfig(
        source_code="tecno",
        target_code="poco",
        target_number="+998901313999",
    ),
    ("tecno", "redmi"): RouteConfig(
        source_code="tecno",
        target_code="redmi",
        target_number="+998908534466",
    ),
}


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(
        name,
        "true" if default else "false",
    )
    return str(raw).strip().casefold() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _env_int(
    name: str,
    default: int,
    minimum: int,
    maximum: int,
) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default
    return value if minimum <= value <= maximum else default


def _env_float(
    name: str,
    default: float,
    minimum: float,
    maximum: float,
) -> float:
    try:
        value = float(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default
    return value if minimum <= value <= maximum else default


def _post_clock() -> tuple[int, int]:
    raw = os.getenv(
        "FORWARDING_POST_TIME",
        "20:00",
    ).strip()
    try:
        hours_raw, minutes_raw = raw.split(":", 1)
        hours = int(hours_raw)
        minutes = int(minutes_raw)
        if 0 <= hours <= 23 and 0 <= minutes <= 59:
            return hours, minutes
    except (TypeError, ValueError):
        pass
    return 20, 0


def _parse_ids(raw: str) -> frozenset[int]:
    result = set()
    for item in str(raw).split(","):
        try:
            value = int(item.strip())
        except (TypeError, ValueError):
            continue
        if value > 0:
            result.add(value)
    return frozenset(result)


def _admin_ids() -> frozenset[int]:
    # Abbos always retains full access, even if extra administrators are
    # configured through the environment.
    return frozenset(
        {202134293}
        | set(
            _parse_ids(
                os.getenv(
                    "FORWARDING_ADMIN_IDS",
                    "202134293",
                )
            )
        )
    )


def _device_controller_ids() -> Mapping[str, frozenset[int]]:
    defaults = {
        "redmi": frozenset({7636344727}),
        "tecno": frozenset({702960146}),
    }
    result = {}
    for device_code, default_ids in defaults.items():
        env_name = (
            "FORWARDING_"
            f"{device_code.upper()}_CONTROLLER_IDS"
        )
        raw = os.getenv(
            env_name,
            ",".join(str(item) for item in sorted(default_ids)),
        )
        parsed = _parse_ids(raw)
        result[device_code] = parsed or default_ids
    return result


def load_forwarding_settings() -> ForwardingSettings:
    post_hour, post_minute = _post_clock()
    return ForwardingSettings(
        enabled=_env_bool(
            "FORWARDING_ENABLED",
            True,
        ),
        post_hour=post_hour,
        post_minute=post_minute,
        poll_seconds=_env_float(
            "FORWARDING_POLL_SECONDS",
            2.0,
            0.5,
            60.0,
        ),
        command_cooldown_seconds=_env_int(
            "FORWARDING_COMMAND_COOLDOWN_SECONDS",
            90,
            10,
            3600,
        ),
        confirmation_timeout_seconds=_env_int(
            "FORWARDING_CONFIRM_TIMEOUT_SECONDS",
            120,
            30,
            1800,
        ),
        correlation_window_seconds=_env_int(
            "FORWARDING_CORRELATION_WINDOW_SECONDS",
            3600,
            120,
            86400,
        ),
        admin_ids=_admin_ids(),
        device_controller_ids=_device_controller_ids(),
    )

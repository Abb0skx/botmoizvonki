from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class CourierOption:
    user_id: int
    name: str
    group_id: int


COURIERS: tuple[CourierOption, ...] = (
    CourierOption(user_id=7636344727, name="Olmas", group_id=-5111626405),
    CourierOption(user_id=202134293, name="Abbos", group_id=-5216093690),
    CourierOption(user_id=1799690992, name="Muzrob Oka", group_id=-5125237049),
)
COURIERS_BY_ID = {courier.user_id: courier for courier in COURIERS}


def courier_option(user_id: int | None) -> CourierOption | None:
    return COURIERS_BY_ID.get(user_id)


def courier_group_id(user_id: int | None) -> int | None:
    option = courier_option(user_id)
    return option.group_id if option else None


def courier_ids() -> frozenset[int]:
    return frozenset(COURIERS_BY_ID)


def courier_group_ids() -> frozenset[int]:
    return frozenset(courier.group_id for courier in COURIERS)

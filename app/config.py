from dataclasses import dataclass
from pathlib import Path
import os

from dotenv import load_dotenv


def _ids(value: str) -> frozenset[int]:
    try:
        return frozenset(int(item.strip()) for item in value.split(",") if item.strip())
    except ValueError as error:
        raise RuntimeError("Telegram IDs must be integers separated by commas") from error


@dataclass(frozen=True)
class Settings:
    bot_token: str
    delivery_group_id: int
    location_channel_id: int
    database_path: Path
    manager_ids: frozenset[int]
    courier_ids: frozenset[int]

    @classmethod
    def load(cls) -> "Settings":
        load_dotenv()
        token = os.getenv("DELIVERY_BOT_TOKEN", "").strip()
        group_id = os.getenv("DELIVERY_GROUP_ID", "").strip()
        location_channel_id = os.getenv("DELIVERY_LOCATION_CHANNEL_ID", "").strip()
        if not token:
            raise RuntimeError("DELIVERY_BOT_TOKEN is not set")
        if not group_id:
            raise RuntimeError("DELIVERY_GROUP_ID is not set")
        if not location_channel_id:
            raise RuntimeError("DELIVERY_LOCATION_CHANNEL_ID is not set")
        try:
            parsed_group_id = int(group_id)
            parsed_location_channel_id = int(location_channel_id)
        except ValueError as error:
            raise RuntimeError("Telegram chat IDs must be integers") from error
        if not str(parsed_location_channel_id).startswith("-100"):
            raise RuntimeError("DELIVERY_LOCATION_CHANNEL_ID must be a channel or supergroup ID starting with -100")
        manager_ids = _ids(os.getenv("DELIVERY_MANAGER_IDS", ""))
        courier_ids = _ids(os.getenv("DELIVERY_COURIER_IDS", ""))
        if not manager_ids:
            raise RuntimeError("DELIVERY_MANAGER_IDS must contain at least one Telegram user ID")
        if not courier_ids:
            raise RuntimeError("DELIVERY_COURIER_IDS must contain at least one Telegram user ID")
        return cls(
            bot_token=token,
            delivery_group_id=parsed_group_id,
            location_channel_id=parsed_location_channel_id,
            database_path=Path(os.getenv("DELIVERY_DB_PATH", "data/delivery.db")),
            manager_ids=manager_ids,
            courier_ids=courier_ids,
        )

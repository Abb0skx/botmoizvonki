from dataclasses import dataclass, field
import os


@dataclass
class Settings:
    database_url: str = 'sqlite:////data/inbox.db'
    meta_app_secret: str = ''
    meta_access_token: str = ''
    meta_verify_token: str = ''
    instagram_account_id: str = ''
    graph_base_url: str = 'https://graph.instagram.com/v26.0'
    telegram_bot_token: str = ''
    telegram_group_id: int = 0
    manager_ids: tuple[int, ...] = ()
    rules_sheet_id: str = '1ZdSyTJr9jSBdBDUZowXi2CpjTb7GZMQCQNsMRCMjywk'
    direct_rules_sheet_name: str = 'direct_rules'
    direct_settings_sheet_name: str = 'direct_settings'
    products_sheet_id: str = '1TrS6C4oHe6nzQTPTa_4se_upXBFF6rmbfnE7RqznR8U'
    products_sheet_name: str = 'bot_prices'
    product_settings_sheet_name: str = 'bot_settings'
    sheets_cache_seconds: int = 60
    context_messages: int = 20
    debounce_seconds: float = 4
    close_after_hours: float = 72
    legacy_webhook_url: str = ''
    worker_enabled: bool = True
    worker_lock_path: str = '/data/worker.lock'
    log_level: str = 'INFO'

    @classmethod
    def from_env(cls):
        return cls(
            database_url=os.getenv('DATABASE_URL', 'sqlite:////data/inbox.db'),
            meta_app_secret=os.getenv('META_APP_SECRET', ''),
            meta_access_token=os.getenv('META_ACCESS_TOKEN', ''),
            meta_verify_token=os.getenv('META_VERIFY_TOKEN', ''),
            instagram_account_id=os.getenv('INSTAGRAM_ACCOUNT_ID', ''),
            graph_base_url=os.getenv('META_GRAPH_BASE_URL', 'https://graph.instagram.com/v26.0'),
            telegram_bot_token=os.getenv('TELEGRAM_BOT_TOKEN', ''),
            telegram_group_id=int(os.getenv('TELEGRAM_GROUP_ID', '0')),
            manager_ids=tuple(int(x) for x in os.getenv('TELEGRAM_MANAGER_IDS', '').split(',') if x.strip()),
            rules_sheet_id=os.getenv('INSTAGRAM_RULES_SHEET_ID', cls.rules_sheet_id),
            direct_rules_sheet_name=os.getenv('INSTAGRAM_DIRECT_RULES_SHEET_NAME', 'direct_rules'),
            direct_settings_sheet_name=os.getenv('INSTAGRAM_DIRECT_SETTINGS_SHEET_NAME', 'direct_settings'),
            products_sheet_id=os.getenv('INSTAGRAM_PRODUCTS_SHEET_ID', cls.products_sheet_id),
            products_sheet_name=os.getenv('INSTAGRAM_PRODUCTS_SHEET_NAME', 'bot_prices'),
            product_settings_sheet_name=os.getenv('INSTAGRAM_SETTINGS_SHEET_NAME', 'bot_settings'),
            sheets_cache_seconds=max(5, min(240, int(os.getenv('SHEETS_CACHE_SECONDS', '60')))),
            context_messages=max(2, min(100, int(os.getenv('CONTEXT_MESSAGES', '20')))),
            debounce_seconds=max(0, float(os.getenv('INCOMING_DEBOUNCE_SECONDS', '4'))),
            close_after_hours=float(os.getenv('TOPIC_CLOSE_AFTER_HOURS', '72')),
            legacy_webhook_url=os.getenv('LEGACY_INSTAGRAM_WEBHOOK_URL', ''),
            worker_enabled=os.getenv('WORKER_ENABLED', 'true').lower() == 'true',
            worker_lock_path=os.getenv('WORKER_LOCK_PATH', '/data/worker.lock'),
        )

    def validate(self):
        missing = [name for name in ('meta_app_secret', 'meta_access_token', 'meta_verify_token',
                   'instagram_account_id', 'telegram_bot_token', 'telegram_group_id') if not getattr(self, name)]
        if missing:
            raise ValueError('Missing configuration: ' + ', '.join(missing))

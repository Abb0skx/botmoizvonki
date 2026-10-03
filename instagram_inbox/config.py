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
    openai_api_key: str = ''
    openai_model: str = 'gpt-4.1-mini'
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
            openai_api_key=os.getenv('OPENAI_API_KEY', ''),
            openai_model=os.getenv('OPENAI_MODEL', 'gpt-4.1-mini'),
            context_messages=max(2, min(100, int(os.getenv('AI_CONTEXT_MESSAGES', '20')))),
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

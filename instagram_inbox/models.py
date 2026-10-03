import time
from sqlalchemy import BigInteger, Boolean, Float, ForeignKey, Integer, JSON, String, Text, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class Client(Base):
    __tablename__ = 'inbox_clients'
    id: Mapped[int] = mapped_column(primary_key=True)
    channel: Mapped[str] = mapped_column(String(32), default='instagram')
    account_id: Mapped[str] = mapped_column(String(80))
    external_id: Mapped[str] = mapped_column(String(80))
    username: Mapped[str] = mapped_column(String(150), default='')
    display_name: Mapped[str] = mapped_column(String(150), default='')
    topic_id: Mapped[int | None] = mapped_column(Integer, unique=True)
    topic_state: Mapped[str] = mapped_column(String(32), default='new')
    topic_name: Mapped[str] = mapped_column(String(128), default='')
    status: Mapped[str] = mapped_column(String(32), default='active')
    revision: Mapped[int] = mapped_column(default=0)
    classified_revision: Mapped[int] = mapped_column(default=0)
    due_at: Mapped[float] = mapped_column(Float, default=0, index=True)
    last_message_at: Mapped[float] = mapped_column(Float, default=time.time)
    last_customer_at: Mapped[float] = mapped_column(Float, default=0)
    created_at: Mapped[float] = mapped_column(Float, default=time.time)
    updated_at: Mapped[float] = mapped_column(Float, default=time.time)
    __table_args__ = (UniqueConstraint('channel', 'account_id', 'external_id'),)


class Message(Base):
    __tablename__ = 'inbox_messages'
    id: Mapped[int] = mapped_column(primary_key=True)
    client_id: Mapped[int] = mapped_column(ForeignKey('inbox_clients.id'), index=True)
    external_id: Mapped[str | None] = mapped_column(String(512), unique=True)
    telegram_message_id: Mapped[int | None] = mapped_column(Integer, index=True)
    telegram_state: Mapped[str] = mapped_column(String(32), default='pending')
    reply_to_external_id: Mapped[str] = mapped_column(String(512), default='')
    direction: Mapped[str] = mapped_column(String(16))
    sender_type: Mapped[str] = mapped_column(String(16))
    message_type: Mapped[str] = mapped_column(String(32), default='text')
    text: Mapped[str] = mapped_column(Text, default='')
    attachments: Mapped[list] = mapped_column(JSON, default=list)
    raw: Mapped[dict] = mapped_column(JSON, default=dict)
    classification: Mapped[dict | None] = mapped_column(JSON)
    requires_reply: Mapped[bool | None] = mapped_column(Boolean)
    revision: Mapped[int] = mapped_column(default=0)
    created_at: Mapped[float] = mapped_column(Float, default=time.time)


class Draft(Base):
    __tablename__ = 'inbox_drafts'
    id: Mapped[int] = mapped_column(primary_key=True)
    client_id: Mapped[int] = mapped_column(ForeignKey('inbox_clients.id'), index=True)
    source_message_id: Mapped[int] = mapped_column(ForeignKey('inbox_messages.id'))
    revision: Mapped[int] = mapped_column()
    text: Mapped[str] = mapped_column(Text)
    category: Mapped[str] = mapped_column(String(64))
    origin: Mapped[str] = mapped_column(String(16), default='template')
    status: Mapped[str] = mapped_column(String(24), default='pending', index=True)
    telegram_message_id: Mapped[int | None] = mapped_column(Integer)
    presentation_state: Mapped[str] = mapped_column(String(24), default='pending')
    manager_id: Mapped[int | None] = mapped_column(BigInteger)
    manager_name: Mapped[str] = mapped_column(String(180), default='')
    external_message_id: Mapped[str] = mapped_column(String(512), default='')
    error: Mapped[str] = mapped_column(Text, default='')
    retry_safe: Mapped[bool] = mapped_column(Boolean, default=False)
    dispatch_started_at: Mapped[float | None] = mapped_column(Float)
    created_at: Mapped[float] = mapped_column(Float, default=time.time)
    updated_at: Mapped[float] = mapped_column(Float, default=time.time)
    sent_at: Mapped[float | None] = mapped_column(Float)
    replaces_id: Mapped[int | None] = mapped_column(Integer, unique=True)


class Manager(Base):
    __tablename__ = 'inbox_managers'
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    username: Mapped[str] = mapped_column(String(150), default='')
    first_name: Mapped[str] = mapped_column(String(180), default='')
    updated_at: Mapped[float] = mapped_column(Float, default=time.time)


class Job(Base):
    __tablename__ = 'inbox_jobs'
    id: Mapped[int] = mapped_column(primary_key=True)
    key: Mapped[str] = mapped_column(String(512), unique=True)
    kind: Mapped[str] = mapped_column(String(32))
    payload: Mapped[dict] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String(24), default='pending', index=True)
    attempts: Mapped[int] = mapped_column(default=0)
    due_at: Mapped[float] = mapped_column(Float, default=time.time, index=True)
    error: Mapped[str] = mapped_column(Text, default='')
    updated_at: Mapped[float] = mapped_column(Float, default=time.time)


class State(Base):
    __tablename__ = 'inbox_state'
    key: Mapped[str] = mapped_column(String(80), primary_key=True)
    value: Mapped[str] = mapped_column(Text)


class TelegramLink(Base):
    __tablename__ = 'inbox_telegram_links'
    telegram_id: Mapped[int] = mapped_column(primary_key=True, autoincrement=False)
    message_id: Mapped[int] = mapped_column(ForeignKey('inbox_messages.id'))

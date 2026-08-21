"""Изолированный модуль клиентских отзывов Texnikach."""

from .database import init_reviews_db

__all__ = ["init_reviews_db"]

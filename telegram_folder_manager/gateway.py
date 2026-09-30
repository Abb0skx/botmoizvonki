from __future__ import annotations

import asyncio
import logging
import os
from typing import Any, Iterable

from .config import FolderSettings, FolderSpec


LOG = logging.getLogger("telegram_folder_manager.gateway")


class TelegramFolderGateway:
    """Narrow MTProto adapter: dialog filters only, no message mutations."""

    def __init__(self, settings: FolderSettings, *, client: Any = None):
        self.settings = settings
        self.client = client
        self._lock = asyncio.Lock()
        self._self_peer = None
        self._self_id: int | None = None

    async def connect(self) -> None:
        if self.client is None:
            from telethon import TelegramClient

            self.settings.session_path.parent.mkdir(parents=True, exist_ok=True)
            old_umask = os.umask(0o077)
            try:
                self.client = TelegramClient(
                    str(self.settings.session_path),
                    self.settings.api_id,
                    self.settings.api_hash,
                    device_model="TEXNIKACH Folder Manager",
                    system_version="server",
                    app_version="1.0",
                    receive_updates=False,
                )
            finally:
                os.umask(old_umask)
            # Entity rows may contain usernames and phone numbers.  Keep only
            # the authorization key in the protected session file.
            self.client.session.save_entities = False
        await self.client.connect()
        if not await self.client.is_user_authorized():
            raise RuntimeError(
                "Telegram user session is not authorized; run "
                "python -m telegram_folder_manager.auth first"
            )
        me = await self.client.get_me()
        self._self_id = int(me.id)
        self._self_peer = await self.client.get_input_entity("me")
        for candidate in self.settings.session_path.parent.glob(
            self.settings.session_path.name + "*"
        ):
            if candidate.is_file():
                candidate.chmod(0o600)

    async def disconnect(self) -> None:
        if self.client is not None:
            await self.client.disconnect()

    @staticmethod
    def _title(folder: Any) -> str:
        title = getattr(folder, "title", "")
        return str(getattr(title, "text", title) or "")

    async def _filters(self) -> tuple[Any, list[Any]]:
        from telethon.tl.functions.messages import GetDialogFiltersRequest

        result = await self.client(GetDialogFiltersRequest())
        filters = list(getattr(result, "filters", result) or [])
        return result, filters

    def _managed(self, filters: Iterable[Any]) -> dict[str, Any]:
        by_title = {
            self._title(folder): folder
            for folder in filters
            if hasattr(folder, "id") and hasattr(folder, "exclude_peers")
        }
        return {
            spec.code: by_title[spec.title]
            for spec in self.settings.folders
            if spec.title in by_title
        }

    @staticmethod
    def _peer_id(peer: Any) -> int | None:
        try:
            from telethon import utils

            return int(utils.get_peer_id(peer))
        except Exception:
            value = getattr(peer, "user_id", None)
            return int(value) if value is not None else None

    @classmethod
    def _contains(cls, peers: Iterable[Any], peer_id: int) -> bool:
        return any(cls._peer_id(peer) == peer_id for peer in peers)

    @classmethod
    def _without(cls, peers: Iterable[Any], peer_id: int) -> list[Any]:
        return [peer for peer in peers if cls._peer_id(peer) != peer_id]

    @staticmethod
    def _next_filter_id(filters: Iterable[Any]) -> int:
        used = {int(folder.id) for folder in filters if hasattr(folder, "id")}
        for value in range(2, 256):
            if value not in used:
                return value
        raise RuntimeError("Telegram account has no free dialog-filter ID")

    @staticmethod
    def _new_filter(spec: FolderSpec, filter_id: int, peers: list[Any]):
        from telethon.tl import types

        return types.DialogFilter(
            id=filter_id,
            title=types.TextWithEntities(spec.title, []),
            pinned_peers=[],
            include_peers=peers,
            exclude_peers=[],
            color=spec.color,
        )

    @staticmethod
    def _clone_filter(folder: Any, *, pinned_peers, include_peers, exclude_peers):
        from telethon.tl import types

        return types.DialogFilter(
            id=int(folder.id),
            title=folder.title,
            pinned_peers=list(pinned_peers),
            include_peers=list(include_peers),
            exclude_peers=list(exclude_peers),
            contacts=getattr(folder, "contacts", None),
            non_contacts=getattr(folder, "non_contacts", None),
            groups=getattr(folder, "groups", None),
            broadcasts=getattr(folder, "broadcasts", None),
            bots=getattr(folder, "bots", None),
            exclude_muted=getattr(folder, "exclude_muted", None),
            exclude_read=getattr(folder, "exclude_read", None),
            exclude_archived=getattr(folder, "exclude_archived", None),
            title_noanimate=getattr(folder, "title_noanimate", None),
            emoticon=getattr(folder, "emoticon", None),
            color=getattr(folder, "color", None),
        )

    async def ensure_folders(self) -> None:
        """Create all manager folders, using Saved Messages as safe sentinel."""
        from telethon.tl.functions.messages import (
            ToggleDialogFilterTagsRequest,
            UpdateDialogFilterRequest,
        )

        async with self._lock:
            result, filters = await self._filters()
            if not bool(getattr(result, "tags_enabled", False)):
                await self.client(ToggleDialogFilterTagsRequest(enabled=True))
            managed_titles = {spec.title for spec in self.settings.folders}
            incompatible = [
                self._title(folder)
                for folder in filters
                if self._title(folder) in managed_titles
                and not hasattr(folder, "exclude_peers")
            ]
            if incompatible:
                raise RuntimeError(
                    "a shared Telegram folder uses a managed title: "
                    + ", ".join(sorted(incompatible))
                )
            managed = self._managed(filters)
            for spec in self.settings.folders:
                if spec.code in managed:
                    current = managed[spec.code]
                    pinned = list(getattr(current, "pinned_peers", ()) or ())
                    included = list(getattr(current, "include_peers", ()) or ())
                    excluded = list(getattr(current, "exclude_peers", ()) or ())
                    changed = False
                    if not self._contains(pinned + included, self._self_id):
                        included.append(self._self_peer)
                        changed = True
                    cleaned = self._without(excluded, self._self_id)
                    changed = changed or len(cleaned) != len(excluded)
                    excluded = cleaned
                    changed = changed or getattr(current, "color", None) != spec.color
                    if changed:
                        replacement = self._clone_filter(
                            current,
                            pinned_peers=pinned,
                            include_peers=included,
                            exclude_peers=excluded,
                        )
                        replacement.color = spec.color
                        await self.client(
                            UpdateDialogFilterRequest(
                                id=int(current.id), filter=replacement
                            )
                        )
                        managed[spec.code] = replacement
                    continue
                filter_id = self._next_filter_id(filters)
                folder = self._new_filter(spec, filter_id, [self._self_peer])
                await self.client(
                    UpdateDialogFilterRequest(id=filter_id, filter=folder)
                )
                filters.append(folder)
                managed[spec.code] = folder
                LOG.info(
                    "telegram_folder_created code=%s filter_id=%s",
                    spec.code, filter_id,
                )

    async def _input_peer(self, chat_id: str):
        numeric = int(chat_id)
        try:
            return await self.client.get_input_entity(numeric)
        except (ValueError, TypeError):
            # GetDialogs does not mark messages read.  It is used only to obtain
            # the access hash required to construct an InputPeerUser.
            async for dialog in self.client.iter_dialogs():
                if int(getattr(dialog, "id", 0)) == numeric:
                    return dialog.input_entity
        raise LookupError("Telegram dialog is not available to the authorized account")

    async def move(self, chat_id: str, folder_code: str) -> None:
        from telethon.tl.functions.messages import UpdateDialogFilterRequest

        async with self._lock:
            _, filters = await self._filters()
            managed = self._managed(filters)
            if len(managed) != len(self.settings.folders):
                # Avoid recursive lock acquisition; callers initialize folders
                # before processing jobs. A deleted folder is recreated inline.
                for spec in self.settings.folders:
                    if spec.code not in managed:
                        filter_id = self._next_filter_id(filters)
                        folder = self._new_filter(spec, filter_id, [self._self_peer])
                        await self.client(
                            UpdateDialogFilterRequest(id=filter_id, filter=folder)
                        )
                        filters.append(folder)
                        managed[spec.code] = folder

            peer = await self._input_peer(chat_id)
            peer_id = self._peer_id(peer)
            if peer_id is None:
                raise LookupError("Telegram peer ID could not be resolved")
            for spec in self.settings.folders:
                current = managed[spec.code]
                pinned = list(getattr(current, "pinned_peers", ()) or ())
                included = list(getattr(current, "include_peers", ()) or ())
                excluded = list(getattr(current, "exclude_peers", ()) or ())
                changed = False
                if spec.code == folder_code:
                    if not self._contains(included, peer_id) and not self._contains(pinned, peer_id):
                        included.append(peer)
                        changed = True
                    cleaned = self._without(excluded, peer_id)
                    changed = changed or len(cleaned) != len(excluded)
                    excluded = cleaned
                else:
                    new_pinned = self._without(pinned, peer_id)
                    new_included = self._without(included, peer_id)
                    changed = (
                        len(new_pinned) != len(pinned)
                        or len(new_included) != len(included)
                    )
                    pinned, included = new_pinned, new_included
                if not changed:
                    continue
                replacement = self._clone_filter(
                    current,
                    pinned_peers=pinned,
                    include_peers=included,
                    exclude_peers=excluded,
                )
                await self.client(
                    UpdateDialogFilterRequest(id=int(current.id), filter=replacement)
                )
                managed[spec.code] = replacement
            LOG.info(
                "telegram_folder_moved chat_id=%s folder=%s", chat_id, folder_code
            )

    async def snapshot(self) -> dict[str, set[str]]:
        async with self._lock:
            _, filters = await self._filters()
            managed = self._managed(filters)
            memberships: dict[str, set[str]] = {}
            for code, folder in managed.items():
                peers = list(getattr(folder, "pinned_peers", ()) or ()) + list(
                    getattr(folder, "include_peers", ()) or ()
                )
                for peer in peers:
                    peer_id = self._peer_id(peer)
                    # Bot clients are private user dialogs. Telegram represents
                    # groups/channels with negative peer IDs; a user may add
                    # those to a folder manually, so ignore them safely.
                    if peer_id is None or peer_id <= 0 or peer_id == self._self_id:
                        continue
                    memberships.setdefault(str(peer_id), set()).add(code)
            return memberships

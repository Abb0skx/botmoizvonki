"""Read-only shared-group membership lookup for Business chat classification."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class SupplierScan:
    members: dict[str, str]
    private_dialog_ids: frozenset[str]
    complete: bool
    groups_found: int
    groups_expected: int
    unavailable: tuple[str, ...]


async def scan_supplier_groups(
    client: Any, group_ids: tuple[int, ...], *, dialog_limit: int = 25000,
    self_user_id: int | None = None,
) -> SupplierScan:
    """Collect current user IDs without reading or marking group messages."""
    wanted = set(group_ids)
    groups: dict[int, Any] = {}
    private_dialog_ids: set[str] = set()
    dialog_count = 0
    async for dialog in client.iter_dialogs(limit=dialog_limit):
        dialog_count += 1
        if (
            bool(getattr(dialog, "is_user", False))
            and int(dialog.id) > 0
            and int(dialog.id) != self_user_id
        ):
            private_dialog_ids.add(str(dialog.id))
        if bool(getattr(dialog, "is_group", False)) and int(dialog.id) in wanted:
            groups[int(dialog.id)] = dialog

    members: dict[str, str] = {}
    unavailable = [str(group_id) for group_id in group_ids if group_id not in groups]
    if dialog_count >= dialog_limit:
        unavailable.append("dialog_scan_limit")
    for group_id in group_ids:
        dialog = groups.get(group_id)
        if dialog is None:
            continue
        count = 0
        try:
            async for user in client.iter_participants(dialog.input_entity):
                user_id = getattr(user, "id", None)
                if isinstance(user_id, int) and user_id > 0 and user_id != self_user_id:
                    members.setdefault(str(user_id), str(group_id))
                    count += 1
        except Exception as exc:
            unavailable.append(f"{group_id}:{type(exc).__name__}")
            continue
        expected = getattr(getattr(dialog, "entity", None), "participants_count", None)
        if isinstance(expected, int) and expected > count:
            unavailable.append(f"{group_id}:partial_{count}_of_{expected}")
    return SupplierScan(
        members=members,
        private_dialog_ids=frozenset(private_dialog_ids),
        complete=not unavailable,
        groups_found=len(groups),
        groups_expected=len(group_ids),
        unavailable=tuple(unavailable),
    )

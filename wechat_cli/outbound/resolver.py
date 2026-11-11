from __future__ import annotations

from collections.abc import Iterable, Mapping

from .models import OutboundError, ResolvedTarget, TargetResolutionError


def _display_name(contact: Mapping[str, object]) -> str:
    return str(
        contact.get("remark")
        or contact.get("nick_name")
        or contact.get("username")
        or ""
    ).strip()


def _resolved(contact: Mapping[str, object]) -> ResolvedTarget:
    username = str(contact.get("username") or "").strip()
    return ResolvedTarget(
        username=username,
        display_name=_display_name(contact) or username,
        kind="group" if "@chatroom" in username else "private",
    )


def resolve_target_from_contacts(
    query: str,
    contacts: Iterable[Mapping[str, object]],
) -> ResolvedTarget:
    requested = str(query or "").strip()
    if not requested:
        raise TargetResolutionError(
            OutboundError.TARGET_NOT_FOUND,
            "Target is required.",
        )

    rows = [contact for contact in contacts if str(contact.get("username") or "").strip()]
    stable_username = requested.startswith("wxid_") or "@chatroom" in requested

    if stable_username:
        matches = [
            contact
            for contact in rows
            if str(contact.get("username") or "").strip() == requested
        ]
    else:
        folded = requested.casefold()
        matches = []
        for contact in rows:
            remark = str(contact.get("remark") or "").strip()
            nickname = str(contact.get("nick_name") or "").strip()
            if (remark and remark.casefold() == folded) or (
                nickname and nickname.casefold() == folded
            ):
                matches.append(contact)

    by_username = {
        str(contact.get("username") or "").strip(): contact
        for contact in matches
    }
    unique = list(by_username.values())

    if not unique:
        raise TargetResolutionError(
            OutboundError.TARGET_NOT_FOUND,
            f"Target not found: {requested}",
        )
    if len(unique) > 1:
        raise TargetResolutionError(
            OutboundError.TARGET_AMBIGUOUS,
            f"Target is ambiguous: {requested}",
        )

    return _resolved(unique[0])

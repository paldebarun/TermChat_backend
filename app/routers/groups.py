import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app import auth, crud
from app.config import get_settings
from app.database import get_db
from app.models import Group, User
from app.schemas import (
    GroupCreate,
    GroupEvent,
    GroupMemberOut,
    GroupMembersAdd,
    GroupOut,
    GroupSummary,
    GroupUpdate,
)
from app.websocket_manager import manager

router = APIRouter(prefix="/groups", tags=["groups"])
settings = get_settings()


def _to_out(group: Group) -> GroupOut:
    creator = next((m.user.username for m in group.members if m.user_id == group.created_by), "unknown")
    return GroupOut(
        id=group.id,
        name=group.name,
        created_by=creator,
        created_at=group.created_at,
        members=[
            GroupMemberOut(
                username=m.user.username, role=m.role, public_key=m.user.public_key, joined_at=m.joined_at
            )
            for m in group.members
        ],
    )


async def _notify(group: Group, event: GroupEvent, extra_usernames: list[str] | None = None) -> None:
    """Push a group_event to every current member (plus e.g. a member who was
    just removed). Live only: offline members re-sync with GET /groups."""
    targets = {m.user.username for m in group.members} | set(extra_usernames or [])
    payload = event.model_dump_json()
    for username in targets:
        await manager.send_to_user(username, payload)


async def _member_group_or_404(db: AsyncSession, group_id: uuid.UUID, user: User) -> Group:
    group = await crud.get_group(db, group_id)
    # Same 404 for "no such group" and "not yours", like uploads.
    if group is None or not any(m.user_id == user.id for m in group.members):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Group not found")
    return group


def _require_admin(group: Group, user: User) -> None:
    if not any(m.user_id == user.id and m.role == "admin" for m in group.members):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Only group admins can do this")


async def _resolve_users(db: AsyncSession, usernames: list[str]) -> list[User]:
    """Usernames -> active users, de-duplicated in order; 400 if any is unknown."""
    users: list[User] = []
    unknown: list[str] = []
    seen: set[str] = set()
    for name in usernames:
        if name in seen:
            continue
        seen.add(name)
        user = await crud.get_user_by_username(db, name)
        if user is None or not user.is_active:
            unknown.append(name)
        else:
            users.append(user)
    if unknown:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Unknown users: {unknown}")
    return users


def _check_size(count: int) -> None:
    if count > settings.group_max_members:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"A group can have at most {settings.group_max_members} members",
        )


@router.post("", response_model=GroupOut, status_code=status.HTTP_201_CREATED)
async def create_group(
    data: GroupCreate,
    current_user: User = Depends(auth.get_current_user),
    db: AsyncSession = Depends(get_db),
):
    others = [u for u in await _resolve_users(db, data.members) if u.id != current_user.id]
    _check_size(len(others) + 1)
    group = await crud.create_group(db, data.name, current_user, others)
    await _notify(group, GroupEvent(group_id=group.id, event="created", actor=current_user.username))
    return _to_out(group)


@router.get("", response_model=list[GroupSummary])
async def list_groups(
    current_user: User = Depends(auth.get_current_user),
    db: AsyncSession = Depends(get_db),
):
    groups = await crud.list_groups_for_user(db, current_user.id)
    return [
        GroupSummary(
            id=g.id,
            name=g.name,
            created_at=g.created_at,
            member_count=len(g.members),
            my_role=next(m.role for m in g.members if m.user_id == current_user.id),
        )
        for g in groups
    ]


@router.get("/{group_id}", response_model=GroupOut)
async def get_group(
    group_id: uuid.UUID,
    current_user: User = Depends(auth.get_current_user),
    db: AsyncSession = Depends(get_db),
):
    return _to_out(await _member_group_or_404(db, group_id, current_user))


@router.patch("/{group_id}", response_model=GroupOut)
async def rename_group(
    group_id: uuid.UUID,
    data: GroupUpdate,
    current_user: User = Depends(auth.get_current_user),
    db: AsyncSession = Depends(get_db),
):
    group = await _member_group_or_404(db, group_id, current_user)
    _require_admin(group, current_user)
    group = await crud.rename_group(db, group, data.name)
    await _notify(group, GroupEvent(group_id=group.id, event="renamed", actor=current_user.username))
    return _to_out(group)


@router.post("/{group_id}/members", response_model=GroupOut)
async def add_members(
    group_id: uuid.UUID,
    data: GroupMembersAdd,
    current_user: User = Depends(auth.get_current_user),
    db: AsyncSession = Depends(get_db),
):
    group = await _member_group_or_404(db, group_id, current_user)
    _require_admin(group, current_user)

    existing = {m.user_id for m in group.members}
    new_users = [u for u in await _resolve_users(db, data.usernames) if u.id not in existing]
    _check_size(len(existing) + len(new_users))
    if new_users:
        group = await crud.add_group_members(db, group, new_users)
        for user in new_users:
            await _notify(
                group,
                GroupEvent(
                    group_id=group.id, event="member_added", actor=current_user.username, username=user.username
                ),
            )
    return _to_out(group)


@router.delete("/{group_id}/members/{username}", status_code=status.HTTP_204_NO_CONTENT)
async def remove_member(
    group_id: uuid.UUID,
    username: str,
    current_user: User = Depends(auth.get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Admins remove anyone; any member may remove themselves (leave)."""
    group = await _member_group_or_404(db, group_id, current_user)
    leaving_self = username == current_user.username
    if not leaving_self:
        _require_admin(group, current_user)

    target = next((m for m in group.members if m.user.username == username), None)
    if target is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Member not found")

    members_before = [m.user.username for m in group.members]
    updated = await crud.remove_group_member(db, group, target.user_id)
    event = GroupEvent(
        group_id=group_id,
        event="member_left" if leaving_self else "member_removed",
        actor=current_user.username,
        username=username,
    )
    if updated is not None:
        await _notify(updated, event, extra_usernames=[username])
    else:  # last member left; the group is gone
        for name in members_before:
            await manager.send_to_user(name, event.model_dump_json())

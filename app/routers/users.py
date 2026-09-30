from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app import auth, crud
from app.database import get_db
from app.models import User
from app.schemas import PublicKeyUpdate, UserPublic

router = APIRouter(prefix="/users", tags=["users"])


@router.get("/me", response_model=UserPublic)
async def read_me(current_user: User = Depends(auth.get_current_user)):
    return current_user


@router.put("/me/public-key", response_model=UserPublic)
async def update_public_key(
    data: PublicKeyUpdate,
    current_user: User = Depends(auth.get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Upload the client-generated RSA public key. Call this right after
    signup, once the client has generated a keypair locally - see
    client/crypto_utils.py::generate_keypair(). The private key must never
    be sent here or anywhere else."""
    return await crud.set_public_key(db, current_user, data.public_key)


@router.get("/{username}/public-key", response_model=UserPublic)
async def get_public_key(
    username: str,
    db: AsyncSession = Depends(get_db),
    _current_user: User = Depends(auth.get_current_user),
):
    user = await crud.get_user_by_username(db, username)
    if user is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")
    return user

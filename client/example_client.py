"""
End-to-end demo of the full flow: signup -> login -> upload public key ->
send an encrypted message -> recipient receives and decrypts it.

Run the server first (docker compose up), then in a separate shell:

    cd client
    pip install -r requirements.txt
    python example_client.py

This spins up two demo users (alice/bob), so it's runnable standalone with
no arguments.
"""
import asyncio
import json
import uuid
from datetime import datetime, timezone

import requests
import websockets

from crypto_utils import decrypt_message, encrypt_message, generate_keypair

BASE_URL = "http://localhost:8000"
WS_URL = "ws://localhost:8000/ws"


def signup_and_login(username: str, email: str, password: str) -> tuple[str, str, str]:
    """Returns (access_token, private_key_pem, public_key_pem)."""
    private_key_pem, public_key_pem = generate_keypair()

    resp = requests.post(
        f"{BASE_URL}/auth/signup",
        json={"username": username, "email": email, "password": password},
    )
    if resp.status_code not in (201, 400):  # 400 = already exists, fine for a repeatable demo
        resp.raise_for_status()

    resp = requests.post(
        f"{BASE_URL}/auth/login",
        data={"username": username, "password": password},
    )
    resp.raise_for_status()
    access_token = resp.json()["access_token"]

    resp = requests.put(
        f"{BASE_URL}/users/me/public-key",
        headers={"Authorization": f"Bearer {access_token}"},
        json={"public_key": public_key_pem},
    )
    resp.raise_for_status()

    return access_token, private_key_pem, public_key_pem


def fetch_public_key(access_token: str, username: str) -> str:
    resp = requests.get(
        f"{BASE_URL}/users/{username}/public-key",
        headers={"Authorization": f"Bearer {access_token}"},
    )
    resp.raise_for_status()
    return resp.json()["public_key"]


async def run_demo():
    alice_token, alice_private, _ = signup_and_login("alice", "alice@example.com", "correct-horse-1")
    bob_token, bob_private, bob_public = signup_and_login("bob", "bob@example.com", "correct-horse-2")

    # Alice looks up Bob's public key (server only ever holds public keys).
    bob_public_from_server = fetch_public_key(alice_token, "bob")
    assert bob_public_from_server == bob_public

    plaintext = "Hey Bob, this message is end-to-end encrypted!"
    encrypted = encrypt_message(bob_public_from_server, plaintext)

    payload = {
        "type": "message",
        "message_id": str(uuid.uuid4()),
        "recipient": "bob",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        **encrypted,  # encrypted_content, encrypted_key, nonce, tag
    }

    async with websockets.connect(f"{WS_URL}?token={bob_token}") as bob_ws, \
            websockets.connect(f"{WS_URL}?token={alice_token}") as alice_ws:

        await alice_ws.send(json.dumps(payload))

        raw = await asyncio.wait_for(bob_ws.recv(), timeout=5)
        received = json.loads(raw)

        decrypted = decrypt_message(bob_private, received)

        print("Server only ever saw ciphertext:")
        print(f"  encrypted_content = {received['encrypted_content'][:40]}...")
        print(f"Bob decrypted it locally to: {decrypted!r}")
        assert decrypted == plaintext


if __name__ == "__main__":
    asyncio.run(run_demo())

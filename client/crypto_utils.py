"""
Reference implementation of the client-side end-to-end encryption scheme
used by this chat service.

This is the piece that actually makes the chat "end-to-end encrypted":
the server (see app/main.py) only ever stores and relays the four opaque
fields produced by encrypt_message() below. It never sees plaintext and
has no private key that could decrypt it - only the two people at either
end of a conversation can.

Scheme: hybrid RSA-OAEP + AES-256-GCM (the standard pattern for encrypting
arbitrary-length messages with a slow-but-key-exchange-friendly public-key
algorithm).

  1. Generate a random 256-bit AES key, unique to this one message.
  2. Encrypt the plaintext with AES-256-GCM using that key
     -> ciphertext + 16-byte auth tag.
  3. Encrypt the AES key itself with the recipient's RSA-2048 public key
     (RSA-OAEP) -> encrypted_key.
  4. Send {encrypted_content, encrypted_key, nonce, tag} to the server.

The recipient reverses the process with their own RSA private key, which
must never leave their device (never uploaded, never logged, never sent
over the network in any form).
"""
import base64
import os

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

_TAG_LENGTH = 16  # bytes, fixed by AES-GCM


def generate_keypair() -> tuple[str, str]:
    """Generate a new RSA-2048 keypair.

    Returns (private_key_pem, public_key_pem).

    Keep private_key_pem on the client only - store it in the OS keychain,
    an encrypted local file, etc. Upload only public_key_pem, via
    PUT /users/me/public-key.
    """
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)

    private_pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()

    public_pem = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode()

    return private_pem, public_pem


def encrypt_message(recipient_public_key_pem: str, plaintext: str) -> dict:
    """Encrypt `plaintext` for the holder of the given RSA public key.

    Returns a dict with the four fields the server expects:
    encrypted_content, encrypted_key, nonce, tag (all base64-encoded str).
    """
    public_key = serialization.load_pem_public_key(recipient_public_key_pem.encode())

    aes_key = AESGCM.generate_key(bit_length=256)
    nonce = os.urandom(12)
    aesgcm = AESGCM(aes_key)

    ciphertext_and_tag = aesgcm.encrypt(nonce, plaintext.encode(), None)
    ciphertext, tag = ciphertext_and_tag[:-_TAG_LENGTH], ciphertext_and_tag[-_TAG_LENGTH:]

    encrypted_key = public_key.encrypt(
        aes_key,
        padding.OAEP(
            mgf=padding.MGF1(algorithm=hashes.SHA256()),
            algorithm=hashes.SHA256(),
            label=None,
        ),
    )

    return {
        "encrypted_content": base64.b64encode(ciphertext).decode(),
        "encrypted_key": base64.b64encode(encrypted_key).decode(),
        "nonce": base64.b64encode(nonce).decode(),
        "tag": base64.b64encode(tag).decode(),
    }


def decrypt_message(private_key_pem: str, payload: dict) -> str:
    """Reverse of encrypt_message(), using the recipient's own private key."""
    private_key = serialization.load_pem_private_key(private_key_pem.encode(), password=None)

    aes_key = private_key.decrypt(
        base64.b64decode(payload["encrypted_key"]),
        padding.OAEP(
            mgf=padding.MGF1(algorithm=hashes.SHA256()),
            algorithm=hashes.SHA256(),
            label=None,
        ),
    )

    nonce = base64.b64decode(payload["nonce"])
    ciphertext = base64.b64decode(payload["encrypted_content"])
    tag = base64.b64decode(payload["tag"])

    aesgcm = AESGCM(aes_key)
    plaintext = aesgcm.decrypt(nonce, ciphertext + tag, None)
    return plaintext.decode()

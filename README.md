# WebSocket Chat — FastAPI + Postgres + E2E Encryption

A realtime chat backend: JWT auth (signup/login/refresh), Postgres
persistence via async SQLAlchemy, multi-connection-aware websocket relay,
true end-to-end encrypted message content, resumable file attachments on
SeaweedFS (S3), and an opt-in, chat-scoped AI assistant (Hermes agent over
MCP, with document search in Chroma).

## Why "end-to-end" requires a client piece

End-to-end encryption means the **server** can never read message content
- only the sender and recipient can. That's only possible if encryption
and decryption happen on the clients, using keys the server never has.
So this project is split accordingly:

- **Server** (`app/`): stores each user's RSA *public* key, and stores/
  relays message payloads that are already ciphertext by the time they
  arrive. It cannot decrypt anything — there is no decryption code path
  in the server at all.
- **Client** (`client/crypto_utils.py`): a reference implementation of the
  encryption scheme (hybrid RSA-OAEP + AES-256-GCM) that any real client
  (web, mobile, CLI) should implement. `client/example_client.py` is a
  runnable demo of the full flow between two users.

If you only deploy `app/` and never encrypt on a client, you'd have a
normal (still password/JWT-secured, still TLS-in-transit-if-you-put-it-
behind HTTPS) chat server — not an E2E encrypted one. The encryption is
what the client does before the ciphertext ever reaches the server.

## Architecture

```
app/
  main.py              FastAPI app, lifespan (creates tables), /ws endpoint
  config.py            Settings from environment (.env)
  database.py          Async engine/session, Base
  models.py            User, Message, UploadedFile, AssistantRun ORM models
  schemas.py           Pydantic request/response/websocket schemas
  auth.py              Password hashing, JWT issue/verify
  crud.py              DB access functions
  storage.py           S3 (SeaweedFS) multipart upload + presigned URLs
  websocket_manager.py Tracks live connections per user (multi-device aware)
  routers/
    auth.py            POST /auth/signup, /auth/login, /auth/refresh
    users.py           GET/PUT public-key registry, GET /users/me
    uploads.py         Resumable multipart attachments
    assistant.py       POST /assistant/query
  assistant/
    service.py         Runs one agent job (subprocess), trims/passes history
    hermes_worker.py   Hermes agent entrypoint (separate venv)
    prompts.py         System prompt (scope, security, earlier turns)
    mcp_server.py      MCP tools: chat context/search, document search/read
    mcp_http.py        Auth boundary for /mcp (signed per-run scope token)
    scope.py           Conversation id + scope tokens
    cache.py           In-memory, per-run store for client-decrypted messages
    indexing.py        Background document indexing after a message is sent
    document_extract.py Text extraction + chunking
    vectorstore.py     Chroma client
tests/                 pytest suite (assistant, API contract)
client/
  crypto_utils.py      generate_keypair / encrypt_message / decrypt_message
  example_client.py    End-to-end demo: signup -> key upload -> encrypted send/receive
```

### Data model

- `users`: id, username, email, hashed_password (bcrypt), public_key (PEM,
  nullable until the client uploads one), is_active, created_at.
- `messages`: id, sender_id, recipient_id, encrypted_content, encrypted_key,
  nonce, tag (all ciphertext/opaque), delivered, created_at.

### Auth flow

1. `POST /auth/signup` — create an account (bcrypt-hashed password).
2. `POST /auth/login` — OAuth2 password flow, returns a short-lived access
   token (default 30 min) and a longer-lived refresh token (default 7 days).
3. `POST /auth/refresh` — exchange a refresh token for a new token pair.
4. Every authenticated HTTP call uses `Authorization: Bearer <access_token>`.
5. The websocket authenticates via `?token=<access_token>` query param at
   connect time (there's no way to send headers from a browser
   `new WebSocket(...)` call, which is why it's a query param here).

Tokens are stateless JWTs (HS256). There's no server-side revocation list
in this version — for production, consider a short access-token lifetime
(already the default) plus a revoked-refresh-token table/Redis set if you
need immediate logout-everywhere semantics.

### Message flow

1. Client encrypts locally with the recipient's public key
   (`client/crypto_utils.encrypt_message`).
2. Client sends the ciphertext payload over the websocket.
3. Server looks up the recipient, persists the message, and:
   - forwards it immediately if the recipient has an open connection, or
   - leaves it `delivered=False` and flushes it to them the next time they
     connect (see the "flush undelivered" block in `main.py`).
4. Recipient decrypts locally with their private key
   (`client/crypto_utils.decrypt_message`).

## File attachments (resumable multipart upload)

Large attachments never go through the websocket or FastAPI's own request
body. FastAPI is the **control plane** (auth, session tracking, presigned
URLs); SeaweedFS is the **data plane** (actual bytes, direct from the browser).

```
Browser → POST /uploads/init            → FastAPI creates a session, returns
                                           a batch of presigned PUT URLs
Browser → PUT <presigned url>           → SeaweedFS, one request per chunk,
                                           N at a time (bounded concurrency,
                                           implemented client-side)
Browser → POST /uploads/{id}/parts      → FastAPI, only if more URLs are
                                           needed than the initial batch
Browser → GET  /uploads/{id}/status     → FastAPI/SeaweedFS, to resume after a
                                           dropped connection (diff against
                                           missing_part_numbers)
Browser → POST /uploads/{id}/complete   → FastAPI finalizes the object in
                                           SeaweedFS; upload_id becomes file_id
Browser → send websocket message with   → {"attachment": {"file_id": "...",
           attachment.file_id                "filename": "doc.pdf"}}
```

### Endpoints

| Method | Path | Description |
|--------|------|--------------|
| POST   | `/uploads/init` | Start a session; returns `upload_id`, `total_parts`, and a first batch of presigned PUT URLs |
| POST   | `/uploads/{upload_id}/parts` | Batch-fetch presigned URLs for specific part numbers |
| GET    | `/uploads/{upload_id}/status` | List which parts SeaweedFS actually has (drives resume) |
| POST   | `/uploads/{upload_id}/complete` | Finalize; body is the list of `{part_number, etag}` |
| POST   | `/uploads/{upload_id}/abort` | Cancel an in-progress upload (multipart abort, not per-part delete) |
| DELETE | `/uploads/{upload_id}` | Remove an already-completed-but-unattached upload |
| GET    | `/uploads/{upload_id}/download-url` | Presigned GET URL, for the uploader or a message recipient |

### What the frontend still needs to implement

- An `UploadManager` with bounded concurrency (e.g. 4 workers) pulling from
  a queue of parts, using `File.slice()` for chunks and `AbortController`
  for cancellation — see the flow diagram above.
- Independent per-part retry (don't restart the whole file on one failed
  chunk).
- Persisting `upload_id` locally so an interrupted upload can call
  `/status` and resume instead of restarting.
- Calling `/uploads/{id}/abort` when a user removes an in-progress
  attachment, or `DELETE /uploads/{id}` if it had already finished.
- Sending the `attachment` field on the websocket message only after
  `/complete` succeeds — text and file upload are independent, so the user
  can keep typing while a large file finishes in the background.

### Why two SeaweedFS endpoints

`app/storage.py` uses two boto3 clients on purpose:

- `S3_INTERNAL_ENDPOINT` (`http://seaweedfs:8333`) — the backend's own calls
  over the docker network (create/list/complete/abort multipart upload).
- `S3_PUBLIC_ENDPOINT` (`http://localhost:8333` in dev) — used *only* to
  generate presigned URLs. SigV4 presigned URLs sign the `Host` header, so
  the URL must be generated against the host the browser will actually hit,
  or SeaweedFS rejects the signature. In production, point this at your public
  SeaweedFS/S3 domain.

SeaweedFS's server-side CORS (`-s3.allowedOrigins`, set in
`docker-compose.yml`) allows the browser to `PUT` directly to those
presigned URLs. Narrow it from `*` to your real frontend origin(s) before
going to production.

### Orphan cleanup

A background task (`app/main.py::_orphan_cleanup_loop`) runs every
`ORPHAN_CLEANUP_INTERVAL_MINUTES` and deletes any `COMPLETED` upload that's
older than `ORPHAN_CLEANUP_THRESHOLD_MINUTES` and was never attached to a
message (browser closed, attachment removed after upload finished, etc).
It's a straightforward `asyncio.create_task` loop for a single replica —
running several app replicas would want this moved to a proper scheduled
job (cron, Celery beat) so it only runs once.

## AI assistant

A chat-scoped assistant answers questions about **one** conversation (you +
a peer) and the files shared in it. Answers go back over HTTP to the asker
only; they are never stored as chat messages (the frontend can send one as a
normal encrypted message if the user wants to share it).

```
Browser -> POST /assistant/query -> FastAPI creates an assistant_runs row and
           a signed, run-scoped token, then starts the Hermes agent as a
           subprocess. The agent calls back to /mcp (bearer = scope token)
           to use its tools, then the answer is returned.
```

**What the agent can see**

- **Shared documents** - attachments between the two users are indexed in
  the background (Chroma) after the message is sent, so a just-sent file may
  take a moment to become searchable. Indexed: PDF (<= 500 pages), DOCX,
  JSON/JSONL, CSV, TXT/MD and common code files; <= 25 MB and <= 500,000
  characters of text.
- **Peer chat messages (opt-in)** - the server only stores ciphertext, so the
  client must send decrypted recent messages as `message_context` (max 100).
  They are held in memory for that single run and discarded. This is ignored
  unless `ASSISTANT_ALLOW_MESSAGE_CONTEXT=true`. Enabling it sends decrypted
  text to the server and the LLM provider (OpenRouter); get user consent.
- **Earlier assistant turns** - `assistant_history` carries the user's own
  previous questions and the assistant's answers (max 20 items, newest within
  `ASSISTANT_MAX_HISTORY_BYTES`, default 40 KB) so follow-ups like "shorten
  that" work. Always honoured; not stored beyond the run.

**Security**

- Each run gets a short-lived token bound to a user, peer and run id; `/mcp`
  rejects it once the run is no longer `RUNNING`.
- The agent subprocess gets an allowlisted environment (no JWT, DB or S3
  secrets) and only the MCP toolset.
- Document and chat text is treated as untrusted data; markdown links and
  images are stripped from answers (no zero-click exfiltration).
- The question and answer are stored in plaintext in `assistant_runs`;
  tool-call audit rows keep only sizes and hashes.

Limits: ~30 s queueing + ~120 s run (clients should time out at >= 160 s),
`ASSISTANT_MAX_CONCURRENT_RUNS` parallel runs; 429 when busy, 504 on timeout,
502 on agent failure. `GET`/`HEAD /mcp/` returning 405 in the logs is normal
(the MCP client probing for an optional server-push stream).

See `backend-contract.md` section 8 for the full request/response contract.

## Running it

```bash
cp .env.example .env
# edit .env and set a real JWT_SECRET_KEY:
python -c "import secrets; print(secrets.token_urlsafe(64))"

docker compose up --build
```

Services started by Compose: `app` (API/websocket, `:8000`, docs at `/docs`),
`db` (Postgres, `127.0.0.1:5432`), `seaweedfs` (S3 on `:8333`) and `chroma`
(vector store, internal only). To use the assistant, set `OPENROUTER_API_KEY`
and `ASSISTANT_SCOPE_TOKEN_SECRET` in `.env`, and set
`ASSISTANT_ALLOW_MESSAGE_CONTEXT=true` if the assistant should see chat text.

SeaweedFS credentials live in `seaweedfs/s3.json` and must match
`S3_ACCESS_KEY`/`S3_SECRET_KEY`. After a restart SeaweedFS can log
`raft.Server: Not current leader` for a few seconds while the master
re-elects itself; the compose file sets `-master.electionTimeout=1s` so this
settles in ~2 s. Don't Ctrl+C during it - and wiping `seaweedfs_data` is not
needed.

Run the tests with `pip install -r requirements-dev.txt && pytest tests`.

Try the full flow:

```bash
cd client
pip install -r requirements.txt
python example_client.py
```

## API summary

| Method | Path                        | Auth | Description |
|--------|-----------------------------|------|--------------|
| GET    | `/`                         | none | Health check |
| POST   | `/auth/signup`              | none | Create account |
| POST   | `/auth/login`               | none | Get access + refresh tokens |
| POST   | `/auth/refresh`             | none | Rotate tokens |
| GET    | `/users/me`                 | bearer | Current user profile |
| PUT    | `/users/me/public-key`      | bearer | Upload your RSA public key |
| GET    | `/users/{username}/public-key` | bearer | Look up someone's public key |
| POST   | `/uploads/init`, `/uploads/{id}/...` | bearer | Resumable attachments (see above) |
| POST   | `/assistant/query`          | bearer | Ask the chat-scoped assistant |
| WS     | `/ws?token=<access_token>`  | query token | Send/receive encrypted messages |

## Notes for going further

- **Migrations**: schema is created via `create_all` at startup for
  simplicity. Swap in Alembic before you need to evolve the schema without
  dropping data. Note `create_all` only creates *missing* tables — if
  you're adding attachments to an already-running deployment (existing
  `messages` table), you'll need to `ALTER TABLE messages ADD COLUMN
  attachment_file_id UUID REFERENCES uploaded_files(id) ON DELETE SET
  NULL, ADD COLUMN attachment_filename VARCHAR(255)` yourself, or just
  drop and let `create_all` rebuild on a fresh dev database.
- **Scaling out**: `ConnectionManager` keeps connections in-process. Running
  more than one app replica needs a pub/sub layer (Redis, NATS, Postgres
  `LISTEN/NOTIFY`) so a message can reach a recipient connected to a
  different replica.
- **Key rotation / multi-device**: this scheme assumes one keypair per
  user. Supporting multiple devices per user with proper E2E semantics
  (e.g. Signal-style per-device sessions) is a significantly bigger design
  — treat this as a solid single-keypair-per-user baseline.
- **Rate limiting / abuse protection**: not included; add something like
  `slowapi` in front of `/auth/*` in a real deployment.

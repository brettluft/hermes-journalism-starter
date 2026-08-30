# Publishing contract

## Spacefast API verification

Verified on **2026-08-30** against the official [setup contract](https://spacefast.com/setup.md) and [OpenAPI document](https://api.spacefast.com/openapi.json).

The authenticated direct operation is `POST https://api.spacefast.com/v1/publish` with `Authorization: Bearer <SPACEFAST_TOKEN>`. The OpenAPI operation uses bearer authentication and documents the `spaces:publish` / `publish:write` scopes. There is no anonymous fallback in this workflow. Redirects are not documented as required and the client disables them.

The request is `multipart/form-data` with:

- one `payload` JSON string containing `publishMode: snapshot`, explicit `teamId`, `space.title`, `channel: live`, and a static `config`;
- optional `spaceId` in that same payload to update exactly one existing Space;
- one `files` part containing only the generated rendition, with filename/path `index.html`.

New authenticated Spaces are Private by default according to the operation description. The client does not add a public visibility override. Direct publication omits `async`, so finalization is awaited by default. Every request has a deterministic `Idempotency-Key` no longer than 255 characters, tied to draft ID, new-versus-existing target, and rendition SHA-256. An update failure is final for that attempt: it must never cause a create fallback.

A direct success is HTTP 201 with `application/json`. Before recording success, validate `data.space.id`, `data.space.liveUrl`, `data.version.id`, `data.version.immutableUrl`, and terminal `activation`/`next` state. An update response ID must exactly equal the locally stored `spaceId`.

## Limits and failure behavior

The implementation permits a rendition of at most 8 MiB, a UTF-8 title of at most 4096 bytes, and a response of at most 1 MiB. It passes a finite, bounded 10-second timeout to `urllib`; this is a per-blocking-I/O/socket timeout, **not** a total wall-clock deadline for the whole publish operation. As a concrete mitigation for a peer that makes slow incremental progress, the timeout is deliberately small and every response read is capped at 1 MiB plus one detection byte. The standard-library transport used here does not cleanly provide an independently testable total request deadline, so callers must not treat the 10-second value as one. The client accepts only the constant HTTPS API URL and refuses all redirects, final URL changes, wrong status, wrong content type, invalid UTF-8/JSON, malformed response shape, unsafe returned URLs, and nonterminal finalization.

Errors expose stable codes only. Do not persist or print authorization headers, environment credentials, raw response bodies, `data.access` tokens, claim keys or claim URLs, or transport exception details.

## Local authority and metadata

`/opt/data/newsroom/drafts/<id>/source.txt` and its generated rendition remain canonical. Publication reads local state and never writes remote content back. `publication.json` may contain only draft/schema identity and allowlisted Spacefast publication state: the exact Space ID, safe live/immutable URLs, rendition SHA-256, and integer publication timestamp. Snapshot read, remote publication, and atomic metadata replacement are one exclusive transaction on the existing stable per-draft lock; local saves and another publication of that draft wait, while another draft has an independent lock. A directory-fsync failure after metadata replacement is reported as `publication_commit_uncertain`, never as an ordinary preservation failure. If locked read-back proves the intended complete metadata is visible, remote success is returned with `durability: uncertain`; this does not claim durable success.

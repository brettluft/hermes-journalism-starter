# Draft Library and Spacefast implementation plan

> **For Hermes:** Use subagent-driven-development to implement this plan task by task. Follow RED-GREEN-REFACTOR within each task, do not weaken the existing strict configuration or startup no-write guarantees, and stop for review at each checkpoint.

**Goal:** Add a built-in private Draft Library and authenticated Spacefast publishing while keeping every generated draft canonically and durably stored under `/opt/data/newsroom/drafts`.

**Architecture:** A bundled `draft-publishing` skill drives a standard-library Python CLI. The CLI saves canonical source and generated static renditions locally, consults the optional strict `newsroom.json.drafts` policy, issues short-lived HMAC-signed Draft Library links, and sends only the static rendition to Spacefast's authenticated REST API. A separate standard-library HTTP server runs as an s6 service in the existing container, reads the HMAC key dynamically, exposes a public liveness endpoint, and serves only validated local renditions with valid signatures. The existing image entrypoint remains inherited, startup validates but never creates persistent state, and explicit `draft_publish.py init` is the only operation that creates the Draft Library key.

**Tech stack:** Python 3 standard library (`argparse`, `hashlib`, `hmac`, `html`, `http.server`, `json`, `os`, `pathlib`, `secrets`, `urllib`), JSON, `unittest`, s6-overlay service directories, Docker, Railway persistent volume and variables.

**Current baseline:** `python3 -m unittest discover -s tests -v` passes all 73 existing tests. Preserve that baseline throughout implementation.

---

## Product contract and security boundaries

### Persistent layout and authority

Use these production paths and no alternatives in public CLI behavior:

```text
/opt/data/newsroom/newsroom.json
/opt/data/newsroom/drafts/
  <draft-id>/
    source.txt
    rendition.html
    publication.json
/opt/data/newsroom/draft-library/
  hmac.key
```

- `/opt/data/newsroom/drafts` is always the canonical authority. A Spacefast space is a disposable publication snapshot, never the source of truth and never an input to a later local update.
- `draft-id` is a bounded ASCII identifier such as `^[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?$`; reject `.`, `..`, separators, percent-encoded separators, Unicode lookalikes, control characters, and overlong IDs.
- `source.txt` stores the generated draft exactly as accepted by the CLI. `rendition.html` is a deterministic, escaped static rendering. `publication.json` stores only non-secret local publication metadata, including the Spacefast `space_id` needed to update an existing space. It must never store `SPACEFAST_TOKEN`, the HMAC key, request headers, raw API responses, or newsroom configuration.
- All writes use fixed names beneath descriptor-opened, no-follow directories; reject symlinks, hard links, FIFOs/devices, unsafe ownership/type changes, and path traversal at every mutable or served boundary. Write temporary files in the destination directory, flush and `fsync`, atomically `os.replace`, then `fsync` the directory. Serialize competing writes with `flock` and leave a recoverable or unchanged state on failure.
- Test code may inject temporary roots through Python constructors/functions. The installed CLI must not expose `--home`, `--root`, arbitrary output paths, or environment overrides that move canonical production drafts outside `/opt/data/newsroom/drafts`.

### Configuration contract

Extend schema-version-1 `newsroom.json` with an optional exact-key `drafts` object:

```json
{
  "drafts": {
    "destination": "library",
    "publishing_policy": "ask_each_time",
    "spacefast_setup_status": "not_configured"
  }
}
```

- `destination` is exactly `library`, `spacefast`, or `both`.
- `publishing_policy` is exactly `ask_each_time`, `auto_private`, or `never`.
- `spacefast_setup_status` is exactly `not_configured` or `configured`.
- If `drafts` is absent, resolve backward-compatible defaults as `library`, `ask_each_time`, and `not_configured` without rewriting the existing file. New `newsroom_config.py init` output and the example template include those defaults.
- If `drafts` is present, require an object with exactly those three keys and string enum values. Reject missing keys, unknown keys, booleans, `null`, near-match casing, URLs, IDs, and credentials in this object. Keep the existing recursive secret-field rejection.
- Policy matrix:
  - `never`: refuse every operation that creates a share link or contacts Spacefast; local save/render remains allowed.
  - `ask_each_time`: require an explicit per-operation approval flag supplied only after the skill records the editor's approval; never infer approval from prior publication.
  - `auto_private`: permit automatic Draft Library publication only. Spacefast is an external publication and still requires explicit per-operation approval when `destination` is `spacefast` or `both`.
- `destination` selects the requested publication targets, but policy can narrow or refuse them. Do not silently fall back from Spacefast to anonymous/public upload or claim full success when one target fails.

### Draft Library HTTP boundary

- Bind the server in the same container under s6. `GET /healthz` is public and returns a small constant response that proves process liveness without disclosing setup, draft IDs, paths, config, key state, environment, or errors.
- Until explicit CLI setup has produced a valid key, every content route returns `503 Service Unavailable`; startup and health checks must not create directories, files, keys, drafts, or config.
- The only content route is `GET /draft/<draft-id>?expires=<unix-seconds>&sig=<lowercase-hex>`. Reject all other methods/routes/parameters. Do not provide listing, search, index, source download, directory redirect, debug, or write endpoints.
- Sign a documented canonical ASCII payload containing version, method, normalized route, and expiry with HMAC-SHA256. Compare with `hmac.compare_digest`. Require integer expiry strictly in the future and no more than 86,400 seconds from server time. Reject duplicate/missing query parameters, alternate encodings, fragments, malformed signatures, clock overflows, and signatures for a different method/path.
- Read and validate `hmac.key` on every signing/content operation so explicit setup or deliberate key rotation takes effect without container restart. Require a fixed high-entropy binary/key encoding and mode `0600`; reject symlink, hard-link, wrong-size, group/world-accessible, or non-regular keys. Never print or log the key or signature canonical payload.
- Read only `rendition.html` for the validated draft ID. Never serve `source.txt`, `publication.json`, `newsroom.json`, dotfiles, arbitrary MIME types, or error details. Use fixed generic 400/403/404/405/503 bodies and avoid draft-existence oracles before signature validation.
- Rendition generation escapes all source/title text and supports only an intentionally small, deterministic presentation subset. Do not pass through raw HTML, script, style, event attributes, URLs, iframes, SVG, forms, template directives, or Markdown HTML.
- Send `Content-Type: text/html; charset=utf-8`, `X-Content-Type-Options: nosniff`, `Referrer-Policy: no-referrer`, `Cache-Control: private, no-store`, a restrictive Content Security Policy such as `default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'`, and no server-version banner.
- Build public links only from an explicit validated `DRAFT_LIBRARY_BASE_URL`, otherwise from validated `RAILWAY_PUBLIC_DOMAIN` prefixed with `https://`. The explicit base URL must be an HTTPS origin with no credentials, query, fragment, or non-root path; Railway input must be a DNS hostname only, with no scheme, port, slash, whitespace, wildcard, or localhost/IP value. Fail closed if neither is valid. Never trust `Host`, `Forwarded`, or `X-Forwarded-*` headers.

### Spacefast boundary

- Bot automation uses only Spacefast's documented authenticated REST API and an injected `urllib` transport. Before coding the concrete paths/payload, verify the current official authenticated create/update contract and capture sanitized request/response fixtures in tests; centralize the verified API origin, route builders, timeout, and expected response fields in `spacefast_client.py`.
- Require Railway variables `SPACEFAST_TOKEN` and explicit `SPACEFAST_TEAM_ID` whenever Spacefast is selected. `spacefast_setup_status=configured` is editorial/setup state, not proof that runtime credentials exist. Missing/blank credentials cause a sanitized refusal before network I/O.
- Never use Spacefast's anonymous browser/upload flow, scrape a web form, derive a team from the token/account, or accept a token/team ID from Discord or command arguments. This is mandatory for shared Discord channels.
- Create a space only when local `publication.json` has no `space_id`. On later snapshots, update that exact `space_id`; never create a replacement merely because update failed. Verify response IDs match the requested/stored ID before atomically recording metadata.
- Send only the generated `rendition.html` plus the minimum required title/team/private-or-public API fields. Never upload `source.txt`, `publication.json`, `newsroom.json`, `sources.json`, prompts, Discord content, local paths, environment, HMAC material, config, or raw working files.
- Use HTTPS only, bounded request/response sizes, a finite timeout, JSON content-type checks, strict response shape/ID validation, sanitized errors, and no redirects to a different origin. Never log authorization headers, token-bearing data, raw response bodies, or unpublished draft content.

## Release acceptance criteria

1. Existing newsroom files without `drafts` still validate, apply, undo, and report status; effective defaults are available to draft publication without mutating those files.
2. Present `drafts` objects are strict and accept only the approved exact enums and keys. New defaults/templates document the contract.
3. Saving a generated draft produces canonical `source.txt`, safe deterministic `rendition.html`, and non-secret metadata only under `/opt/data/newsroom/drafts/<draft-id>`.
4. Neither container startup nor the Draft Library server writes anything beneath `/opt/data`; only explicit `draft_publish.py init` creates `/opt/data/newsroom/draft-library/hmac.key`, with safe atomic creation and mode `0600`.
5. `/healthz` remains public and constant before setup. Content is `503` before setup, and after setup only an unexpired correctly signed URL can retrieve one rendition. Link TTL cannot exceed 24 hours.
6. Traversal, symlink/hard-link, special-file, race, malformed-query, signature, method, MIME-sniffing, reflected/stored XSS, and error-disclosure tests pass.
7. Base URLs come only from valid `DRAFT_LIBRARY_BASE_URL` or `RAILWAY_PUBLIC_DOMAIN`, never request headers.
8. Spacefast publication requires authenticated REST, `SPACEFAST_TOKEN`, explicit `SPACEFAST_TEAM_ID`, configured status, destination/policy authorization, and per-operation approval where required. Anonymous publishing is impossible.
9. A second Spacefast snapshot updates the stored `space_id`; a failed update does not create a new space or change canonical local source/rendition/metadata.
10. Spacefast receives only the generated static rendition and minimum API metadata. Tests prove raw source, newsroom config, secrets, local paths, and publication metadata are absent from outbound requests.
11. s6 starts the HTTP server beside the inherited Hermes gateway without adding `ENTRYPOINT`, replacing `CMD`, or modifying the upstream setup ordering. The new run script performs no setup writes.
12. README, skill, configuration contract, environment example, and SECURITY documentation explain setup, policies, Railway variables/domain, private-link limitations, Spacefast's external trust boundary, rotation/revocation, and canonical-local authority.
13. All unit/integration tests, syntax checks, fixture checks, startup no-write checks, container build, container smoke tests, diff review, and secret scans pass.

---

### Task 1: Extend the strict newsroom configuration contract

**Objective:** Add backward-compatible draft preferences without relaxing any existing newsroom validation, transaction, audit, or startup behavior.

**Files:**
- Modify: `skills/newsroom-setup/scripts/newsroom_config.py`
- Modify: `tests/test_newsroom_config.py`
- Modify: `skills/newsroom-setup/templates/newsroom.example.json`
- Modify: `skills/newsroom-setup/references/configuration-contract.md`

**Step 1: Write failing configuration tests**

Add focused cases for:

- A legacy document with no `drafts` member remaining valid through `validate`, `apply`, `undo`, and `status`.
- A pure `effective_drafts_config(document)` helper returning the three backward-compatible defaults without mutating its input.
- New `init` output containing the default `drafts` object.
- Every valid enum combination.
- Rejection of a non-object, absent required key, extra key, wrong type, casing variant, unsupported enum, URL/team/space fields, and nested secret field.
- Existing revision, cross-file completion, atomic apply/undo, recovery, and content-hash-only audit behavior remaining unchanged when `drafts` is present.
- The example template passing the CLI validator and containing no secret or deployment-specific values.

**Step 2: Verify RED**

Run:

```bash
python3 -m unittest tests.test_newsroom_config -v
```

Expected: only the newly added draft-contract tests fail because defaults and strict validation are absent.

**Step 3: Implement the minimum contract**

Add immutable enum/default constants and a helper that validates an exact-key object or supplies a fresh default mapping for an absent object. Include defaults in `default_newsroom()`, but do not migrate/rewrite old files on read, status, startup, or draft publication. Call validation from `validate_newsroom()` while preserving the existing secret scan and transaction semantics. Update the example and contract with the policy matrix and make clear that credentials remain Railway variables, not JSON fields.

**Step 4: Verify GREEN and regressions**

Run:

```bash
python3 -m unittest tests.test_newsroom_config -v
python3 -m unittest discover -s tests -v
python3 -m json.tool skills/newsroom-setup/templates/newsroom.example.json >/dev/null
```

Inspect a temporary legacy apply/undo cycle and confirm the stored legacy file gains no `drafts` field unless the approved candidate itself includes one.

**Checkpoint:** Review only the four listed files. Do not proceed if any existing persistence or recovery test regresses.

---

### Task 2: Build canonical draft storage, safe rendering, key setup, and signed links

**Objective:** Implement one deterministic CLI/library layer that stores canonical drafts locally, generates inert static HTML, initializes the signing key only on explicit request, and enforces publication policy before issuing a link.

**Files:**
- Create: `skills/draft-publishing/scripts/draft_store.py`
- Create: `skills/draft-publishing/scripts/draft_publish.py`
- Create: `tests/test_draft_store.py`
- Create: `tests/test_draft_publish.py`

**CLI contract:**

```text
draft_publish.py init
draft_publish.py status
draft_publish.py save --id DRAFT_ID --title TITLE --input SOURCE_FILE
draft_publish.py link --id DRAFT_ID --ttl-seconds SECONDS [--editor-approved]
draft_publish.py publish --id DRAFT_ID [--editor-approved]
```

All commands print one sanitized JSON object and use nonzero status for refusal/validation/I/O/network failure. `save` is local only. `link` targets only the library. `publish` resolves configured destination and policy, invokes the library and/or Spacefast adapters, and reports each target separately; Spacefast wiring is completed in Task 4. Do not expose secrets, arbitrary roots/output paths, raw response data, source content, or tokens as options.

**Step 1: Write failing store and CLI tests**

Test directly with injected temporary path objects and clocks, and test public argument parsing as a subprocess. Require:

- Strict safe draft IDs and title/source byte limits; regular UTF-8 input only; malformed UTF-8 and Unicode surrogates fail without partial state.
- Fixed production defaults exactly matching the persistent layout above and no CLI option/environment variable that relocates them.
- `save` creates descriptor-relative safe directories/files with restrictive modes, stable escaping, and atomic replacement; repeated save updates the same ID rather than duplicating it.
- Rendering adversarial strings including `<script>`, raw HTML, event handlers, closing tags, entities, `javascript:`, CSS/SVG/form/iframe payloads, RTL/control characters, and malformed Markdown as visible escaped text, never executable markup.
- `publication.json` contains only an allowlisted schema and no raw source/config/secret values.
- Failure injection at create/write/fsync/replace stages leaves either the complete former snapshot or complete new snapshot, never mixed source/rendition metadata.
- Symlinks, hard links, special files, replaced parent directories, and concurrent writers are refused or serialized without touching an outside sentinel.
- `init` alone atomically creates a cryptographically random HMAC key at the fixed path with `0600`; repeat init preserves it; wrong-type/linked/permissive existing keys are refused; `status`, `save`, `link`, imports, and server startup do not create it.
- Signed link canonicalization, deterministic clock-injected signatures, TTL lower bound and maximum 86,400, dynamic key re-read, base URL precedence/validation, and no use of request headers.
- Policy matrix behavior, including explicit approval being consumed per command and `auto_private` never authorizing Spacefast.
- Public errors and JSON output omit paths, source text, title, key, signature payload, environment values, and tracebacks.

**Step 2: Verify RED**

Run:

```bash
python3 -m unittest tests.test_draft_store tests.test_draft_publish -v
```

Expected: import/file failures because the scripts do not exist.

**Step 3: Implement the minimum local pipeline**

Keep filesystem primitives and rendering in `draft_store.py`, with small injectable store/clock/randomness objects and fixed production factories. Reuse the proven descriptor-relative, `O_NOFOLLOW`, regular-file, link-count, flock, atomic-write, and directory-`fsync` patterns from `newsroom_config.py`; do not import its CLI module in a way that triggers output or initialization. Use `html.escape(..., quote=True)` and generate the complete HTML shell internally. Treat draft text as text: implement only explicit headings/paragraphs/lists if each token is escaped first; otherwise render escaped preformatted text.

In `draft_publish.py`, load and strictly validate `/opt/data/newsroom/newsroom.json`, resolve effective defaults, enforce the policy matrix, validate base URL from process environment, read the key at operation time, and sign the documented canonical payload. Use `secrets.token_bytes` only inside explicit `init`. Do not make imports, parser construction, status, or any read path create storage.

**Step 4: Verify GREEN and inspect artifacts**

Run:

```bash
python3 -m unittest tests.test_draft_store tests.test_draft_publish -v
python3 -m py_compile skills/draft-publishing/scripts/draft_store.py skills/draft-publishing/scripts/draft_publish.py
python3 -m unittest discover -s tests -v
```

In a temporary injected store, inspect modes and parse `publication.json`; open `rendition.html` as text and confirm every attacker-controlled value is escaped. Confirm production-path CLI tests mock filesystem entry points rather than writing to real `/opt/data/newsroom`.

**Checkpoint:** Review the storage and rendering attack surface before adding an HTTP listener. Every filesystem defect receives a regression test.

---

### Task 3: Add the private HTTP server and s6 integration

**Objective:** Serve only signed static renditions from a write-free s6-managed process while preserving the inherited Hermes entrypoint and gateway command.

**Files:**
- Create: `skills/draft-publishing/scripts/draft_library_server.py`
- Create: `docker/services.d/draft-library/run`
- Create: `tests/test_draft_library_server.py`
- Create: `tests/test_container_services.py`
- Modify: `Dockerfile`
- Modify: `.github/workflows/validate.yml`

**Step 1: Write failing server tests**

Start the server on loopback with an ephemeral test port and injected temporary store/clock; use real HTTP requests. Require:

- Constant public `GET /healthz` before and after key creation, with no setup/draft disclosure.
- `503` for every content request before a valid explicit key exists, while the temporary root remains byte-for-byte/write-set unchanged.
- Successful retrieval only for a valid signature, normalized route, existing safe rendition, future expiry, and TTL at or below 24 hours.
- Rejection of expired/far-future/non-integer/overflow expiry; short/long/non-hex/case-variant signatures; duplicate/unknown parameters; encoded slash/backslash/NUL/dot segments; Unicode IDs; path suffixes; query confusion; alternate methods; and signatures copied to another ID/method/expiry.
- Signature verification happens before existence-sensitive lookup, and invalid requests use identical generic responses for existing and absent draft IDs.
- Key creation or safe rotation after process launch works without restart; malformed/permissive/linked key state fails closed.
- Response body equals only the stored rendition, correct security headers are present on success and errors where applicable, server banners are suppressed, and no source/metadata/config route can be reached.
- Concurrent requests, disconnected clients, oversized request targets/headers, and malformed HTTP do not crash the process or produce traceback/path/content logs.

Add static/container assertions that:

- `docker/services.d/draft-library/run` is executable, uses `exec`, launches the installed server, and contains no `mkdir`, `touch`, `install`, key generation, newsroom init, `chmod`, recursive ownership change, or shell backgrounding.
- `Dockerfile` copies the service to `/etc/services.d/draft-library/run`, still has no `ENTRYPOINT`, retains `CMD ["gateway", "run"]`, and leaves `00-journalism-bootstrap` before upstream setup.
- CI compiles all three draft Python files, validates both shell scripts, runs all tests, and builds the image.

**Step 2: Verify RED**

Run:

```bash
python3 -m unittest tests.test_draft_library_server tests.test_container_services -v
```

Expected: failure because the server and service directory are absent.

**Step 3: Implement the server and service**

Subclass `ThreadingHTTPServer`/`BaseHTTPRequestHandler` with bounded, explicit routing and sanitized logging. Set `daemon_threads`, finite request handling limits/timeouts, and an explicit bind address/port read without writing state. Reuse signing/key/path validators from Task 2; do not duplicate a weaker validator. Read the key and rendition on each content request using safe file descriptors. Authenticate before opening the draft directory. Return static bytes only after all checks.

The s6 run script should contain only strict shell setup and an `exec python3 /opt/hermes/skills/draft-publishing/scripts/draft_library_server.py`. Copy it with `--chmod=0755`. Do not edit `railway.json` to add a `startCommand`, do not replace the base image entrypoint/CMD, and do not add setup work to `00-journalism-bootstrap`. Use the Railway-provided `PORT` for the HTTP listener only after validating it as an integer in range; document a safe local default if the image contract requires one.

**Step 4: Verify GREEN and container behavior**

Run:

```bash
python3 -m unittest tests.test_draft_library_server tests.test_container_services -v
python3 -m unittest discover -s tests -v
python3 -m py_compile skills/draft-publishing/scripts/draft_store.py skills/draft-publishing/scripts/draft_publish.py skills/draft-publishing/scripts/draft_library_server.py
bash -n docker/cont-init.d/00-journalism-bootstrap
bash -n docker/services.d/draft-library/run
docker build -t hermes-journalism-starter:draft-test .
```

Run two container smoke tests with a temporary volume:

1. Start without draft setup, verify the inherited gateway/s6 process tree remains alive, `/healthz` responds, content returns 503, and the mounted volume has no new `newsroom`, `drafts`, `draft-library`, or key path attributable to this service.
2. Run explicit CLI init/save in the mounted volume, request one signed link through the exposed port, verify exact safe HTML and headers, restart the same container/volume, and verify dynamic serving without startup rewrites.

**Checkpoint:** Inspect `docker image inspect` for inherited entrypoint/CMD and the running process tree for both gateway and Draft Library service. Resolve any ambiguity with the upstream s6 contract before proceeding.

---

### Task 4: Add authenticated Spacefast snapshot publishing and the skill workflow

**Objective:** Publish only generated static renditions through authenticated Spacefast REST, update an existing `space_id`, and encode approval and shared-Discord safeguards in the user-facing skill.

**Files:**
- Create: `skills/draft-publishing/scripts/spacefast_client.py`
- Create: `skills/draft-publishing/SKILL.md`
- Create: `skills/draft-publishing/references/publishing-contract.md`
- Create: `tests/test_spacefast_client.py`
- Modify: `tests/test_draft_publish.py`
- Modify: `tests/test_scaffold.py`
- Modify: `skills/newsroom-setup/SKILL.md`

**Step 1: Confirm and freeze the authenticated API contract**

Before production code, consult Spacefast's current official authenticated REST documentation. Record in `publishing-contract.md` the documentation URL and verification date, authenticated create/update methods and routes, required private/public visibility behavior, team/title/content fields, response ID/URL fields, size limits, and redirect semantics. If Spacefast does not offer a documented authenticated update API that can target an explicit team and existing space ID, stop and report the blocker rather than implementing anonymous or scraped automation.

Translate the verified contract into sanitized in-memory fixtures in `tests/test_spacefast_client.py`; fixtures must use fake hosts/tokens/team/space IDs and no copied credentials or real unpublished content.

**Step 2: Write failing client and orchestration tests**

Use a fake injected `urllib` opener/transport; tests must never contact Spacefast. Require:

- Missing/blank `SPACEFAST_TOKEN`, missing/blank/invalid explicit `SPACEFAST_TEAM_ID`, `not_configured`, non-Spacefast destination, `never`, and absent required editor approval all fail before transport invocation.
- Authorization comes only from the environment, is sent only to the verified HTTPS origin, and never appears in exceptions, JSON output, metadata, logs, or reprs.
- Redirects are disabled or restricted to the same verified origin according to the documented contract; HTTP downgrade, cross-origin redirect, malformed URL, oversized body/response, wrong content type, timeout, and invalid JSON/shape are sanitized failures.
- First publication calls authenticated create with explicit team ID and only title plus `rendition.html`/required visibility metadata. Assert byte-for-byte that source text, newsroom/sources JSON, publication metadata, local paths, HMAC data, and fake secret marker are absent.
- Successful create validates `space_id` and URL before atomically storing the allowlisted metadata.
- Second publication reads local `space_id`, calls authenticated update for that same ID, and never calls create. Response ID mismatch is rejected.
- Update failure, timeout, 4xx/5xx, malformed response, and process interruption preserve canonical `source.txt`, `rendition.html`, and prior `publication.json`; they never trigger create fallback.
- `destination=both` reports library and Spacefast outcomes independently; partial failure is explicit and retry does not duplicate the successful Spacefast space.
- The skill refuses anonymous Spacefast in shared Discord, asks for approval according to the matrix, never asks for tokens/team IDs in chat, runs local save first, and says local drafts are canonical.

**Step 3: Verify RED**

Run:

```bash
python3 -m unittest tests.test_spacefast_client tests.test_draft_publish tests.test_scaffold -v
```

Expected: new API/client/skill assertions fail.

**Step 4: Implement the minimum authenticated client and workflow**

Build a narrow client around an injected transport, verified constant API origin, explicit request builders, finite timeout, bounded reads, strict JSON decoding, and response allowlists. Define a redirect handler that fails closed unless official API documentation explicitly requires a same-origin redirect. Use a redacted exception type with stable public error codes. Keep tokens in short-lived request headers only.

Wire the client into `draft_publish.py` after local draft/config/policy validation. Read only the static rendition for the payload. Store only normalized `space_id`, returned public/edit URL if contractually safe, last-published rendition hash, and timestamp. Never replace canonical local files from API responses.

Write the skill in standard Hermes frontmatter format. Its flow is: generate/save locally, show destination and disclosure boundary, obtain required approval, initialize Draft Library only when explicitly requested, publish, verify each target, and report partial failures. For `auto_private`, automatically issue only a private Draft Library link; require approval before Spacefast. Tell operators to place `SPACEFAST_TOKEN` and `SPACEFAST_TEAM_ID` in Railway variables and never Discord. Link newsroom setup to the optional draft preference interview without changing the existing four initial editorial questions.

**Step 5: Verify GREEN**

Run:

```bash
python3 -m unittest tests.test_spacefast_client tests.test_draft_publish tests.test_scaffold -v
python3 -m unittest discover -s tests -v
python3 -m py_compile skills/draft-publishing/scripts/*.py
```

Inspect captured outbound requests and local metadata manually. Search both for source/config/key/token sentinel strings and require zero matches.

**Checkpoint:** Obtain separate API-contract and security reviews. Block on anonymous upload, create-on-update-failure, secret persistence/logging, raw-source upload, or any remote-to-local overwrite.

---

### Task 5: Document operations and perform final validation

**Objective:** Make deployment, setup, use, revocation, trust boundaries, and verification reproducible, then validate the complete change without committing it.

**Files:**
- Modify: `README.md`
- Modify: `SECURITY.md`
- Modify: `.env.example`
- Modify: `.github/workflows/validate.yml` if final command coverage is incomplete
- Modify: `tests/test_scaffold.py`
- Modify: `tests/test_container_services.py`
- Review: `Dockerfile`
- Review: `railway.json`
- Review: `docker/cont-init.d/00-journalism-bootstrap`
- Review: all files created or modified in Tasks 1-4

**Step 1: Write failing documentation assertions**

Require documentation to state:

- Canonical local drafts and exact persistent paths under `/opt/data/newsroom/drafts`.
- Explicit `draft_publish.py init` and the guarantee that startup does not create the HMAC key or draft state.
- Draft config fields/defaults and the complete policy matrix, especially `auto_private` not authorizing Spacefast.
- Draft Library Railway domain setup, base URL precedence, 24-hour maximum, link forwarding risk, revocation by key rotation/removal, and lack of user identity authentication after possession of a valid link.
- `/healthz` versus pre-setup content 503 behavior.
- `SPACEFAST_TOKEN` and `SPACEFAST_TEAM_ID` as Railway variables, authenticated REST only, no anonymous shared-Discord flow, external disclosure implications, and local canonical authority.
- Only static rendition upload, never raw source/config, while warning that the rendition itself may still contain sensitive text and remains unsuitable for source-protection work.
- Existing Discord, Railway, Baseten, pairing, persistent-volume, and sensitive-document warnings remain intact.
- Local validation, container smoke-test, key-mode check, and Spacefast fixture-test commands.

Update `.env.example` with blank/commented `DRAFT_LIBRARY_BASE_URL`, `RAILWAY_PUBLIC_DOMAIN` guidance, `SPACEFAST_TOKEN`, and `SPACEFAST_TEAM_ID`; include no sample secret and make clear which values are conditional. Do not make Spacefast variables globally required in `00-journalism-bootstrap`, because library-only deployments must start and missing Spacefast credentials must be enforced at publication time.

**Step 2: Verify RED**

Run the focused scaffold/container tests and confirm they fail for missing operational documentation.

**Step 3: Complete documentation and CI**

Add concise operator setup and usage examples to README, the new disclosure/link/API boundaries to SECURITY, and exact validation commands. Ensure CI runs discovery, compiles every Python script, validates both shell scripts, parses JSON examples/fixtures, and builds the image. Keep `railway.json` free of `startCommand`; preserve inherited entrypoint/CMD and existing restart policy.

**Step 4: Run final automated validation**

Run from the repository root:

```bash
python3 -m unittest discover -s tests -v
python3 -m py_compile \
  skills/newsroom-setup/scripts/newsroom_config.py \
  skills/draft-publishing/scripts/draft_store.py \
  skills/draft-publishing/scripts/draft_publish.py \
  skills/draft-publishing/scripts/draft_library_server.py \
  skills/draft-publishing/scripts/spacefast_client.py
bash -n docker/cont-init.d/00-journalism-bootstrap
bash -n docker/services.d/draft-library/run
python3 -m json.tool skills/newsroom-setup/templates/newsroom.example.json >/dev/null
python3 -m json.tool skills/newsroom-setup/templates/sources.example.json >/dev/null
docker build -t hermes-journalism-starter:final-validation .
```

Repeat the pre-setup no-write and post-setup signed-content container smoke tests from Task 3. Run Spacefast tests only against fake transport fixtures by default. If an operator separately approves a live sandbox test, use a non-production team/draft, verify create then update the same ID, remove the test space, and never place credentials in shell history, logs, fixtures, or this repository.

**Step 5: Perform adversarial and release review**

- Run traversal/link/special-file/race and stored/reflected XSS suites under repeated execution.
- Verify a signed URL at exactly the accepted TTL edge and refusal above 86,400 seconds using an injected clock, not sleep.
- Verify old newsroom JSON remains byte-identical after status/server startup.
- Verify startup with an empty mounted volume creates no draft/key/newsroom state attributable to the new service.
- Verify key rotation invalidates old signatures and requires no server restart.
- Verify Spacefast create then update calls preserve one ID and failed update never creates.
- Inspect successful outbound fixture captures to prove only static rendition/minimum metadata leaves the process.
- Inspect HTTP headers and generic error bodies with `curl`; ensure no Python/server version, filesystem path, draft content, or key/setup detail leaks.
- Inspect `docker image inspect` and process tree to prove inherited entrypoint, gateway command, and parallel s6 service.
- Run `git diff --check`, inspect `git diff --stat` and full `git diff`, and confirm only planned files changed.
- Scan added lines and fixtures for token/key/auth-header patterns, real domains/team IDs, private content, absolute developer paths, and generated artifacts such as `__pycache__`.
- Request independent specification-compliance and security reviews. Blocking findings include startup writes, key generation outside explicit init, noncanonical storage, weak path handling, XSS, >24-hour links, host-header URL construction, anonymous Spacefast use, missing approval, secret leakage, raw source/config upload, update-to-create fallback, or remote state becoming authoritative.

**Step 6: Finish without committing**

Fix each blocking finding with a failing regression test, repeat all relevant validation, and leave the working tree uncommitted for owner review. Report the passing test count, container smoke-test results, changed-file list, and any live Spacefast test deliberately not run. Do not commit or push as part of this plan execution.

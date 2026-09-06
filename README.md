# Hermes Journalism Starter

[![Deploy on Railway](https://railway.com/button.svg)](https://railway.com/deploy/hermes-agent-newsroom-and-journalism-sta)

This is a proof of concept for running [Hermes Agent](https://github.com/NousResearch/hermes-agent) as a private Discord research assistant on Railway. It uses a Baseten-hosted model and is designed to support newsroom-specific workflows.

It is not a hosted product. Each newsroom or journalist deploys and pays for their own Railway and Baseten accounts, owns their Discord bot, and controls their Hermes data volume.

## What is included

- The official `nousresearch/hermes-agent` container image
- Discord gateway mode
- Baseten's OpenAI-compatible inference endpoint
- Persistent Hermes configuration, sessions, memory, and skills under `/opt/data`
- The bundled `newsroom-setup` skill for conversational coverage and source configuration
- The bundled `government-records-research` skill for on-demand, cited research in official records
- The bundled `community-skill-sharing` skill for sanitizing and contributing custom newsroom skills upstream
- The [`unslop`](skills/unslop/SKILL.md) editing skill for removing common AI writing patterns
- Manual approval for dangerous commands
- Secret redaction in Hermes tool output
- Tool-loop hard stops for an unattended gateway

## Before deploying

Create the following:

1. A [Railway](https://railway.com/) account.
2. A [Baseten](https://www.baseten.co/) API key with access to the configured model.
3. A Discord application and bot. Open the included [Discord setup helper](setup/discord-bot-setup.html) in a browser to walk through the portal steps and generate the least-privilege invite link.
4. A private Discord channel for the bot.

In the Discord Developer Portal, enable **Message Content Intent**. Server Members Intent is not needed because this starter does not use a user-name allowlist. Invite the bot with these permissions:

- View Channels
- Send Messages
- Send Messages in Threads
- Embed Links
- Attach Files
- Read Message History
- Add Reactions

Do not grant Administrator.

Turn on Developer Mode in Discord, then copy the ID for the allowed channel. The setup helper explains where to find it.

## Railway deployment

Click the deploy button above or follow these steps:

1. Click **[![Deploy on Railway](https://railway.com/button.svg)](https://railway.com/deploy/hermes-agent-newsroom-and-journalism-sta)**.
2. Enter your `BASETEN_API_KEY`, `DISCORD_BOT_TOKEN`, and `DISCORD_ALLOWED_CHANNELS`.
3. Ensure a persistent volume is attached and mounted at `/opt/data` (to preserve newsroom configurations, sessions, and draft signing keys).
4. Deploy the service.
5. Confirm the bot appears online in Discord, then send it a message in your allowed channel. No `@mention` is required.

Railway does not override the image start command: the official Hermes image entrypoint runs the inherited `gateway run` command. The `00-journalism-bootstrap` init script only validates required variables and does not modify newsroom state or installed skill copies. The official `01-hermes-setup` script then safely seeds the starter configuration and SOUL onto a fresh volume and synchronizes bundled skills.

Required variables:

```text
BASETEN_API_KEY
DISCORD_BOT_TOKEN
DISCORD_ALLOWED_CHANNELS
```

Comma-separate multiple channel IDs. `DISCORD_ALLOWED_CHANNELS` limits the server channels where Hermes responds. The image sets `DISCORD_ALLOW_ALL_USERS=true`, `DISCORD_REQUIRE_MENTION=false`, and `DISCORD_AUTO_THREAD=false` so that any staff member who can access the allowed channel can communicate with the bot directly without requiring individual pairing approvals. Server channels not listed in `DISCORD_ALLOWED_CHANNELS` are strictly ignored.

Direct messages (DMs) to the bot should not be used for newsroom reporting. Keep communication in your designated private newsroom channels. Review `hermes pairing list` after restoring or reusing a persistent volume.

After the private deployment works, use Railway's template composer to generate a reusable template from the project. In the template, mark all three required variables as user-supplied and attach a volume at `/opt/data`. Railway generates the final one-click template URL.

## First newsroom session

Start with a message such as: "Set up coverage for our newsroom." The `newsroom-setup` skill checks the deployment, asks five editorial questions, including the newsroom's style guide and house rules, discovers and tests a candidate source pack, and asks an editor to approve it. The agent does not produce editorial content until an editor confirms the style. The intended first-session outcome is that the editor names the coverage, confirms the style, approves the discovered source pack, and receives one live, cited result from an approved source.

Setup is conversational and can be rerun. For example:

- "Set up coverage for our newsroom."
- "Add a place to our coverage."
- "Stop following a beat."
- "Show sources for this public body."
- "Test sources for this place."
- "Undo the last newsroom configuration change."

The editor does not need to supply APIs, selectors, feeds, or scheduling details. Source discovery is broad, but global support means discovery plus explicit reporting of checked sources, failures, and gaps. It is not guaranteed support for every government.

This release handles on-demand government records first. Monitoring is deferred and is never scheduled automatically. A future monitoring feature would require a separate, explicit editor decision.

### Persistent newsroom state

On Railway, `$HERMES_HOME` is `/opt/data`, so mutable newsroom state is under `/opt/data/newsroom`. The configuration CLI writes only after conversational setup or an explicit CLI command, not during bootstrap. Its files are:

- `$HERMES_HOME/newsroom/newsroom.json` for coverage and editorial preferences
- `$HERMES_HOME/newsroom/sources.json` for discovered and approved sources
- `$HERMES_HOME/newsroom/newsroom.json.previous` and `$HERMES_HOME/newsroom/sources.json.previous` for the previous versions used by undo
- `$HERMES_HOME/newsroom/audit/config-events.jsonl` for configuration events without configuration content

The required Railway persistent volume at `/opt/data` keeps these files across restarts and redeploys. Bundled skill upgrades do not overwrite newsroom choices. They also preserve locally modified or deliberately deleted skill copies under the synchronization rules below. Setup changes are validated, can be reviewed before apply, and can be undone.

## Draft publishing

The local path `/opt/data/newsroom/drafts` is the canonical draft store. Railway must have a persistent volume mounted at `/opt/data`; otherwise drafts, the signing key, and publication records disappear on redeploy. Local state remains canonical after any publication.

Draft Library setup is explicit. Run this once from the Railway service shell:

```bash
python3 /opt/data/skills/draft-publishing/scripts/draft_publish.py init
```

Startup, skill import, `status`, and the HTTP server do not create the key or draft state. `init` creates a 32-byte HMAC key at `/opt/data/newsroom/draft-library/hmac.key` with mode `600` and does not replace an existing key.

The optional `drafts` object in `newsroom.json` has these effective defaults for older configurations:

- `destination`: `library`, with allowed values `library`, `spacefast`, or `both`
- `publishing_policy`: `ask_each_time`
- `spacefast_setup_status`: `not_configured`, or `configured` after operator setup

The publication policy matrix is:

| Policy | Draft Library | Spacefast |
| --- | --- | --- |
| `ask_each_time` | Require editor approval for each link. | Require editor approval for each publish. |
| `auto_private` | May automate the private Draft Library only. | Never Spacefast. Each publish still requires editor approval. |
| `never` | Disabled. | Disabled. |

### Railway public domain for Draft Library

In Railway, enable public networking for the service and generate or attach an HTTPS domain. Base URL precedence is `DRAFT_LIBRARY_BASE_URL` first, then `RAILWAY_PUBLIC_DOMAIN`. Set `DRAFT_LIBRARY_BASE_URL` only for a validated custom HTTPS origin. Railway normally supplies `RAILWAY_PUBLIC_DOMAIN` after public networking is enabled.

Signed links can last no more than 86400 seconds. Anyone with a forwarded valid link can read the rendition until it expires. There is no per-user identity or authorization after possession of the link. Removing or rotating `/opt/data/newsroom/draft-library/hmac.key`, followed by explicit `init` when replacing it, revokes old links without a restart.

`/healthz` is a public liveness endpoint and returns 200 even before setup. Draft content returns 503 before explicit setup. Do not put private details in health checks or treat liveness as readiness for content.

### Optional Spacefast publication

To enable direct publication, set `SPACEFAST_TOKEN` and `SPACEFAST_TEAM_ID` as private Railway variables and mark `spacefast_setup_status` as `configured` through the reviewed newsroom configuration workflow. They are optional and are not bootstrap requirements. Spacefast publication uses authenticated REST only. Never ask for these values. Never post them in Discord. There is no anonymous shared Discord flow.

Each Spacefast publish requires explicit editor approval, including under `auto_private`. Publishing is an external disclosure to Spacefast, while local remains canonical. Only the generated static rendition is uploaded. The raw source, newsroom config, publication metadata, and HMAC key are never uploaded. A rendition can still contain sensitive text. It is not suitable for source-protection work or confidential identities.

## Community skill sharing

Newsrooms that build custom investigative scrapers, public data decoders, or FOIA trackers can sanitize, validate, and share them back to the starter repository.

Run:

```bash
python3 /opt/data/skills/community-skill-sharing/scripts/package_skill.py --skill custom-skill-name --author "@reporter"
```

The script verifies required frontmatter, ensures Python scripts compile cleanly, checks that templates parse as valid JSON, normalizes absolute paths to `$HERMES_HOME`, and scans for accidental secrets or tokens. It outputs a pre-filled one-click GitHub issue URL where the contributor can review and open the contribution with a single click without needing GitHub credentials inside the container.

## Updating

Redeploy after merging repository changes. Hermes seeds `config.yaml` and `SOUL.md` only when they are absent. On startup, Hermes also synchronizes bundled skills into the persistent `/opt/data/skills` directory. It installs new bundled skills on existing volumes, updates unchanged copies, and preserves locally modified or deliberately deleted copies. Redeploys do not overwrite newsroom choices in `/opt/data/newsroom`.

To adopt an updated starter config or SOUL on an existing volume, compare the repository version with the copy in `/opt/data` and merge it deliberately.

## Security boundary

Do not use this proof of concept for sensitive source documents, source identities, restricted personal data, credentials, or material that could put someone at risk. Discord, Railway, Hermes, Baseten, and the selected model are all part of the data path.

Use a newsroom-controlled system for sensitive investigations. See [SECURITY.md](SECURITY.md) for the specific limits of this deployment.

## Newsroom configuration CLI

The conversational setup skill normally drives this CLI. Operators can also use it directly. On a deployment, initialize missing default files and inspect the profile with:

```bash
export HERMES_HOME="${HERMES_HOME:-/opt/data}"
$HERMES_HOME/skills/newsroom-setup/scripts/newsroom_config.py init --home "$HERMES_HOME"
$HERMES_HOME/skills/newsroom-setup/scripts/newsroom_config.py status --home "$HERMES_HOME"
```

`init` creates the authoritative files only when they are absent. Do not edit the authoritative JSON under `$HERMES_HOME/newsroom` directly. Work on copies in a separate draft directory so validation happens before any persistent configuration changes:

```bash
DRAFT_DIR="$(mktemp -d)"
cp "$HERMES_HOME/newsroom/newsroom.json" "$DRAFT_DIR/newsroom.json"
cp "$HERMES_HOME/newsroom/sources.json" "$DRAFT_DIR/sources.json"
# Edit only $DRAFT_DIR/newsroom.json and $DRAFT_DIR/sources.json.

$HERMES_HOME/skills/newsroom-setup/scripts/newsroom_config.py validate --kind sources --input "$DRAFT_DIR/sources.json"
$HERMES_HOME/skills/newsroom-setup/scripts/newsroom_config.py validate --kind newsroom --input "$DRAFT_DIR/newsroom.json"
```

After editorial review, apply sources first and mark newsroom complete last. `apply` operates one document at a time:

```bash
$HERMES_HOME/skills/newsroom-setup/scripts/newsroom_config.py apply --home "$HERMES_HOME" --kind sources --input "$DRAFT_DIR/sources.json"
$HERMES_HOME/skills/newsroom-setup/scripts/newsroom_config.py apply --home "$HERMES_HOME" --kind newsroom --input "$DRAFT_DIR/newsroom.json"
$HERMES_HOME/skills/newsroom-setup/scripts/newsroom_config.py status --home "$HERMES_HOME"
```

That ordering prevents a profile from claiming complete setup before it has an active, validated official source. When removing the last such source, first apply a newsroom draft with `setup_status` set to `partial`, then apply the sources draft. The CLI rejects an ordering that would leave an invalid complete profile.

`undo` also operates one document at a time and restores that document's single previous version as a new revision. Inspect status, then undo changes in reverse order where the profile invariant permits it:

```bash
$HERMES_HOME/skills/newsroom-setup/scripts/newsroom_config.py undo --home "$HERMES_HOME" --kind newsroom
$HERMES_HOME/skills/newsroom-setup/scripts/newsroom_config.py undo --home "$HERMES_HOME" --kind sources
$HERMES_HOME/skills/newsroom-setup/scripts/newsroom_config.py status --home "$HERMES_HOME"
```

An apply or undo can replace the one available previous version for that document. Keep reviewed drafts outside the authoritative directory until the workflow is complete, and remove the temporary directory when it is no longer needed.

## Local validation

Run these commands from the repository root. They use fake Spacefast fixtures and make no live Spacefast request by default.

```bash
python3 -m unittest discover -s tests -v
python3 -m py_compile skills/newsroom-setup/scripts/newsroom_config.py skills/draft-publishing/scripts/draft_store.py skills/draft-publishing/scripts/spacefast_client.py skills/draft-publishing/scripts/draft_publish.py skills/draft-publishing/scripts/draft_library_server.py skills/community-skill-sharing/scripts/package_skill.py
bash -n docker/cont-init.d/00-journalism-bootstrap
bash -n docker/services.d/draft-library/run
python3 -m json.tool railway.json >/dev/null
python3 -m json.tool skills/newsroom-setup/templates/newsroom.example.json >/dev/null
python3 -m json.tool skills/newsroom-setup/templates/sources.example.json >/dev/null
python3 -m unittest tests.test_spacefast_client -v
docker build -t hermes-journalism-starter .
```

The focused fake Spacefast command tests the authenticated client against local fixtures. There is no live Spacefast test in the default suite.

For a container smoke test, use a disposable host directory. This starts only the bundled Draft Library server so it does not require real Discord or Baseten credentials:

```bash
SMOKE_DATA="$(mktemp -d)"
docker run --rm -d --name journalism-draft-smoke -p 8080:8080 -v "$SMOKE_DATA:/opt/data" --entrypoint python3 hermes-journalism-starter /opt/hermes/skills/draft-publishing/scripts/draft_library_server.py
curl -i http://127.0.0.1:8080/healthz
curl -i 'http://127.0.0.1:8080/draft/smoke?expires=1&sig=0000000000000000000000000000000000000000000000000000000000000000'
# The health request must return 200. Content must return 503 before explicit setup.
docker exec journalism-draft-smoke python3 /opt/hermes/skills/draft-publishing/scripts/draft_publish.py init
docker exec journalism-draft-smoke stat -c '%a' /opt/data/newsroom/draft-library/hmac.key
# The HMAC key mode must be 600.
docker stop journalism-draft-smoke
rm -rf "$SMOKE_DATA"
```

## License

The starter files in this repository are available under the MIT License. Hermes Agent remains subject to its own upstream license.

The bundled `unslop` skill comes from the [Cursor plugins repository](https://github.com/cursor/plugins/tree/main/pstack/skills/unslop) and is distributed under its original MIT license. Its attribution and source record are stored with the skill.

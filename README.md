# Hermes Journalism Starter

This is a proof of concept for running [Hermes Agent](https://github.com/NousResearch/hermes-agent) as a private Discord research assistant on Railway. It uses a Baseten-hosted model and is designed to support newsroom-specific workflows.

It is not a hosted product. Each newsroom or journalist deploys and pays for their own Railway and Baseten accounts, owns their Discord bot, and controls their Hermes data volume.

## What is included

- The official `nousresearch/hermes-agent` container image
- Discord gateway mode
- Baseten's OpenAI-compatible inference endpoint
- Persistent Hermes configuration, sessions, memory, and skills under `/opt/data`
- The bundled `newsroom-setup` skill for conversational coverage and source configuration
- The bundled `government-records-research` skill for on-demand, cited research in official records
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

This repository is ready to become a Railway template. During private testing:

1. Create a Railway project from this GitHub repository.
2. Add a persistent volume mounted at `/opt/data`. This is required. Redeployments lose Hermes sessions, memory, and configuration without it.
3. Add the variables below.
4. Deploy the service.
5. Confirm the bot appears online, then send it a message in the allowed Discord channel. No `@mention` is required.

Railway does not override the image start command: the official Hermes image entrypoint runs the inherited `gateway run` command. The `00-journalism-bootstrap` init script only validates required variables and does not modify newsroom state or installed skill copies. The official `01-hermes-setup` script then safely seeds the starter configuration and SOUL onto a fresh volume and synchronizes bundled skills.

Required variables:

```text
BASETEN_API_KEY
DISCORD_BOT_TOKEN
DISCORD_ALLOWED_CHANNELS
```

Comma-separate multiple channel IDs. `DISCORD_ALLOWED_CHANNELS` limits the server channels where Hermes responds. Any human member who can access one of those channels can communicate with the bot. The image defaults to `DISCORD_REQUIRE_MENTION=false` and `DISCORD_AUTO_THREAD=false`, so replies stay in the channel and do not require `@mention`.

By default, Discord direct messages are denied because this starter does not configure a user or role allowlist. The channel allowlist authorizes guild messages only when the message comes from an allowed channel. Do not set `DISCORD_ALLOW_ALL_USERS`, `GATEWAY_ALLOW_ALL_USERS`, `DISCORD_ALLOWED_USERS`, or `DISCORD_ALLOWED_ROLES` unless you deliberately want to change that behavior. User and role authorization can permit DMs. An operator-approved Discord pairing is also an authorization grant and can permit that paired user to use DMs. Review `hermes pairing list` after restoring or reusing a persistent volume.

After the private deployment works, use Railway's template composer to generate a reusable template from the project. In the template, mark all three required variables as user-supplied and attach a volume at `/opt/data`. Railway generates the final one-click template URL.

## First newsroom session

Start with a message such as: "Set up coverage for our newsroom." The `newsroom-setup` skill checks the deployment, asks four editorial questions, discovers and tests a candidate source pack, and asks an editor to approve it. The intended first-session outcome is that the editor names the coverage, approves the discovered source pack, and receives one live, cited result from an approved source.

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

For a quick test from a repository checkout, initialize only a disposable directory and read it back:

```bash
export HERMES_HOME="$(mktemp -d)"
python3 skills/newsroom-setup/scripts/newsroom_config.py init --home "$HERMES_HOME"
python3 skills/newsroom-setup/scripts/newsroom_config.py status --home "$HERMES_HOME"
python3 -m py_compile skills/newsroom-setup/scripts/newsroom_config.py
python3 -m unittest discover -s tests -v
python3 -m json.tool skills/newsroom-setup/templates/newsroom.example.json >/dev/null
python3 -m json.tool skills/newsroom-setup/templates/sources.example.json >/dev/null
docker build -t hermes-journalism-starter .
```

## License

The starter files in this repository are available under the MIT License. Hermes Agent remains subject to its own upstream license.

The bundled `unslop` skill comes from the [Cursor plugins repository](https://github.com/cursor/plugins/tree/main/pstack/skills/unslop) and is distributed under its original MIT license. Its attribution and source record are stored with the skill.

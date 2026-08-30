# Hermes Journalism Starter

This is a proof of concept for running [Hermes Agent](https://github.com/NousResearch/hermes-agent) as a private Discord research assistant on Railway. It uses a Baseten-hosted model and is designed to support newsroom-specific workflows.

It is not a hosted product. Each newsroom or journalist deploys and pays for their own Railway and Baseten accounts, owns their Discord bot, and controls their Hermes data volume.

## What is included

- The official `nousresearch/hermes-agent` container image
- Discord gateway mode
- Baseten's OpenAI-compatible inference endpoint
- Persistent Hermes configuration, sessions, memory, and skills under `/opt/data`
- No custom journalism skills yet; targeted workflows will be added as they are defined
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

Railway does not override the image start command: the official Hermes image entrypoint runs the inherited `gateway run` command. The `00-journalism-bootstrap` init script only validates required variables. The official `01-hermes-setup` script then safely seeds the starter configuration and SOUL onto a fresh volume.

Required variables:

```text
BASETEN_API_KEY
DISCORD_BOT_TOKEN
DISCORD_ALLOWED_CHANNELS
```

Comma-separate multiple channel IDs. `DISCORD_ALLOWED_CHANNELS` limits the server channels where Hermes responds. Any human member who can access one of those channels can communicate with the bot. The image defaults to `DISCORD_REQUIRE_MENTION=false` and `DISCORD_AUTO_THREAD=false`, so replies stay in the channel and do not require `@mention`.

By default, Discord direct messages are denied because this starter does not configure a user or role allowlist. The channel allowlist authorizes guild messages only when the message comes from an allowed channel. Do not set `DISCORD_ALLOW_ALL_USERS`, `GATEWAY_ALLOW_ALL_USERS`, `DISCORD_ALLOWED_USERS`, or `DISCORD_ALLOWED_ROLES` unless you deliberately want to change that behavior. User and role authorization can permit DMs. An operator-approved Discord pairing is also an authorization grant and can permit that paired user to use DMs. Review `hermes pairing list` after restoring or reusing a persistent volume.

After the private deployment works, use Railway's template composer to generate a reusable template from the project. In the template, mark all three required variables as user-supplied and attach a volume at `/opt/data`. Railway generates the final one-click template URL.

## Updating

Redeploy after merging repository changes. Hermes seeds `config.yaml` and `SOUL.md` only when they are absent.

To adopt an updated starter config or SOUL on an existing volume, compare the repository version with the copy in `/opt/data` and merge it deliberately.

## Security boundary

Do not use this proof of concept for sensitive source documents, source identities, restricted personal data, credentials, or material that could put someone at risk. Discord, Railway, Hermes, Baseten, and the selected model are all part of the data path.

Use a newsroom-controlled system for sensitive investigations. See [SECURITY.md](SECURITY.md) for the specific limits of this deployment.

## Local validation

```bash
python3 -m unittest discover -s tests -v
docker build -t hermes-journalism-starter .
```

## License

The starter files in this repository are available under the MIT License. Hermes Agent remains subject to its own upstream license.

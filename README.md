# Hermes Journalism Starter

This is a proof of concept for running [Hermes Agent](https://github.com/NousResearch/hermes-agent) as a private Discord research assistant on Railway. It uses a Baseten-hosted model and includes a small set of journalism workflows.

It is not a hosted product. Each newsroom or journalist deploys and pays for their own Railway and Baseten accounts, owns their Discord bot, and controls their Hermes data volume.

## What is included

- The official `nousresearch/hermes-agent` container image
- Discord gateway mode
- Baseten's OpenAI-compatible inference endpoint
- Persistent Hermes configuration, sessions, memory, and skills under `/opt/data`
- Source verification, transcript analysis, and public-records research skills
- Manual approval for dangerous commands
- Secret redaction in Hermes tool output
- Tool-loop hard stops for an unattended gateway

## Before deploying

Create the following:

1. A [Railway](https://railway.com/) account.
2. A [Baseten](https://www.baseten.co/) API key with access to the configured model.
3. A Discord application and bot in the [Discord Developer Portal](https://discord.com/developers/applications).
4. A private Discord channel for the bot.

In the Discord Developer Portal, enable **Message Content Intent** and **Server Members Intent**. Invite the bot with these permissions:

- View Channels
- Send Messages
- Send Messages in Threads
- Embed Links
- Attach Files
- Read Message History
- Add Reactions

Do not grant Administrator.

Turn on Developer Mode in Discord, then copy the IDs for the allowed channel and users.

## Railway deployment

This repository is ready to become a Railway template. During private testing:

1. Create a Railway project from this GitHub repository.
2. Add a persistent volume mounted at `/opt/data`. This is required. Redeployments lose Hermes sessions, memory, and configuration without it.
3. Add the variables below.
4. Deploy the service.
5. Confirm the bot appears online, then mention it in the allowed Discord channel.

Railway does not override the image start command: the official Hermes image entrypoint runs the inherited `gateway run` command. The `00-journalism-bootstrap` init script only validates required variables. The official `01-hermes-setup` script then safely seeds the starter configuration and SOUL and synchronizes bundled skills onto a fresh volume.

Required variables:

```text
BASETEN_API_KEY
DISCORD_BOT_TOKEN
DISCORD_ALLOWED_CHANNELS
DISCORD_ALLOWED_USERS
```

Comma-separate multiple Discord IDs. Keep both allowlists narrow. `DISCORD_ALLOWED_CHANNELS` limits server channels, while `DISCORD_ALLOWED_USERS` controls which people may use the bot.

Discord direct messages remain enabled for allowed users, and `DISCORD_ALLOWED_CHANNELS` does not restrict DMs. This proof of concept does not provide a strict channel-only mode. Do not add `DISCORD_ALLOWED_ROLES` unless you deliberately want members of those roles to be authorized, including through DMs when Hermes can verify the role in a mutual server.

After the private deployment works, use Railway's template composer to generate a reusable template from the project. In the template, mark all four required variables as user-supplied and attach a volume at `/opt/data`. Railway generates the final one-click template URL.

## Updating

Redeploy after merging repository changes. Hermes seeds `config.yaml` and `SOUL.md` only when they are absent. Its bundled-skill manifest can update an unchanged starter skill while preserving a locally modified or deliberately deleted copy.

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

The starter files and journalism skills in this repository are available under the MIT License. Hermes Agent remains subject to its own upstream license.

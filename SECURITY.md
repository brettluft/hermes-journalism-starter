# Security notes

This repository is a proof of concept for routine collaboration. It is not a secure document drop, source-protection system, or compliance product.

## Do not submit

Do not send sensitive source documents or identities through this Discord bot. Do not submit passwords, API keys, government identification numbers, health information, financial account credentials, tax records, payment-card data, biometric data, information about children, or legally privileged material.

Discord receives the original message and attachments. Railway runs the Hermes container and persistent volume. Hermes stores sessions and may cache attachments. Baseten and the selected model process prompts and extracted document contents. Each provider may retain operational metadata even when model inputs and outputs are not retained.

## Required controls

- Restrict the bot with `DISCORD_ALLOWED_CHANNELS`.
- Restrict membership and visibility of every allowed channel. The container sets `DISCORD_ALLOW_ALL_USERS=true` so all team members in allowed channels can converse with the bot without individual pairing codes.
- Do not use Discord direct messages (DMs) for sensitive newsroom work.
- Review `hermes pairing list` when restoring or reusing `/opt/data`. An approved Discord pairing is a separate authorization grant and can permit that user to use DMs.
- Use a private Discord channel.
- Give the bot only the permissions documented in README.md.
- Never grant the bot Administrator.
- Store tokens only as Railway variables.
- Mount the persistent volume only at `/opt/data`.
- Review Railway members and deployment logs.
- Rotate a token immediately if it appears in chat, logs, a commit, or an issue.
- The starter responds without `@mention` in allowed channels. Do not allow it into general-purpose or public channels.
- The starter disables automatic Discord threads, so everyone in an allowed channel can see the shared conversation.

The starter enables Hermes secret redaction and manual command approval. These controls reduce accidental exposure but do not make Discord suitable for sensitive documents.

## Draft publication boundary

Draft Library signed links are bearer links. Anyone who receives a forwarded valid link can read its rendition until expiry or signing-key rotation. Link possession has no per-user identity check. The public `/healthz` endpoint contains liveness only.

Spacefast is a separate external disclosure. Use only authenticated REST with `SPACEFAST_TOKEN` and `SPACEFAST_TEAM_ID` stored as Railway variables. Never ask for or post either value in Discord. There is no anonymous shared-Discord Spacefast flow, and each Spacefast publish requires editor approval.

Only the generated static rendition is uploaded to Spacefast, never the raw source, newsroom config, publication metadata, or HMAC key. The local draft remains canonical. A static rendition can still contain sensitive text. Draft Library and Spacefast are not suitable for source-protection work, sensitive documents, or confidential identities.

## Baseten review

Before wider use, the operator should review Baseten's current Terms, DPA, privacy policy, security documentation, subprocessors, model retention, and training-use terms. Obtain written answers when organizational policy requires them. Do not infer that a zero-data-retention claim covers account metadata, billing records, abuse-prevention logs, application logs, or every inference product.

## Reporting a problem

Do not open a public issue containing credentials, private documents, source identities, or exploit details. Contact the repository owner privately. Rotate any exposed credential before sending the report.

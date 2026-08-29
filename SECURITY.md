# Security notes

This repository is a proof of concept for routine collaboration. It is not a secure document drop, source-protection system, or compliance product.

## Do not submit

Do not send sensitive source documents or identities through this Discord bot. Do not submit passwords, API keys, government identification numbers, health information, financial account credentials, tax records, payment-card data, biometric data, information about children, or legally privileged material.

Discord receives the original message and attachments. Railway runs the Hermes container and persistent volume. Hermes stores sessions and may cache attachments. Baseten and the selected model process prompts and extracted document contents. Each provider may retain operational metadata even when model inputs and outputs are not retained.

## Required controls

- Restrict the bot with `DISCORD_ALLOWED_CHANNELS`.
- Require an explicit `DISCORD_ALLOWED_USERS` list.
- Understand that authorized users can still use Discord DMs; the channel allowlist applies only to server channels.
- Do not configure `DISCORD_ALLOWED_ROLES` unless role-based DM authorization is acceptable.
- Use a private Discord channel.
- Give the bot only the permissions documented in README.md.
- Never grant the bot Administrator.
- Store tokens only as Railway variables.
- Mount the persistent volume only at `/opt/data`.
- Review Railway members and deployment logs.
- Rotate a token immediately if it appears in chat, logs, a commit, or an issue.
- Keep Discord mentions required in server channels.

The starter enables Hermes secret redaction and manual command approval. These controls reduce accidental exposure but do not make Discord suitable for sensitive documents.

## Baseten review

Before wider use, the operator should review Baseten's current Terms, DPA, privacy policy, security documentation, subprocessors, model retention, and training-use terms. Obtain written answers when organizational policy requires them. Do not infer that a zero-data-retention claim covers account metadata, billing records, abuse-prevention logs, application logs, or every inference product.

## Reporting a problem

Do not open a public issue containing credentials, private documents, source identities, or exploit details. Contact the repository owner privately. Rotate any exposed credential before sending the report.

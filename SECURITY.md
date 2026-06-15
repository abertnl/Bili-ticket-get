# Security Policy

## Sensitive Data

This project stores runtime credentials in the local `config.json` file. The
file is ignored by Git and should never be committed, shared, uploaded in issue
attachments, or included in release archives.

Treat these values as secrets:

- Bilibili cookies such as `SESSDATA`, `bili_jct`, and `DedeUserID`
- `rrocr_token`
- Bark URL keys
- ServerChan keys
- `server.admin_token` or `TICKET_BUY_ADMIN_TOKEN`

If any credential is exposed, log out of the affected account or rotate the
token immediately.

## Reporting a Vulnerability

Please avoid posting exploitable security details in public issues. Prefer
GitHub Security Advisories if they are enabled for this repository. If not,
open a minimal public issue that says a private security report is needed,
without including secrets, payloads, or reproduction details.

## Local Deployment

The web server is intended for local use. If you expose it through a proxy,
tunnel, or public network, set a strong `server.admin_token` and configure
`server.allowed_origins` explicitly.

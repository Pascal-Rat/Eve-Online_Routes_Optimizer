# Security policy

## Supported version

Security fixes target the latest release on the default branch.

## Reporting a vulnerability

Please do not publish credentials, tokens, private route data, or a working exploit in a public
issue. If GitHub private vulnerability reporting is enabled for the repository, use **Security >
Report a vulnerability**. Otherwise, open a minimal issue that says a private security report is
needed without including sensitive details.

For ordinary bugs that do not expose private information or create a security boundary failure, use
the normal bug-report template.

## Security boundary

The web interface is designed for localhost use and binds to `127.0.0.1`. It is not hardened as a
public internet service. Do not reverse-proxy or expose it to an untrusted network without adding
authentication, transport security, request-hardening, and a dedicated deployment threat model.

The public courier workflow does not require EVE OAuth credentials. Do not commit ESI client
secrets, refresh tokens, access tokens, personal execution state, or private contract data.

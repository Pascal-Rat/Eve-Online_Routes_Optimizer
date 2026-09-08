# Security policy

## Supported version

Security fixes target the latest release on the default branch.

## Reporting a vulnerability

Please do not publish credentials, tokens, private route data, or a working exploit in a public
issue. If GitHub private vulnerability reporting is enabled for the repository, use **Security >
Report a vulnerability**. Otherwise, open a minimal issue that says a private security report is
needed without including sensitive details.

For ordinary bugs that do not expose private information or bypass a security restriction, use
the [bug report form](https://github.com/Pascal-Rat/Eve-Online_Routes_Optimizer/issues/new?template=bug_report.yml).

## Security boundary

The web interface runs on your own computer and listens at `127.0.0.1`, the local loopback address.
It is designed for a local user and is not hardened as a public internet service. Exposing it
through a proxy or to an untrusted network requires additional authentication, encrypted transport,
request protections and a security review of that deployment.

The public courier workflow does not require EVE OAuth credentials. Do not commit ESI client
secrets, refresh tokens, access tokens, personal execution state, or private contract data.

Local plans and execution files can reveal your intended route and accepted jobs even though the
contract scan uses public data. Check exports, logs and screenshots before attaching them to an
issue. The [storage reference](docs/INTERFACES.md#where-files-are-stored) explains where those files
live. Workspace revisions, proposal identities and process locks prevent stale acceptance and
competing application writes. Bounded connections, read deadlines and response/decompression limits
reduce accidental resource exhaustion. These controls do not authenticate other local processes or
protect against a user who can edit the workspace directly. See the
[model and recovery limits](docs/DOMAIN.md#current-beta-limitations).

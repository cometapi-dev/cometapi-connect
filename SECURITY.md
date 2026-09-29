# Security policy

## Report a vulnerability

Email [support@cometapi.com](mailto:support@cometapi.com) with the subject **CometAPI Connect security report**. Include the affected version or commit, operating system, a minimal reproduction, and the expected impact. Share only redacted diagnostics; do not send API keys, credential-store exports, or private application backups.

Please report vulnerabilities privately before opening a public issue or pull request. CometAPI maintainers will review reports and coordinate a fix and disclosure where appropriate. The project is in active development, and security fixes target the latest development version.

## Local security model

CometAPI Connect runs a browser interface on loopback. Its session authentication, host/origin checks, and request headers protect configuration operations. Keep these protections enabled and do not expose the local server through a public proxy or tunnel.

Connecting an application writes credentials into that application's configuration or supported native credential store. Some applications use plaintext configuration files. Local backups can contain previous credentials and require the same protection as the original files. Restoration checks for subsequent changes before overwriting configuration.

Credential writes to Git-tracked files are refused, and configuration inside a Git project requires Git to be available for verification. If the home directory is Git-managed, the backup-directory ignore rule persists after restoration because the backups can still contain credentials. A Git-tracked LiteLLM YAML file requires a separate private configuration; Connect does not assume that an environment file will be loaded automatically.

The helper runs with the current user's permissions. It does not provide isolation from malicious software running as that same user. Close selected applications before applying or restoring configuration, and review the proposed changes and adapter requirements.

## Development and distribution

Automated CI uses dummy credentials and isolated profiles. Live acceptance work must define its targets and spending budget separately. Keep signing credentials in the repository's secret store; do not place them in source files or downloaded artifacts.

Development downloads may be unsigned or unnotarized. Check their build metadata. Production Windows and macOS artifacts require the signing and verification steps in the [release packaging guide](distribution/README.md).

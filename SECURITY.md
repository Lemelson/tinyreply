# Security

Please do not open a public issue for a vulnerability. Report it privately through
[GitHub security advisories](https://github.com/Lemelson/tinyreply/security/advisories/new).

Most relevant here: anything that could send a second DM to the same person, leak an access token, or let
someone other than the operator trigger sends. tinyreply opens no network port, reads secrets only from
`STATE_DIR/.env`, and runs under a dedicated user in the provided systemd unit.

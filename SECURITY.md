# Security policy

## Reporting a vulnerability

Please report security problems privately, not in a public issue. On
GitHub, open the repository's **Security** tab and choose **Report a
vulnerability**. Include what you found, how to reproduce it, and which
version or commit you tested.

You will get an answer as soon as possible. Once a fix is released, the
report can be credited in the changelog if you like.

## Supported versions

Only the latest release gets security fixes.

## Scope

MediaIndex is built for private use by one to five people who trust its
admin; see [Who it's built for](docs/security.md#who-its-built-for).
Anything that lets someone without an account in, or lets one account see
another's private media, is in scope. Problems that only matter with open
registration or many users, such as one account filling the disk, are
known limits of that design rather than vulnerabilities.

MediaIndex is meant to run behind a reverse proxy with HTTPS, set up as
[Installation](docs/installation.md) describes. Problems that only occur
with a different setup, for example with the app published without a
proxy or with `DEV_MODE=true`, are still welcome, but may be answered with
a documentation change.

[Security model](docs/security.md) describes how the app is protected, and
its known limitations.

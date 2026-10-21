# Contributing

Thanks for helping with MediaIndex. Bug reports, fixes and improvements to
the docs are all welcome.

## Scope

MediaIndex is for private use by one to five people (see [Who it's
for](README.md#who-its-for)). Changes that only make sense for a public or
large setup, such as open sign-up, storage quotas, moderation tools or
scaling to many users, are out of scope.

## Before you start

- For a bug, open an issue with what you did, what you expected, what
  happened, and the relevant lines from the logs. Leave out passwords,
  tokens and your domain.
- For a larger change, open an issue first, so we can agree on the
  approach before you spend time on it.
- Security problems go through [SECURITY.md](SECURITY.md), not an issue.

## Setup and tests

[Development](docs/development.md) explains how to run the app locally,
the project layout and the design rules. In short:

```bash
python3.11 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
make test    # needs Docker for the throwaway PostgreSQL
make lint
```

## Pull requests

- Keep each pull request to one change, and include tests for it. The
  suite must pass, and `make lint` must be clean.
- Follow the existing code: no SQL in routes, no inline JavaScript, tags
  through `app/media/tagging.py`. See the design rules in
  [Development](docs/development.md#design-rules).
- Update the docs in `docs/` when you change behaviour or add a setting.
  New settings go in [Configuration](docs/configuration.md), and
  `.env.example` and `docker-compose.yml.example` too.
- Write commit messages as a short summary line, a blank line, and a body
  that explains why the change is needed.
- Describe user-visible changes in the pull request, so they can be
  added to [CHANGELOG.md](CHANGELOG.md) for the next release.

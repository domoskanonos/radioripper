# Security Policy

## Reporting a Vulnerability

If you discover a security vulnerability in Radio-Ripper, please report it privately.

**Do not** open a public issue. Instead, email the maintainers at the address listed in the project's commit history, or open a [GitHub Security Advisory](https://github.com/domoskanonos/radioripper/security/advisories/new).

We will acknowledge receipt within 48 hours and provide an estimated timeline for a fix.

## Supported Versions

| Version | Supported          |
|---------|--------------------|
| 2.0.x   | :white_check_mark: |
| < 2.0   | :x:                |

## Security Best Practices

- Run the Docker container as a non-root user (default).
- Never commit `.env` files or API keys to the repository.
- Use environment variables or a secrets manager for sensitive configuration.
- Keep dependencies updated via `uv sync` and monitor for CVEs.

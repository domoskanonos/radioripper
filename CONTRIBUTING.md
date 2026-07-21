# Contributing to Radio-Ripper

Thank you for considering contributing! We welcome contributions of all kinds: bug reports, feature requests, documentation, and code.

## Code of Conduct

This project adheres to the [Contributor Covenant Code of Conduct](CODE_OF_CONDUCT.md). By participating, you agree to uphold it.

## How to Contribute

### Reporting Bugs

1. Check the [issue tracker](https://github.com/domoskanonos/radioripper/issues) for existing reports.
2. If none exists, [open a new issue](https://github.com/domoskanonos/radioripper/issues/new/choose) using the bug report template.
3. Include:
   - Python version and OS
   - Steps to reproduce
   - Expected vs actual behavior
   - Relevant logs or error output

### Feature Requests

Open an issue using the feature request template. Describe the problem you're solving, not just the solution.

### Pull Requests

1. Fork the repository.
2. Create a feature branch: `git checkout -b feat/my-feature`.
3. Install development dependencies:
   ```bash
   uv sync --extra dev
   ```
4. Run checks before committing:
   ```bash
   pre-commit run --all-files
   uv run pytest --cov -q
   uv run mypy src/radio_ripper/
   ```
5. Write or update tests for your changes. Coverage must not decrease below 80%.
6. Update documentation (README, arc42) if your change affects architecture or usage.
7. Open a PR against the `main` branch.

## Development Setup

```bash
git clone https://github.com/domoskanonos/radioripper.git
cd radioripper
uv sync --extra dev
pre-commit install
```

## Code Style

- **Line length**: 100 characters
- **Formatting**: Ruff (compatible with Black)
- **Type annotations**: Required for all function signatures
- **Imports**: Sorted via ruff (isort-compatible)
- **Naming**: `snake_case` for functions/variables, `PascalCase` for classes, `UPPER_CASE` for constants

## Testing

- All new code must include tests.
- Tests use `pytest` with `pytest-asyncio` for async code.
- HTTP mocking via `respx`.
- Run tests: `uv run pytest --cov -v`
- Coverage gate: 80%.

## Project Structure

```
src/radio_ripper/    # Main package
tests/               # Mirrors src layout
docs/                # arc42 architecture docs + diagrams
```

## Questions?

Open a [discussion](https://github.com/domoskanonos/radioripper/discussions) or issue.

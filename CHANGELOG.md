# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [2.1.0] - 2026-07-21

### Added
- arc42 architecture documentation (12 sections + 4 ADRs)
- GitHub Pages deployment via mkdocs-material
- SECURITY.md, CODE_OF_CONDUCT.md, CONTRIBUTING.md
- GitHub issue templates (bug report, feature request)
- Pull request template with checklist
- .editorconfig for consistent formatting
- Docker Hub push in CI pipeline
- Env example with placeholders (acoustid API key)

### Changed
- Bump Python matrix to 3.11–3.13 in CI
- Replace mutable default args with ClassVar
- Consolidate SIM117 combined-with statements
- Upgrade pre-commit hooks to latest tags
- Ruff format alignment in storage + fingerprint modules
- Move .env → .env.example (remove committed secret)

### Fixed
- FakeRepo mocks for abstract methods (`find_all_by_recording_id`, etc.)
- mypy strictness: 14 type errors resolved (dict args, PIL, HttpUrl)
- Docker build: COPY LICENSE + README.md into builder stage
- Dockerignore: preserve LICENSE in build context

### Security
- Remove API key from .env (committed secret cleanup)

## [2.0.0] - 2026-07-21

### Added
- Full async/await implementation with asyncio event loop
- Multi-stream parallel recording with per-station tasks
- ICY metadata state machine parser with StreamTitle extraction
- SQLite-based duplicate detection (WAL mode)
- ID3v2 tagging with cover art embedding
- iTunes Search API enrichment for album art
- Playlist resolution (.m3u/.pls formats)
- Exponential backoff reconnect strategy
- Graceful shutdown via SIGINT/SIGTERM
- Gradio web GUI (optional extra)
- REST API layer (ConfigApi, StationApi, LibraryApi, RipperApi)
- Comprehensive test suite (pytest + pytest-asyncio + respx)
- Docker multi-stage build
- CI pipeline with lint, type-check, test matrix (3.11, 3.12)
- Pre-commit hooks for code quality

### Security
- Non-root user in Docker container
- Input validation via Pydantic v2
- Graceful error handling throughout

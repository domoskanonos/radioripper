# Changelog

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
- CI pipeline with lint, type-check, test matrix
- Pre-commit hooks for code quality

### Security
- Non-root user in Docker container
- Input validation via Pydantic v2
- Graceful error handling throughout

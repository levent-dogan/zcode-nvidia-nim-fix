# Changelog

All notable changes to this project are documented here. The project follows Semantic Versioning.

## 0.1.3 - 2026-07-13

### Added

- Add debug-safe NVIDIA API key fingerprints to upstream diagnostic logs.
- Log upstream response status, elapsed time, and `Retry-After` without printing API keys.
- Document multi-project, multi-key usage and how to diagnose `429` responses.

## 0.1.2 - 2026-07-11

### Fixed

- Relay NVIDIA SSE responses incrementally with `read1` instead of waiting for large read buffers.
- Avoid sending a second HTTP status after a streaming response has already started.
- Classify client disconnects separately from NVIDIA upstream failures.
- Explicitly configure setuptools package discovery so wheel builds ignore local backup and screenshot folders.
- Prevent false NVIDIA NIM detection when `nim` appears inside unrelated words.
- Display the effective host and port in the PowerShell launcher.

### Added

- Preserve NVIDIA-supported `stream_options` request data.
- Preserve `reasoning_effort` for the supported GPT-OSS model family while stripping it for GLM.
- Require explicit, client-key-only authorization for non-loopback proxy bindings.
- Validate CLI ports and timeout values with readable errors.
- Add an MIT license and correct package author metadata.
- Test Python 3.10 and 3.12 in CI and verify wheel construction.

### Changed

- Upgrade GitHub Actions to Node 24-based action versions.
- Limit tool-call diagnostic buffering to the first few parsed SSE events.
- Use process-scoped PowerShell execution-policy guidance.

## 0.1.1 - 2026-07-09

- Added configurable NVIDIA upstream timeout handling.
- Suppressed expected Windows client-disconnect tracebacks.
- Added HTTP 504 and client-disconnect troubleshooting documentation.

## 0.1.0 - 2026-07-08

- Initial ZCode NVIDIA NIM compatibility proxy.
- Added provider-specific `extra_body` sanitation, streaming support, Windows launchers, multi-key client mode, tests, CI, and security documentation.

# Security Policy

## Supported Use

This proxy is intended for local developer use with ZCode and NVIDIA NIM OpenAI-compatible chat completions. Keep the default loopback binding unless you have separately configured TLS termination, firewall rules, and client access controls.

## Secrets

- Do not commit API keys.
- Do not commit `.env`; it is intentionally ignored by Git.
- In default `env` mode, set `NVIDIA_API_KEY` in the local environment.
- In `client` mode, put each NVIDIA API key in the corresponding ZCode provider API key field.
- In `pool` mode, keep real NVIDIA keys only in `.env` and put only `NIM_PROXY_CLIENT_KEY` in ZCode.
- Generate a random local proxy key, keep it different from every NVIDIA key, and rotate it if exposed.
- Restrict `.env` filesystem permissions to the Windows account that runs the proxy.
- The `.env` loader accepts only the documented credential names and never executes file content.
- The proxy never logs API key values, local proxy credentials, or full message content in any mode.
- `/health` exposes aggregate queue/key-state counts only. It does not expose raw keys or fingerprints.

`env` mode cannot bind to a non-loopback address. `client` and `pool` modes still require explicit `--allow-remote`; that flag does not add TLS or firewall protection.

## Key Failure State

- `408` and `429` cool down only the affected key.
- `401` and `403` quarantine only the affected key until restart.
- A quarantined key should be removed or replaced in `.env` before restarting.
- Streaming requests are never replayed after response streaming begins.

## Reporting Issues

When reporting issues, include:

- proxy version
- Python version
- sanitized stripped key names
- HTTP status code
- queue mode and aggregate `/health` counts

Do not include `.env`, API keys, local proxy credentials, key fingerprints, or full prompt/message content.

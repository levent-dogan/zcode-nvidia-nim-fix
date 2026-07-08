# Security Policy

## Supported Use

This proxy is intended for local developer use with ZCode and NVIDIA NIM OpenAI-compatible chat completions.

## Secrets

- Do not commit API keys.
- In default `env` mode, set `NVIDIA_API_KEY` in the local environment.
- In `client` mode, put each NVIDIA API key in the corresponding ZCode provider API key field.
- The proxy never logs API keys in either mode.

## Reporting Issues

When reporting issues, include:

- proxy version
- Python version
- sanitized stripped key names
- HTTP status code

Do not include API keys or full prompt/message content.

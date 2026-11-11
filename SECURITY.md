# Security Policy

## Supported versions

Only the latest `main` is supported.

## Reporting a vulnerability

Please report security issues privately through the repository's issue tracker
rather than in a public issue. Include a description, the affected version and,
if possible, a reproduction.

The project holds no credentials of its own: it reads the Mistral API key from
the environment or from the harness credential store, and it never writes a
secret to disk, to its logs or to the trace file. If you find a path where a
key could leak into output, that is a bug worth reporting.

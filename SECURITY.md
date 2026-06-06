# Security Policy

## Scope

PhoneTransfer handles full device backups containing contacts, messages, photos, call history, and health data. Security issues in the following areas are in scope:

- Backup password exposure (logging, disk writes, memory leaks)
- Path traversal or arbitrary file write during backup extraction or injection
- Unauthenticated access via the companion TCP socket (port 7337)
- Companion APK impersonation (a malicious app binding port 7337 before the companion)
- PII leaking into log files

## Reporting a vulnerability

Email **rotor101riot@proton.me** with the subject line `[SECURITY] PhoneTransfer`.

Include:
- A description of the issue and its impact
- Steps to reproduce (device types, OS versions, categories involved)
- Any proof-of-concept code or logs (redact real personal data)

Please do not open a public GitHub issue for security vulnerabilities.

## What to expect

You will receive an acknowledgement within 7 days. Fixes for confirmed vulnerabilities will be prioritised over feature work. There is no bug bounty programme.

## Out of scope

- Vulnerabilities that require physical access to an already-trusted (paired) iOS device
- Issues in third-party tools (ADB, libimobiledevice, pymobiledevice3) — report those upstream
- Social engineering

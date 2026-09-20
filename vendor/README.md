# Vendor tools

No manual payload is required. The worker Dockerfile installs pinned BDRip_Scripts
and Sup2Sup repositories with their upstream lockfiles in isolated environments.
See [integration details](../docs/integrations.md). This directory is reserved for
future local vendor assets; its contents are not copied into the worker image.

The installed BDRip_Scripts environment supplies both CRF Studio and the release
stage's BBCode/NFO templates, MD5 hashing, verified private torrents, and resumable
TTG screenshot uploads. Configure `TU_TTG_TOKEN` in the project's `.env` for uploads.

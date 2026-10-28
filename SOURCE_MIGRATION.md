# ChannelHub 0.4.1 independent source migration

## Frozen source

- Repository: `JENN2046/VCPToolBox`
- Source commit: `fff563b2d33ad8aa93474714e050619a07e353e4`
- Source directory: `Plugin/ChannelHub/`
- Plugin manifest: `name=ChannelHub`, `version=0.4.1`, `pluginType=service`
- Copied without edits: all **46 tracked plugin files** under that directory
- Preserved regression tests: six `tests/channelHub*.test.js` files from the same commit
- Preserved root VCP `LICENSE` for VCP-derived material

The embedded `Plugin/ChannelHub/SOURCE_PROVENANCE.json` is an **earlier provenance record from the original integration effort**. Its `sourceRef`, `sourceCommit` and `integrationBase` describe that earlier process. They are intentionally preserved byte-for-byte and **must not be confused** with this repository's frozen migration source commit above.

The frozen Git repository tree represents the historical 0.4.1 source baseline. It does **not** certify the presence of every runtime-only production overlay or prove equivalence of external NapCat, QQ or installed host behavior.

## Ownership

This repository owns ChannelHub source, public B2 adapter contract, native VCP service wrapper, management routes, admin UI, core routing and tests. QQPersonal, other channel drivers, NativeSync, VCP's AgentManager, RAG, tools and desktop History keep their own code and authority.

The adapter API v1 and plugin install path are retained. The installed plugin directory must be `Plugin/ChannelHub/` relative to the **VCP host**, because `channel-hub.js` references the host's native `modules/agentManager`. This is expected by design, not a standalone server.

## Validation boundary

- `npm test` in this repository: no-dependency layout, manifest and sensitive-file smoke checks.
- GitHub Actions: checks exact file equality against the pinned source commit and runs preserved ChannelHub tests inside the pinned host fixture.
- The CI ZIP is an **install candidate**, not authorization to deploy, replace the running service, send real QQ messages or release C2.
- NativeSync C2-R3 is excluded from this source migration.

## Distribution and license

VCP-derived material retains the original VCP LICENSE (CC BY-NC-SA 4.0). This repository does not assert relicensing of upstream material or a separate commercial license. Verify the provenance and dependencies before broader distribution. Keep secrets, runtime database, credentials, adapter state, private media and diagnostic dumps outside Git.

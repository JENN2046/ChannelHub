# ChannelHub

**VCP-native multichannel gateway** · Stable source baseline: `0.4.1`

ChannelHub accepts authenticated channel events, binds channel conversations to existing VCP Agents, invokes the native VCP conversation endpoint and routes replies back to their source channels. It does **not** own VCP models, Agent prompts, memory stores, tools or desktop History.

## Source and deployment

- Source-of-record repository: `JENN2046/ChannelHub`.
- Frozen baseline: `JENN2046/VCPToolBox@fff563b2d33ad8aa93474714e050619a07e353e4`, `Plugin/ChannelHub/`.
- Install artifact: `Plugin/ChannelHub/` is packaged as a top-level `ChannelHub/` directory under the VCP host's `Plugin/` directory.
- The VCP runtime provides `modules/agentManager`, `PORT`, `Key` and the native plugin lifecycle. This is a **VCP service plugin**, not a separate Agent engine.
- Adapter implementations, including QQPersonal / OneBot, are **separate sibling plugins**; this repository does not ship NapCat or live credentials.
- Cross-end continuity and C2-R3 are **not part of the 0.4.1 stable baseline**.

## Layout

- `Plugin/ChannelHub/`: installable native VCP service, core modules, adapter contract, admin routes and UI
- `tests/`: preserved regression tests, executed in the pinned VCP host fixture
- `.github/workflows/`: source checks, pinned-host regression and ZIP package

Read [Plugin/ChannelHub/README.md](Plugin/ChannelHub/README.md) for configuration and adapter behavior.

## Safety and release

ChannelHub is opt-in; `ChannelHubEnabled=false` is the example default. Runtime credentials, adapter tokens, state databases, logs and private media **must not** be committed. This source migration does not alter any running VCP, QQ or C2 deployment. A green CI run is not a production QQ acceptance test.

The inherited VCP-derived material requires its upstream copyright and license notices. See [LICENSE](LICENSE) and [SOURCE_MIGRATION.md](SOURCE_MIGRATION.md) for scope and provenance.

## License

VCP-derived source retains upstream CC BY-NC-SA 4.0 obligations. Check copyright and distribution terms before commercial use.

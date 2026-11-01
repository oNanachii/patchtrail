# References and third party notices

The implementation was developed with AI assistance for this project. Architectural references inspected on 2026-10-28:

- mini-swe-agent, SWE-agent organization, MIT: https://github.com/SWE-agent/mini-swe-agent and its LICENSE.md. Reference: small model/environment/agent interfaces and a bounded trajectory loop.
- OpenHands Software Agent SDK, OpenHands organization, MIT: https://github.com/OpenHands/software-agent-sdk and its LICENSE. Reference: workspace, events and frontend/backend boundaries.
- Inspect AI, UK AI Security Institute, MIT: https://github.com/UKGovernmentBEIS/inspect_ai and its LICENSE. Reference: task/solver/scorer separation, explicit sandbox and evaluation logs.

No upstream framework source has been vendored into this version and no upstream benchmark result is claimed. Dependencies retain their own licenses. The three fixture repositories and demonstration fixes are authored for this project based on previously observed bug patterns, not third-party benchmark samples.

DeepSeek is an external API provider. Runtime usage follows the user's own account configuration. Credentials are excluded from source, fixtures, logs and public reports.

The frontend bundles React under the Meta Platforms MIT license and self-hosts JetBrains Mono under SIL Open Font License1.1. Exact notices are retained in licenses/React-MIT.txt and licenses/JetBrainsMono-OFL.txt, and the static distribution includes THIRD_PARTY_LICENSES.txt. These components retain their original licenses.

The optional QuixBugs benchmark downloader pins jkoppel/QuixBugs revision
4257f44b0ff1181dedaedee6a447e133219fcebf. Upstream identifies its license as MIT
(Copyright 2017–2019 James Koppel) and separately provides historical IP context
in legal_notes.txt. The dataset directory is generated locally. Published
benchmark request/response artifacts reproduce source and test excerpts;
[docs/QUIXBUGS_NOTICE.txt](docs/QUIXBUGS_NOTICE.txt) retains both upstream notices.
Results reported here are our own paired execution, not upstream benchmark scores.

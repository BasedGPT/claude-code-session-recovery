# Contributing

## Reporting issues

Open an issue and include:

- Output of `python tools/diagnose.py` (the most useful thing you can provide)
- Claude Code CLI version (`claude --version`)
- Claude Desktop version (Help → About)
- Windows version

## Pull requests

This repo is maintained on a best-effort basis. PRs are welcome for:

- New fixtures covering broken states not currently in the fixture suite
- New `troubleshooting.json` rows (must include a matching fixture)
- Bug fixes in existing tools
- Docs improvements

PRs that add new mutator scripts must pass all five mutator gates (see `docs/architecture.md`). A mutator without a fixture will not be merged.

Cross-platform support (macOS, Linux) is deferred to v2. PRs adding it are welcome in principle but will not be prioritised by the maintainer.

## Contact

Issues only. No direct email. Maintainer: [@BasedGPT](https://github.com/BasedGPT).

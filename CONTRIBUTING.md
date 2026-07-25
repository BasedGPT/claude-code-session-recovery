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
- New `troubleshooting.json` rows (must include a matching fixture, a matching
  `troubleshooting.md` section, and a documented `details` anchor)
- Bug fixes in existing tools
- Docs improvements

PRs that add new mutator scripts must pass all five mutator gates (see `docs/architecture.md`). A mutator without a fixture will not be merged.

## Fixture contracts

Run both suites before submitting a change:

```text
python -m pytest -q tests
python tests/run_fixture_tests.py
```

A fixture with `golden/dry-run.txt` must also include
`golden/dry-run.exit`. The fixture runner executes the mutator against an
isolated state copy and rejects:

- Any change to the copied state's content or layout during a dry run
- A dry-run exit status that differs from `dry-run.exit`
- Output that differs from `dry-run.txt`

`tests/regen_goldens.py` does not regenerate the exit contract. It refuses to
overwrite `dry-run.txt` if the exit status changes or the dry run modifies its
state copy. Review an exit-status change as a behaviour change, then update the
contract deliberately if that change is accepted.

Shared modules under `tools/` own reusable implementation. Executable scripts
own their CLI interface, reporting, paths, and repair policy. Preserve those
observable contracts when moving code across the seam.

Cross-platform support (macOS, Linux) is deferred to v2. PRs adding it are welcome in principle but will not be prioritised by the maintainer.

## Contact

Issues only. No direct email. Maintainer: [@BasedGPT](https://github.com/BasedGPT).

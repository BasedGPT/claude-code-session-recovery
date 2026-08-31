# Release process

This repository treats every change to the public tree as a versioned release.
The release identity is intentionally small and local:

- `VERSION` is the single source of the numeric `major.minor.patch` version.
- `docs/releases/vX.Y.Z.md` is the human release note for that version.
- `vX.Y.Z` is an annotated Git tag pointing to the exact tested commit.
- `tools/release.py` is the release-policy seam used by local checks and CI.

## Rules

Every pull request that changes the public repository must:

1. advance `VERSION` from the target branch's version;
2. add a new matching `docs/releases/vX.Y.Z.md` file with a `# vX.Y.Z` heading;
3. pass `python tools/release.py check --base <base-sha> --head <head-sha>`;
4. pass the normal test and fixture-contract suites.

Use a patch increment for fixes, documentation, and other non-breaking
changes; a minor increment for a backward-compatible capability; and a major
increment for a breaking CLI, output, or safety-contract change.

The check also requires the base version to have an annotated tag. This keeps
one untagged public release from silently becoming the base for another.

## Bootstrap and tagging

The first formal baseline is `v1.0.0`. After this baseline is merged and the
normal checks are green, create the annotated tag on the exact `main` commit:

```text
git fetch origin main --tags
git tag -a v1.0.0 origin/main -m "Release v1.0.0"
git push origin v1.0.0
```

For a later release, substitute the value from `VERSION`. Never move or delete
an existing `vX.Y.Z` tag.

The tag workflow runs the tests again, verifies the tag/version/commit binding,
and publishes a deterministic manifest containing raw-byte SHA-256 hashes for
`VERSION`, the matching release note, and every tracked file under `tools/`.
The manifest is a release asset rather than a tracked file so it does not need
to hash itself.

## Local verification

```text
python tools/release.py check --base origin/main --head HEAD
python tools/release.py tag-check --tag v1.0.0 --ref HEAD
python tools/release.py manifest --ref v1.0.0 --output release-manifest-v1.0.0.json
```

`check` is for a change before tagging. `tag-check` and `manifest` are for an
exact release ref. All commands are read-only except for the explicitly named
generated manifest output.

## GitHub repository settings

The repository ruleset must require pull requests and the `version-policy`,
`fixtures`, and `lint` checks before `main` can advance. It must deny
force-pushes to `main` and updates/deletions of `v*` tags. The release workflow
is the only workflow that needs `contents: write`, and only on a tag event.

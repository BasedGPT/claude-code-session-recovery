# Release process

This repository treats every change to the public tree as a versioned release.
The release identity is intentionally small and local:

- `VERSION` is the single source of the numeric `major.minor.patch` version.
- `vX.Y.Z` is an annotated Git tag pointing to the exact tested commit.
- `tools/release.py` is the small release-policy seam used by local checks and CI.

## Rules

Every pull request that changes the public repository must:

1. advance `VERSION` from the target branch's version;
2. pass `python tools/release.py check --base <base-sha> --head <head-sha>`;
3. pass the normal test and fixture-contract suites.

Use a patch increment for fixes, documentation, and other non-breaking
changes; a minor increment for a backward-compatible capability; and a major
increment for a breaking CLI, output, or safety-contract change.

The check also requires the base version to have an annotated tag. This keeps
one untagged public release from silently becoming the base for another.

## Tagging

After a versioned commit is merged and the normal checks are green, create the
annotated tag on the exact `main` commit:

```text
git fetch origin main --tags
git tag -a vX.Y.Z origin/main -m "Release vX.Y.Z"
git push origin vX.Y.Z
```

Replace `X.Y.Z` with the value from `VERSION`. Never move or delete an existing
`vX.Y.Z` tag.

The tag workflow runs the tests again and verifies the tag/version/commit
binding. The tag itself is the release record.

## Local verification

```text
python tools/release.py check --base origin/main --head HEAD
python tools/release.py tag-check --tag v1.0.0 --ref HEAD
```

`check` is for a change before tagging. `tag-check` is for an exact release ref.
Both commands are read-only.

## GitHub repository settings

The repository ruleset must require pull requests and the `version-policy`,
`fixtures`, and `lint` checks before `main` can advance. It must deny
force-pushes to `main` and updates/deletions of `v*` tags. The release workflow
only needs read access because releases are represented by the protected tag.

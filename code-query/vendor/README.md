# vendor/ — generated copy of the `cq` engine

`vendor/cq/` holds the index/search/CLI engine for the `code-query` Skill so
that this directory works after being copied into another repository.

## The copy is generated, and committed

`vendor/cq/` is a **generated** copy of the upstream `cq/` package, and it is
**committed** so that copying this directory into another repository yields a
working Skill without access to the upstream repository (FR-KIT-01).

Never hand-edit `vendor/cq/`. Fix upstream `cq/` and regenerate:

```powershell
pwsh -NoLogo -NoProfile -File sync-vendor.ps1
```

```bash
bash sync-vendor.sh
```

Drift between upstream and the copy is detected by
`hve/tests/test_cq_vendor_sync.py`, which compares every distributed file
byte for byte.

Run the sync inside the upstream repository (or pass `-Source` / `--source`
with a path to a `cq/` checkout) **before** committing.
Downstream copies cannot run the sync (they have no upstream), so `setup`
fails closed with a remediation message when `vendor/cq/` is missing.

## What the sync excludes

| Excluded | Reason |
|---|---|
| `cq/tests/` | Asserts against upstream repository paths (`hve-dev/...`) |
| `cq/golden-queries.json` | Expected paths and line numbers of the upstream repository only |
| `__pycache__/` | Build artefact |

同梱物は上流コミットとともに版管理されるため、バージョンは git 履歴から特定できる。

## Do not edit files under `vendor/cq/`

Fix bugs upstream in `cq/`, then re-run the sync. Direct edits are lost on the
next sync and are not covered by upstream tests.

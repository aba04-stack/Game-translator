# RPGMTL v5.0 — Quality Hardened

## Safety / correctness
- Inject is transactional: targeted source files and protected assets are snapshotted before writes.
- If an injector reports errors, modifies a protected asset, changes an unapproved JSON path, or raises an exception, RPGMTL restores the pre-inject bytes and reports failure.
- Rollback snapshots use independent file copies, never hardlinks, so in-place writes cannot corrupt the rollback copy.
- Durable original backups are created before QA/SmartWarp/injection when backup mode is enabled.
- RPG Maker keeps flat JSON backup compatibility plus path-preserving backups.
- RPG Maker dialogue/scroll blocks are injected only when every original line still exactly matches the Extract snapshot.
- RPG Maker entries now carry `source_path`, allowing exact source tracking, safety checks and project locking.
- JSON safety maps use the actual resolved source path instead of relying on filenames alone.

## Translation / dependency behavior
- OpenAI SDK loading is lazy: provider construction works for mocks/local tooling without the optional SDK; the real request reports a precise missing-dependency error.
- Engine-specific token validation remains mandatory before Inject.
- Manual translation edits remain authoritative when synchronized from `*_translated.json`.

## Verification
- Python bytecode compilation: passed.
- Transactional/asset safety + round-trip + engine token suites: 108 passed, 3 skipped.
- Full project test collection still has environment-dependent GUI/provider tests when optional packages are absent (`PyQt6`, `openai`, `rubymarshal`); those are not reported as passed.

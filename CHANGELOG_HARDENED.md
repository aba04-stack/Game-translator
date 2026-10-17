# RPGMTL v5.0 Hardened — audit changes

## Research-driven changes

RPGMTL was compared conceptually against current game-localization tools and engine-native approaches. The strongest recurring ideas were:

- Translator++ / similar editors: translation memory, context, glossary, editable structured entries.
- MTool: automatic engine detection and a simple Extract → Translate workflow.
- XUnity AutoTranslator: Unity-specific runtime handling, local translation cache, formatting/font concerns.
- Ren'Py native translation: engine syntax must remain separate from translated dialogue/UI strings.
- UABEA: Unity serialized files/AssetBundles require a real asset-aware layer; plain JSON/TXT extraction is not equivalent.

RPGMTL now adopts the safe parts of those ideas without pretending unsupported Unity binary formats are text files.

## Fixes in this build

### Cross-game safety
- Project manifest upgraded to v2.
- Extracted source files are fingerprinted with SHA-256.
- Inject accepts a changed live file only when the original backup matches the Extract fingerprint.
- Legacy flat RPG Maker backups are recognized.
- A workspace cannot silently be reused for a different game that happens to share the same engine/template/anchor files.

### Engine-specific translation safety
- Added protected syntax handling for Unity, Godot, Ren'Py, Tyrano, Kirikiri, Wolf and VX/Ace.
- Translation validation now checks both RPG escape codes and engine-specific placeholders/tags.
- Polish mode uses the same structural validation.
- Translation Memory remains namespaced by target language + engine + style profile.

### Extract/index integrity
- Pipeline metadata updates now merge into `extract_index.json` instead of replacing engine-specific fields.
- Unity `sources` mappings therefore survive Extract → Translate → Inject.

### Tests
- Added regression tests for engine-token protection/restoration.
- Added regression test proving that changing an extracted source file blocks Inject.
- Re-checked Unity/RPG Maker injection idempotency and backup restoration.

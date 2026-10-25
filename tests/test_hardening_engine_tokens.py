import json
from pathlib import Path

from engine.project_lock import write_manifest, verify_manifest
from ai_translator import _protect_codes, _restore_codes, _validate_engine_tokens


def test_unity_tokens_are_protected_and_restored():
    src = 'Hello {playerName} <color=#fff>{0}</color> %s ${score}'
    protected, mapping = _protect_codes(src, 'unity')
    assert protected != src
    restored = _restore_codes(protected, mapping)
    assert restored == src
    assert _validate_engine_tokens(src, restored, 'unity')


def test_renpy_tags_are_protected_and_validated():
    src = '{b}Hello {name}{/b} [player_name]'
    protected, mapping = _protect_codes(src, 'renpy')
    assert _restore_codes(protected, mapping) == src
    assert _validate_engine_tokens(src, _restore_codes(protected, mapping), 'renpy')
    assert not _validate_engine_tokens(src, 'Привет', 'renpy')


def test_project_lock_binds_extracted_source_content(tmp_path):
    game = tmp_path / 'game'
    out = tmp_path / 'out'
    (game / 'data').mkdir(parents=True)
    (game / 'data' / 'Common.json').write_text('{"text":"Hello"}', encoding='utf-8')
    (out / 'extracted').mkdir(parents=True)
    (out / 'extracted' / 'Common.json').write_text(json.dumps([
        {'file': 'Common.json', 'source_path': 'data/Common.json', 'original': 'Hello', 'translated': ''}
    ]), encoding='utf-8')
    (out / 'extract_index.json').write_text(json.dumps({
        'game_type': 'unity', 'sources': {'Common.json': 'data/Common.json'}
    }), encoding='utf-8')
    write_manifest(out, game, 'unity')
    assert verify_manifest(out, game, 'unity')[0]
    (game / 'data' / 'Common.json').write_text('{"text":"OTHER GAME"}', encoding='utf-8')
    assert not verify_manifest(out, game, 'unity')[0]


def test_rpgmv_plugin_placeholders_are_protected_and_validated():
    """
    BUG (найдено по прямому запросу пользователя — "у всех движков свой
    режим перевода, проверь, подходит ли"): rpgmv/rpgmz были ЕДИНСТВЕННЫМИ
    из 9 движков БЕЗ записи в _ENGINE_TOKEN_PATTERNS. _validate_codes
    покрывает классические RPG Maker коды (\\C[n], \\N[n]) — но множество
    популярных плагинов (Yanfly Engine Plugins, VisuStella MZ и другие,
    которыми пользуется огромная доля реальных MV/MZ игр) вводят СВОИ
    плейсхолдеры: позиционные %1/%2 ("Welcome, %1! You have %2 gold.") и
    JS-template-подобные {playerName}. Перевод, полностью потерявший такой
    плейсхолдер, проходил валидацию как корректный — эмпирически
    подтверждено до фикса.

    _protect_codes/_restore_codes используют тот же словарь — значит фикс
    не только ловит потерю постфактум, но и ПРЕДОТВРАЩАЕТ её: плейсхолдер
    заменяется на непрозрачный маркер §N§ ДО отправки в ИИ и восстанавливается
    после, так что модель физически не видит и не может испортить исходный
    токен.
    """
    src = "Welcome, %1! You have %2 gold. Hello {playerName}, quest complete!"
    for game_type in ("rpgmv", "rpgmz"):
        protected, mapping = _protect_codes(src, game_type)
        assert protected != src, f"{game_type}: плейсхолдеры плагина должны были защититься"
        restored = _restore_codes(protected, mapping)
        assert restored == src
        assert _validate_engine_tokens(src, restored, game_type)

        # Перевод, потерявший плейсхолдеры целиком, должен провалить валидацию.
        assert not _validate_engine_tokens(src, "Добро пожаловать! Квест завершён!", game_type), (
            f"БАГ: {game_type} не заметил полностью потерянные %1/%2/{{playerName}}"
        )
        # Легитимный перевод, сохранивший все токены -- должен пройти.
        good = "Добро пожаловать, %1! У вас %2 золота. Привет, {playerName}, квест завершён!"
        assert _validate_engine_tokens(src, good, game_type)

def test_engine_imports():
    from engine import injector, extractor
    assert hasattr(injector, 'inject_translations')
    assert hasattr(extractor, 'extract_game')

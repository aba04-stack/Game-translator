from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_windows_icon_is_a_well_formed_ico_file():
    icon = (ROOT / "icon.ico").read_bytes()

    assert len(icon) >= 1024
    assert icon[:4] == bytes((0, 0, 1, 0))
    count = int.from_bytes(icon[4:6], "little")
    assert count >= 1

    directory_end = 6 + 16 * count
    assert directory_end <= len(icon)
    for index in range(count):
        entry = 6 + 16 * index
        size = int.from_bytes(icon[entry + 8:entry + 12], "little")
        offset = int.from_bytes(icon[entry + 12:entry + 16], "little")
        assert size > 0
        assert offset >= directory_end
        assert offset + size <= len(icon)


def test_windows_build_and_installer_embed_the_icon():
    builder = (ROOT / "build_exe.bat").read_text(encoding="utf-8")
    installer = (ROOT / "packaging" / "PatchLingo.iss").read_text(encoding="utf-8")
    gui = (ROOT / "rpgmtl_desktop.py").read_text(encoding="utf-8")

    assert '--icon "icon.ico"' in builder
    assert '--add-data "icon.ico;."' in builder
    assert 'SetupIconFile=..\\icon.ico' in installer
    assert "QIcon(str(icon_path))" in gui
    assert "w.setWindowIcon(app_icon)" in gui

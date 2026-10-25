from pathlib import Path
import json
import tempfile
from pipeline import Pipeline

def test_full_pipeline_mock():
    # Создаем фейковую структуру игры
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_path = Path(tmpdir)
        game_dir = tmp_path / "game"
        out_dir = tmp_path / "output"
        game_dir.mkdir()
        
        # MV/MZ структура
        data_dir = game_dir / "data"
        data_dir.mkdir()
        
        with open(data_dir / "System.json", "w", encoding="utf-8") as f:
            json.dump({"gameTitle": "Test Game"}, f)
            
        # Создаем Map001.json как dict (как в реальном MV/MZ)
        map_data = {
            "events": [
                None,
                {
                    "pages": [
                        {
                                "list": [
                                    {"code": 101, "parameters": ["", 0, 0, 2, "Speaker"]},
                                    {"code": 401, "parameters": ["Привет, мир!"]},
                                    {"code": 401, "parameters": ["Как дела?"]}
                                ]
                        }
                    ]
                }
            ]
        }
        
        with open(data_dir / "Map001.json", "w", encoding="utf-8") as f:
            json.dump(map_data, f)
            
        p = Pipeline(str(game_dir), str(out_dir))
        
        # 1. Извлечение
        ok, msg = p.extract()
        assert ok is True
        
        # 2. Мокаем перевод (создаем _translated.json)
        extracted_file = out_dir / "extracted" / "Map001.json"
        with open(extracted_file, "r", encoding="utf-8") as f:
            extracted_data = json.load(f)
            
        for e in extracted_data:
            e["translated"] = "\n".join([line + " [TR]" for line in e["original"].split("\n")])
            
        with open(out_dir / "extracted" / "Map001_translated.json", "w", encoding="utf-8") as f:
            json.dump(extracted_data, f)
            
        # 3. Инжект
        inj_ok, inj_msg = p.inject(apply_warp=False)
        assert inj_ok is True
        
        # 4. Проверяем файл игры
        with open(data_dir / "Map001.json", "r", encoding="utf-8") as f:
            new_map_data = json.load(f)
            
        assert new_map_data["events"][1]["pages"][0]["list"][1]["parameters"][0] == "Привет, мир! [TR]"
        assert new_map_data["events"][1]["pages"][0]["list"][2]["parameters"][0] == "Как дела? [TR]"
        
        # 5. Проверяем защиту от двойной инъекции (идемпотентность)
        # Если мы еще раз вызовем inject, он должен использовать бэкап и не сломать файл
        inj_ok2, inj_msg2 = p.inject(apply_warp=False)
        assert inj_ok2 is True
        
        with open(data_dir / "Map001.json", "r", encoding="utf-8") as f:
            new_map_data2 = json.load(f)
            
        # Текст все еще должен быть [TR], а не [TR] [TR] (потому что откатился к оригиналу перед вставкой)
        assert new_map_data2["events"][1]["pages"][0]["list"][1]["parameters"][0] == "Привет, мир! [TR]"

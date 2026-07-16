"""原子寫入測試：確保寫入不留半截檔、能覆蓋既有檔、會建父目錄。"""
import json

from app.atomicio import atomic_write_text


def test_creates_parent_dirs_and_writes(tmp_path):
    target = tmp_path / "nested" / "deep" / "data.json"
    atomic_write_text(target, json.dumps({"a": 1}, ensure_ascii=False))
    assert json.loads(target.read_text(encoding="utf-8")) == {"a": 1}


def test_overwrites_existing_file(tmp_path):
    target = tmp_path / "data.json"
    atomic_write_text(target, "舊內容")
    atomic_write_text(target, "新內容")
    assert target.read_text(encoding="utf-8") == "新內容"


def test_no_temp_files_left_behind(tmp_path):
    target = tmp_path / "data.json"
    atomic_write_text(target, "內容")
    # 不殘留 .tmp* 暫存檔
    assert [p.name for p in tmp_path.iterdir()] == ["data.json"]

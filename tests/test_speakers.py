"""講者名冊：累積順序、安全性過濾、上限與 prompt 片段。"""
import pytest

from app.speakers import MAX_NAMES, SpeakerRoster
from app.stores.local_store import LocalJsonStore


def make_roster(tmp_path):
    return SpeakerRoster(LocalJsonStore(tmp_path / "db.json"))


def test_empty_roster(tmp_path):
    assert make_roster(tmp_path).names() == []


def test_remember_and_reload(tmp_path):
    path = tmp_path / "db.json"
    SpeakerRoster(LocalJsonStore(path)).remember(["王霖翔", "李經理"])
    # 重新載入（等同重啟服務）要還在——證明有進資料庫
    assert SpeakerRoster(LocalJsonStore(path)).names() == ["王霖翔", "李經理"]


def test_remember_moves_existing_name_to_front(tmp_path):
    """最近用到的排前面：名冊滿了要淘汰時，掉的才會是真的沒在用的名字。"""
    roster = make_roster(tmp_path)
    roster.remember(["A君", "B君", "C君"])
    roster.remember(["B君"])
    assert roster.names() == ["B君", "A君", "C君"]


def test_remember_skips_unsafe_names(tmp_path):
    """代號、含冒號、過長的字串進了名冊只會污染 prompt，靜靜濾掉。"""
    roster = make_roster(tmp_path)
    roster.remember(["講者A", "王霖翔：", "好" * 30, "  ", "王霖翔"])
    assert roster.names() == ["王霖翔"]


def test_remember_never_raises(tmp_path):
    """名冊是分析流程的副作用，壞資料不該讓整場分析失敗。"""
    assert make_roster(tmp_path).remember(["講者A", ""]) == []


def test_remember_caps_at_max(tmp_path):
    roster = make_roster(tmp_path)
    names = roster.remember([f"人{i}" for i in range(MAX_NAMES + 10)])
    assert len(names) == MAX_NAMES
    assert names[0] == "人0"  # 傳入順序即新到舊，超出的尾巴被丟掉


def test_replace_overwrites_whole_list(tmp_path):
    roster = make_roster(tmp_path)
    roster.remember(["舊的"])
    assert roster.replace([" 王霖翔 ", "李經理", "王霖翔"]) == ["王霖翔", "李經理"]


def test_replace_rejects_bad_input(tmp_path):
    """設定畫面手動輸入的錯誤要講清楚，不像 remember 那樣默默吞掉。"""
    roster = make_roster(tmp_path)
    with pytest.raises(ValueError):
        roster.replace(["   "])
    with pytest.raises(ValueError):
        roster.replace(["講者A"])
    with pytest.raises(ValueError):
        roster.replace([f"人{i}" for i in range(MAX_NAMES + 1)])

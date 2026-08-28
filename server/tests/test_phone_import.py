"""電話番号の正規化と CSV 取り込み。

★ ここが緩むと DNC 照合が漏れる。`090-1234-5678` と `+819012345678` が
  同じ人を指すのに違う文字列で入ると、断った相手にかけることになる。
  正規化は取り込み時に強制し、失敗したら**入れない**。

★ 「全件検証してから」を守れているかも見る。途中で落ちて 380 件だけ
  入った状態は、再取り込みで重複するか、どこから再開するか分からなくなる。
"""

from __future__ import annotations

import pytest

from app.domain.phone import (
    InvalidPhoneNumber,
    parse_contacts_csv,
    parse_phone_list,
    to_e164,
)

# ---------------------------------------------------------------- 正規化


@pytest.mark.parametrize(
    "raw",
    [
        "09012345678",
        "090-1234-5678",
        "090 1234 5678",
        "+819012345678",
        "+81 90-1234-5678",
        "０９０−１２３４−５６７８",   # 全角。Excel から貼ると混ざる
    ],
)
def test_同じ番号はどう書かれても同じE164になる(raw):
    """★ これが DNC 照合の前提。1 つでも別の文字列になると照合が漏れる。"""
    assert to_e164(raw) == "+819012345678"


def test_固定電話も正規化できる():
    assert to_e164("03-1234-5678") == "+81312345678"


@pytest.mark.parametrize(
    "raw",
    ["", "   ", "abc", "090-1234", "12345678901234567890", "090-1234-567X"],
)
def test_不正な番号は拒否する(raw):
    """★ 握り潰して「そのまま入れる」を選ぶと、DNC 照合をすり抜ける行が生まれる。
    取り込みが 1 件失敗するほうが、断った相手にかけるより安い。
    """
    with pytest.raises(InvalidPhoneNumber):
        to_e164(raw)


# ---------------------------------------------------------------- CSV


def test_見出しの揺れを吸収する():
    """現場の CSV は毎回違う見出しで来る。"""
    for header in ["phone", "TEL", "電話番号", "phone_number"]:
        result = parse_contacts_csv(f"{header},company\n09012345678,テスト株式会社\n")
        assert result.ok, result.rejected
        assert result.accepted[0].phone_e164 == "+819012345678"


def test_会社名と担当者を取り込む():
    result = parse_contacts_csv(
        "電話番号,会社名,担当者,部署\n"
        "090-1234-5678,株式会社サンプル,佐藤 一郎,営業部\n"
    )
    row = result.accepted[0]
    assert (row.company_name, row.person_name, row.department) == (
        "株式会社サンプル", "佐藤 一郎", "営業部"
    )


def test_電話番号の列が無ければ何も取り込まない():
    result = parse_contacts_csv("会社名,担当者\nサンプル,佐藤\n")
    assert not result.ok
    assert "電話番号の列が見つかりません" in result.rejected[0].reason


def test_空ファイルを安全に扱う():
    assert not parse_contacts_csv("").ok


def test_空行は飛ばす():
    result = parse_contacts_csv("phone\n09012345678\n\n\n09087654321\n")
    assert result.ok
    assert len(result.accepted) == 2


# ★ ファイル内の重複を弾く。同じ相手に 2 回かける原因になる
def test_ファイル内の重複を弾く():
    result = parse_contacts_csv(
        "phone\n090-1234-5678\n09012345678\n"   # 表記違いの同じ番号
    )
    assert len(result.accepted) == 1
    assert len(result.rejected) == 1
    assert "重複" in result.rejected[0].reason


def test_不正な行は行番号付きで返す():
    """どの行を直せばよいか分からないと、取り込みが終わらない。"""
    result = parse_contacts_csv("phone\n09012345678\nabc\n09087654321\n")
    assert len(result.accepted) == 2
    assert result.rejected[0].line == 3      # 見出しが 1 行目
    assert result.rejected[0].raw == "abc"


def test_不正な行があってもokにはならない():
    """★ 呼び出し側が「全件検証してから 1 トランザクション」を判断できること。"""
    assert not parse_contacts_csv("phone\n09012345678\nabc\n").ok


def test_BOM付きの見出しを扱える():
    """Excel が保存する CSV は BOM 付きになる。"""
    result = parse_contacts_csv("﻿phone,company\n09012345678,サンプル\n")
    assert result.ok, result.rejected


# ---------------------------------------------------------------- DNC


def test_DNCは1行1番号で取り込める():
    result = parse_phone_list("090-1234-5678\n03-1234-5678\n")
    assert [r.phone_e164 for r in result.accepted] == ["+819012345678", "+81312345678"]


def test_DNCはCSVの1列目を見る():
    result = parse_phone_list("090-1234-5678,拒否,2026-01-06\n")
    assert result.accepted[0].phone_e164 == "+819012345678"


# ★ 連絡先と違い、DNC は重複しても害がない。黙って飛ばす
def test_DNCの重複は黙って飛ばす():
    result = parse_phone_list("09012345678\n090-1234-5678\n")
    assert len(result.accepted) == 1
    assert result.rejected == []


def test_DNCは不正な行だけを報告する():
    """★ 1 件不正でも他は入れる。DNC は入れ過ぎても害がない側なので、
    全部止めるほうが危険。
    """
    result = parse_phone_list("09012345678\nabc\n03-1234-5678\n")
    assert len(result.accepted) == 2
    assert len(result.rejected) == 1

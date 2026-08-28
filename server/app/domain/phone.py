"""電話番号の正規化。

★ DNC の照合は**文字列の完全一致**で行う。`090-1234-5678` と `09012345678` と
  `+819012345678` は同じ人を指すのに、列に混在した形式で入ると照合が漏れる。
  **漏れた結果が「断った相手への再架電」になる**ので、正規化は投入時に強制する。

★ 検証に失敗したら取り込みを拒否する。例外を握り潰して「そのまま入れる」を
  選ぶと、DNC 照合をすり抜ける行が生まれる。取り込みが 1 件失敗するほうが、
  断った相手にかけるより安い。
"""

from __future__ import annotations

from dataclasses import dataclass


class InvalidPhoneNumber(ValueError):
    pass


def to_e164(raw: str, region: str = "JP") -> str:
    """任意の表記を E.164 に正規化する。失敗したら例外。"""
    text = (raw or "").strip()
    if not text:
        raise InvalidPhoneNumber("空の電話番号")

    try:
        import phonenumbers
    except ImportError:  # pragma: no cover - 依存が入っていれば通らない
        return _fallback(text)

    try:
        parsed = phonenumbers.parse(text, region)
    except phonenumbers.NumberParseException as exc:
        raise InvalidPhoneNumber(f"解析できません: {raw}") from exc

    if not phonenumbers.is_valid_number(parsed):
        raise InvalidPhoneNumber(f"有効な番号ではありません: {raw}")

    return phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.E164)


def _fallback(text: str) -> str:
    """phonenumbers が無い環境向けの最小実装。日本の番号だけを想定する。"""
    digits = "".join(ch for ch in text if ch.isdigit() or ch == "+")
    if digits.startswith("+"):
        if len(digits) < 8:
            raise InvalidPhoneNumber(f"短すぎます: {text}")
        return digits
    if digits.startswith("0") and len(digits) in (10, 11):
        return "+81" + digits[1:]
    raise InvalidPhoneNumber(f"正規化できません: {text}")


@dataclass(frozen=True)
class ParsedRow:
    line: int
    phone_e164: str
    company_name: str | None = None
    person_name: str | None = None
    department: str | None = None


@dataclass(frozen=True)
class RejectedRow:
    line: int
    raw: str
    reason: str


@dataclass(frozen=True)
class ParseResult:
    accepted: list[ParsedRow]
    rejected: list[RejectedRow]

    @property
    def ok(self) -> bool:
        return not self.rejected


# 列名の揺れを吸収する。現場の CSV は毎回違う見出しで来る
_PHONE_HEADERS = {"phone", "tel", "telephone", "電話番号", "電話", "tel1", "phone_number"}
_COMPANY_HEADERS = {"company", "company_name", "会社名", "企業名", "法人名"}
_PERSON_HEADERS = {"name", "person", "person_name", "氏名", "担当者", "担当者名"}
_DEPARTMENT_HEADERS = {"department", "部署", "部署名", "所属"}


def _pick(headers: list[str], candidates: set[str]) -> int | None:
    for index, header in enumerate(headers):
        if header.strip().lower().lstrip("﻿") in candidates:
            return index
    return None


def parse_contacts_csv(text: str, *, region: str = "JP") -> ParseResult:
    """連絡先の CSV を解析する。

    ★ 全件を検証してから返す。途中で例外にすると、呼び出し側が
      「1000 件中 380 件だけ入った」状態を作りやすい。再取り込みで重複するか、
      どこから再開するか分からなくなる。
    """
    import csv
    import io

    reader = csv.reader(io.StringIO(text))
    try:
        headers = next(reader)
    except StopIteration:
        return ParseResult([], [RejectedRow(0, "", "ファイルが空です")])

    phone_at = _pick(headers, _PHONE_HEADERS)
    if phone_at is None:
        return ParseResult(
            [],
            [
                RejectedRow(
                    1,
                    ",".join(headers),
                    "電話番号の列が見つかりません（phone / tel / 電話番号 のいずれか）",
                )
            ],
        )

    company_at = _pick(headers, _COMPANY_HEADERS)
    person_at = _pick(headers, _PERSON_HEADERS)
    department_at = _pick(headers, _DEPARTMENT_HEADERS)

    def cell(row: list[str], index: int | None) -> str | None:
        if index is None or index >= len(row):
            return None
        return row[index].strip() or None

    accepted: list[ParsedRow] = []
    rejected: list[RejectedRow] = []
    seen: dict[str, int] = {}

    for line, row in enumerate(reader, start=2):
        if not any(c.strip() for c in row):
            continue
        raw = cell(row, phone_at) or ""
        try:
            phone = to_e164(raw, region)
        except InvalidPhoneNumber as exc:
            rejected.append(RejectedRow(line, raw, str(exc)))
            continue

        # ★ ファイル内の重複も弾く。同じ相手に 2 回かける原因になる
        if phone in seen:
            rejected.append(
                RejectedRow(line, raw, f"{seen[phone]} 行目と重複しています")
            )
            continue
        seen[phone] = line

        accepted.append(
            ParsedRow(
                line=line,
                phone_e164=phone,
                company_name=cell(row, company_at),
                person_name=cell(row, person_at),
                department=cell(row, department_at),
            )
        )

    return ParseResult(accepted, rejected)


def parse_phone_list(text: str, *, region: str = "JP") -> ParseResult:
    """DNC 取り込み用。1 行 1 番号、または CSV の 1 列目。"""
    accepted: list[ParsedRow] = []
    rejected: list[RejectedRow] = []
    seen: set[str] = set()

    for line, raw_line in enumerate(text.splitlines(), start=1):
        raw = raw_line.split(",")[0].strip()
        if not raw:
            continue
        try:
            phone = to_e164(raw, region)
        except InvalidPhoneNumber as exc:
            rejected.append(RejectedRow(line, raw, str(exc)))
            continue
        if phone in seen:
            continue          # DNC は重複しても害がないので黙って飛ばす
        seen.add(phone)
        accepted.append(ParsedRow(line=line, phone_e164=phone))

    return ParseResult(accepted, rejected)

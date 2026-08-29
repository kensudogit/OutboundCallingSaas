#!/usr/bin/env python3
"""デプロイ前に、足りない環境変数を洗い出して設定コマンドを組み立てる。

    python scripts/check-deploy-env.py                        # Fly / 全アプリ
    python scripts/check-deploy-env.py --app media            # 1 つだけ
    python scripts/check-deploy-env.py --print-only           # CLI を叩かず一覧だけ
    python scripts/check-deploy-env.py --platform railway     # Railway を確認

★ 不足を「1 件ずつ」出さない。PaaS では 1 件直すたびに再デプロイなので、
  6 個足りなければ 6 回デプロイし直すことになる。まとめて 1 コマンドにする。

★ 必須の一覧は config.py の _required(...) から読む。設定を足したときに
  ここを直し忘れて、デプロイしてから気付くのを避けるため。二重管理しない。

★ 値は表示しない。fly secrets list も名前しか返さないので、
  このスクリプトの出力をそのまま貼っても秘密は漏れない。
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "server" / "app" / "config.py"

# fly.toml の [env] に書いてあるもの。secrets には要らない
FROM_FLY_TOML = {"APP_ENV", "PORT", "MEDIA_PORT", "NODE_ENV"}

# Postgres をアタッチすると Fly が自動で入れる
AUTO_BY_PLATFORM = {"DATABASE_URL"}

# 必須ではないが、無いと機能が止まるもの
IMPORTANT_OPTIONAL = {
    "DATABASE_MIGRATOR_URL": (
        "Alembic が使う。未設定だと migrate が落ちる。BYPASSRLS を持つ migrator ロール"
    ),
    "PUBLIC_WSS_URL": "未設定だと PUBLIC_BASE_URL から推測される。media アプリを指すべき",
    "REDIS_URL": "未設定だと通話中の文字起こしが画面に出ない（通話自体は成立する）",
    # ★ media サービスだけの話。entrypoint の media は MEDIA_PORT を見て bind するので、
    #   PaaS が待つポート（PORT）と揃えないとヘルスチェックが通らず、
    #   「Deploying のまま進まない」になる
    "MEDIA_PORT": "media が bind するポート。PORT と同じ値にする（揃えないとヘルスチェックが通らない）",
}

APPS = {
    "api": {
        "config": "fly.toml",
        "note": "API + 定期ジョブ",
        "extra": ["DATABASE_MIGRATOR_URL", "PUBLIC_WSS_URL", "REDIS_URL"],
    },
    "media": {
        "config": "fly.media.toml",
        "note": "音声ワーカー。署名検証と DB 記録を行うので同じ値が要る",
        "extra": ["DATABASE_MIGRATOR_URL", "PUBLIC_WSS_URL", "REDIS_URL"],
    },
    "web": {
        "config": "web/fly.toml",
        "note": "フロント。サーバー側の変数は要らない",
        "only": ["API_BASE_URL", "NEXT_PUBLIC_WSS_URL"],
    },
}


# ---------------------------------------------------------------- Railway

# ★ Railway は「サービス」単位。fly.toml のようなファイルからは名前を引けないので、
#   リンク済みプロジェクトのサービス名をそのまま使う。
RAILWAY_SERVICES = {
    "api": {
        "service": "OutboundCallingSaas",
        "note": "API。railway.toml を Config as Code に指定する",
        "extra": ["DATABASE_MIGRATOR_URL", "PUBLIC_WSS_URL", "REDIS_URL"],
    },
    "media": {
        "service": "OutboundCallingSaas-media",
        "note": "音声ワーカー。railway.media.toml。値は api と同一にする",
        "extra": ["DATABASE_MIGRATOR_URL", "PUBLIC_WSS_URL", "REDIS_URL", "MEDIA_PORT"],
    },
    "jobs": {
        "service": "OutboundCallingSaas-jobs",
        "note": "定期ジョブ。railway.jobs.toml。ドメインは生成しない",
        "extra": ["DATABASE_MIGRATOR_URL", "REDIS_URL"],
    },
}

# Railway が自動で入れるもの。config.py はこれらから PUBLIC_BASE_URL を補完する
RAILWAY_AUTO = {"RAILWAY_PUBLIC_DOMAIN", "RAILWAY_STATIC_URL", "PORT"}

# ★ Railway の Postgres がくれる DATABASE_URL は postgres（superuser）。
#   そのまま使うと RLS が素通りし、起動時のガード（assert_rls_enforced）で
#   production は落ちる。db/bootstrap-roles.sql で作ったロールに向け直す。
RAILWAY_PLACEHOLDERS = {
    "JWT_SECRET": "$(openssl rand -hex 32)",
    "TWILIO_ACCOUNT_SID": "AC********************************",
    "TWILIO_AUTH_TOKEN": "********************************",
    "TWILIO_CALLER_ID": "+81XXXXXXXXXX",
    # 未設定でも RAILWAY_PUBLIC_DOMAIN から補完される。明示するならこの形
    "PUBLIC_BASE_URL": "https://${{RAILWAY_PUBLIC_DOMAIN}}",
    "PUBLIC_WSS_URL": "wss://<media サービスのドメイン>",
    "DATABASE_URL": (
        "postgresql://app_user:<pass>@${{Postgres.RAILWAY_PRIVATE_DOMAIN}}"
        ":5432/${{Postgres.PGDATABASE}}"
    ),
    "DATABASE_MIGRATOR_URL": (
        "postgresql://migrator:<pass>@${{Postgres.RAILWAY_PRIVATE_DOMAIN}}"
        ":5432/${{Postgres.PGDATABASE}}"
    ),
    "REDIS_URL": "${{Redis.REDIS_URL}}",
    "MEDIA_PORT": "8080",
    "PORT": "8080",
}


def railway_variable_names(service: str) -> set[str] | None:
    """設定済みの変数名を取る。値は読まない（貼っても漏れないようにする）。"""
    # ★ Windows の npm シムは railway.cmd。argv[0] を "railway" のままにすると
    #   CreateProcess が解決できず、常に「取得できませんでした」になる
    exe = shutil.which("railway")
    if not exe:
        return None
    try:
        result = subprocess.run(
            [exe, "variables", "--kv", "-s", service],
            capture_output=True, text=True, timeout=30, check=False,
        )
    except (subprocess.SubprocessError, OSError):
        return None
    if result.returncode != 0:
        return None
    names = set()
    for line in result.stdout.splitlines():
        if "=" in line:
            names.add(line.split("=", 1)[0].strip())
    return names or None


def report_railway(key: str, spec: dict, required: dict[str, str], print_only: bool) -> int:
    service = spec["service"]

    print()
    print("=" * 74)
    print(f" {key}: {service} — {spec['note']}")
    print("=" * 74)

    wanted = {k: v for k, v in required.items() if k not in FROM_FLY_TOML}
    for extra in spec.get("extra", []):
        wanted.setdefault(extra, IMPORTANT_OPTIONAL.get(extra, ""))

    existing = None if print_only else railway_variable_names(service)

    if existing is None:
        if not print_only:
            print("  ※ railway から取得できませんでした")
            print("     （未ログイン / railway link 未実行 / サービス名違い）")
            print("     必要な変数の一覧だけ出します")
        missing = sorted(wanted)
    else:
        missing = sorted(k for k in wanted if k not in existing)
        for k in sorted(k for k in wanted if k in existing):
            print(f"  [設定済] {k}")
        # ★ Railway の DATABASE_URL は自動で入るが、それは superuser の URL。
        #   「設定済」に見えても RLS のガードで落ちるので、ここで警告する
        if "DATABASE_URL" in existing:
            print("  [要確認] DATABASE_URL — Railway の既定値は postgres(superuser)。")
            print("           そのままだと RLS が効かず、起動時のガードで落ちます。")
            print("           db/bootstrap-roles.sql を流し、app_user に向け直すこと")

    if not missing:
        print("  不足なし")
        return 0

    print()
    for k in missing:
        hint = wanted.get(k) or IMPORTANT_OPTIONAL.get(k, "")
        print(f"  [不足] {k}" + (f" — {hint}" if hint else ""))

    print()
    print("  設定コマンド（1 回で全部入れる。1 件ずつ入れるとその数だけ再デプロイになる）:")
    print()
    print(f"    railway variables -s {service} \\")
    for i, k in enumerate(missing):
        tail = " \\" if i < len(missing) - 1 else ""
        value = SHARED_WITH_API.get(k) if key != "api" else None
        value = value or RAILWAY_PLACEHOLDERS.get(k, "<値>")
        print(f'      --set "{k}={value}"{tail}')
    if key != "api":
        print()
        print("    ★ 値は api と同一にすること。特に JWT_SECRET を生成し直すと、")
        print("      api が発行したトークンをこのサービスで検証できなくなる。")
    return len(missing)


def required_from_config() -> dict[str, str]:
    """config.py の _required(...) を唯一の出所として読む。"""
    text = CONFIG.read_text(encoding="utf-8")
    found: dict[str, str] = {}
    for match in re.finditer(r'_required\(\s*"(\w+)"\s*,\s*"([^"]*)"', text):
        found[match.group(1)] = match.group(2)
    return found


def app_name(config_path: Path) -> str | None:
    if not config_path.exists():
        return None
    match = re.search(r'^\s*app\s*=\s*"([^"]+)"', config_path.read_text(encoding="utf-8"), re.M)
    return match.group(1) if match else None


def fly_secrets(app: str) -> set[str] | None:
    """設定済みの secret 名を取る。値は返らない。"""
    exe = shutil.which("fly")
    if not exe:
        return None
    try:
        result = subprocess.run(
            [exe, "secrets", "list", "-a", app, "--json"],
            capture_output=True, text=True, timeout=30, check=False,
        )
    except (subprocess.SubprocessError, OSError):
        return None
    if result.returncode != 0:
        return None
    try:
        return {item["Name"] for item in json.loads(result.stdout)}
    except (json.JSONDecodeError, KeyError, TypeError):
        return None


PLACEHOLDERS = {
    "JWT_SECRET": "$(openssl rand -hex 32)",
    "TWILIO_ACCOUNT_SID": "AC********************************",
    "TWILIO_AUTH_TOKEN": "********************************",
    "TWILIO_CALLER_ID": "+81XXXXXXXXXX",
    "PUBLIC_BASE_URL": "https://<api アプリ>.fly.dev",
    "PUBLIC_WSS_URL": "wss://<media アプリ>.fly.dev",
    "DATABASE_URL": "postgres://app_user:...@<db>.flycast:5432/<db>",
    "DATABASE_MIGRATOR_URL": "postgres://migrator:...@<db>.flycast:5432/<db>",
    "REDIS_URL": "redis://<redis>:6379/0",
    "API_BASE_URL": "http://<api アプリ>.internal:8000",
    "NEXT_PUBLIC_WSS_URL": "wss://<media アプリ>.fly.dev",
}


# ★ アプリごとに値が違ってはいけないもの。api で決めた値をそのまま使う。
#   $(openssl rand -hex 32) をアプリごとに実行すると別々の値になり、
#   api が発行したトークンを media 側で検証できなくなる（今は検証していないが、
#   後で検証を足したときに「なぜか通らない」形で表面化する）
SHARED_WITH_API = {
    "JWT_SECRET": "<api と同じ値>",
    "TWILIO_AUTH_TOKEN": "<api と同じ値>",
    "TWILIO_ACCOUNT_SID": "<api と同じ値>",
    "DATABASE_URL": "<api と同じ値>",
    "DATABASE_MIGRATOR_URL": "<api と同じ値>",
    "REDIS_URL": "<api と同じ値>",
    "PUBLIC_BASE_URL": "<api と同じ値>",
    "PUBLIC_WSS_URL": "<api と同じ値>",
    "TWILIO_CALLER_ID": "<api と同じ値>",
}


def report(key: str, spec: dict, required: dict[str, str], print_only: bool) -> int:
    config_path = ROOT / spec["config"]
    name = app_name(config_path)

    print()
    print("=" * 74)
    print(f" {key}: {name or '(fly.toml が見つかりません)'} — {spec['note']}")
    print("=" * 74)

    if name is None:
        print(f"  {spec['config']} がありません")
        return 1

    if "only" in spec:
        wanted = {k: "" for k in spec["only"]}
    else:
        wanted = {
            k: v for k, v in required.items()
            if k not in FROM_FLY_TOML
        }
        for extra in spec.get("extra", []):
            wanted.setdefault(extra, IMPORTANT_OPTIONAL.get(extra, ""))

    existing = None if print_only else fly_secrets(name)

    if existing is None:
        if not print_only:
            print("  ※ fly から取得できませんでした（未ログイン / アプリ未作成 / fly が無い）")
            print("     必要な変数の一覧だけ出します")
        missing = sorted(wanted)
        if "DATABASE_URL" in missing:
            print("  [自動] DATABASE_URL — Postgres をアタッチすると Fly が入れる")
            missing.remove("DATABASE_URL")
    else:
        missing = sorted(k for k in wanted if k not in existing and k not in AUTO_BY_PLATFORM)
        present = sorted(k for k in wanted if k in existing)
        for k in present:
            print(f"  [設定済] {k}")
        for k in sorted(AUTO_BY_PLATFORM & set(wanted)):
            if k not in existing:
                print(f"  [要確認] {k} — Postgres をアタッチすると自動で入る")

    if not missing:
        print("  不足なし")
        return 0

    print()
    for k in missing:
        hint = wanted.get(k) or IMPORTANT_OPTIONAL.get(k, "")
        print(f"  [不足] {k}" + (f" — {hint}" if hint else ""))

    print()
    print("  設定コマンド:")
    print()
    print(f"    fly secrets set -a {name} \\")
    for i, k in enumerate(missing):
        tail = " \\" if i < len(missing) - 1 else ""
        # ★ media は api と同じ値を使う。生成コマンドを再実行させない
        value = SHARED_WITH_API.get(k) if key == "media" else None
        print(f"      {k}={value or PLACEHOLDERS.get(k, '<値>')}{tail}")
    if key == "media":
        print()
        print("    ★ media の値は api と同一にすること。特に JWT_SECRET を")
        print("      生成し直すと、api が発行したトークンを検証できなくなる。")
        print("      確認: fly secrets list -a <api アプリ>")
    return len(missing)


def main() -> int:
    parser = argparse.ArgumentParser(description="デプロイ前の環境変数チェック")
    parser.add_argument(
        "--platform", choices=("fly", "railway"), default="fly", help="確認する PaaS"
    )
    parser.add_argument("--app", help="1 つだけ確認する（api / media / jobs / web）")
    parser.add_argument(
        "--print-only", action="store_true", help="CLI を叩かず必要な変数を並べるだけ"
    )
    args = parser.parse_args()

    required = required_from_config()
    if not required:
        print("config.py から必須変数を読めませんでした", file=sys.stderr)
        return 2

    table = APPS if args.platform == "fly" else RAILWAY_SERVICES
    fn = report if args.platform == "fly" else report_railway

    if args.app and args.app not in table:
        print(
            f"--platform {args.platform} で使えるのは: {', '.join(sorted(table))}",
            file=sys.stderr,
        )
        return 2

    targets = {args.app: table[args.app]} if args.app else table
    total = sum(fn(k, v, required, args.print_only) for k, v in targets.items())

    print()
    print("=" * 74)
    if total == 0:
        print(" 不足はありません。デプロイできます。")
        print()
        print(" ★ マネージド Postgres を使う場合、先に db/bootstrap-roles.sql を")
        print("   1 回流しておくこと。流さないと RLS が効かず、起動時のガードで止まります。")
        print("   Railway の Postgres が配る DATABASE_URL は superuser なので必ず該当します。")
    else:
        print(f" {total} 件の設定が不足しています")
        print()
        print(" ★ PUBLIC_BASE_URL は Twilio Console に登録する URL と 1 文字も")
        print("   違ってはいけません。片方だけ変えると Webhook が全件 403 になります。")
    print("=" * 74)
    return 0 if total == 0 else 1


if __name__ == "__main__":
    sys.exit(main())

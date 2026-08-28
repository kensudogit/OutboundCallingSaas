# 架電特化型SaaS

アウトバウンドコール（インサイドセールス向け発信基盤）の実装。
Python 3.12 + FastAPI / Next.js 15 + React + TypeScript / PostgreSQL 17 / Twilio。

リスト管理 → 発信 → 通話中のリアルタイム文字起こしとサジェスト → 録音 → 結果登録 → KPI まで。

---

## 守っている 5 原則

架電システムは**バグが電話として相手に届く**。二重発信は相手の携帯が 2 回鳴るということで、
DNC チェック漏れは断った相手にもう一度かけるということで、どちらもロールバックできない。
だから次の 5 点は構造で担保していて、プロジェクトの事情で緩めない。

### 1. 発信は必ず 1 つの関門を通す

DNC（再勧誘拒否）・架電可能時間帯・重複・回数上限・予約・番号の妥当性を
[`app/dialer/gate.py`](server/app/dialer/gate.py) の `can_call()` に集約している。
Twilio の SDK を呼ぶのは [`app/dialer/dialer.py`](server/app/dialer/dialer.py) の 1 箇所だけ。

```bash
# 守れているかは grep 1 回で確認できる。1 件（dialer.py）以外が出たら関門を通らない経路
grep -rn "calls.create" server/app --include=*.py
```

判定は bool ではなく理由コードを返す。UI が出し分けられ、`call_attempts_blocked`
に記録されて**「関門が機能している証跡」**にも監視項目にもなる。

### 2. 通話の同一性は Call SID で担保する

1 本の通話に 3 経路が非同期に触る——発信 API のレスポンス、`statusCallback`、Media Stream。
**到着順は保証されない。** `unique (tenant_id, provider_call_sid)` を置いて全経路 upsert にし、
状態は `call_status_rank()` で**進む方向にしか動かさない**。

`completed` が `answered` より先に着いても状態は巻き戻らず、`answered_at` だけが後から埋まる。
通話時間は Twilio が計算した値をそのまま採る（`ended_at - answered_at` の引き算をしない）。

### 3. 音声ストリームを API から分離する

Media Streams は 20ms ごとに音声フレームが来る（1 通話で毎秒 50 メッセージ）。
[`app/realtime/media_app.py`](server/app/realtime/media_app.py) は**別の ASGI アプリ**で、
別ポート・別プロセスで動かす。web ワーカーとは Redis Pub/Sub で繋ぐ。

Next.js は BFF であって音声を通さない。WebRTC も WSS もブラウザから FastAPI へ直接つなぐ。

### 4. テナント分離は PostgreSQL の RLS

アプリの `WHERE tenant_id = ?` に頼らない。全テーブルで `force row level security` +
`with check`、接続ごとに `SET LOCAL app.tenant_id`。
未設定の接続からは**1 行も見えない**（`current_setting(..., true)` が NULL を返す）。

### 5. 録音と文字起こしは個人情報として設計する

保存期間（`recordings.expires_at`）と削除ジョブを最初から持つ。録音の URL は
自社サーバーが仲介し、聴取は監査ログに残す。番号は末尾 4 桁だけ残してログに出す。

---

## 起動

### 1. 依存とサービス

```bash
docker compose up -d
```

PostgreSQL は **5434**、Redis は **6381**（既存環境と衝突させないため）。

```bash
cd server && pip install -e ".[dev]"
cd ../web && npm install
```

### 2. 設定

```bash
cp server/.env.example server/.env
```

架電特有で忘れやすいのは次の 3 つ。

| 変数 | 注意 |
| --- | --- |
| `TWILIO_CALLER_ID` | 購入済みか検証済みの番号でないと発信自体が失敗する |
| `PUBLIC_BASE_URL` | **Twilio に登録した URL と 1 文字も違ってはいけない**（署名検証の入力） |
| `TWILIO_AUTH_TOKEN` | 署名検証に使う。API Key Secret とは別物 |

設定は起動時に**全部まとめて**検証される。1 件ずつ落とすと、コンテナデプロイでは
6 件足りないときに 6 回やり直すことになる。

### 3. Twilio の疎通（コードを書く前に）

```bash
python .claude/skills/outbound-calling-saas/scripts/preflight.py --env-file server/.env
```

開発中は Twilio から `localhost` に届かないのでトンネルを張る。
**この URL が署名検証の入力になる**ので、変わったら `.env` と Twilio Console の
両方を更新する（片方だけだと全件 403 になる）。

```bash
cloudflared tunnel --url http://localhost:8000
```

### 4. DB とデモデータ

```bash
cd server && python -m app.db.migrate --drop --seed
```

デモリストの `+819000000003` は **DNC に登録済み**。キューに出てこないこと、
直接発信しても 403 になることを最初に確認すると、関門が効いている証拠になる。

### 5. 起動

```bash
cd server && uvicorn app.app:app --port 8000 --reload                       # API
cd server && uvicorn app.realtime.media_app:media_app --port 8001 --reload  # 音声（別プロセス）
cd server && python -m app.jobs.maintenance --loop 60                       # 定期ジョブ
cd web && npm run dev                                                       # 画面
```

**定期ジョブは必ず動かす。** 予約の解放（担当者のブラウザが落ちたとき）、
録音の削除、KPI のロールアップがここにある。動かさないとリストが少しずつ枯れる。

---

## 検証

```bash
cd server && python -m pytest
```

50 件。関門の全分岐、署名検証（公式実装との一致を含む）、状態の単調更新、
μ-law 変換と発話区間の検出。**Twilio にも DB にも接続しない**ので CI に載る。

### Twilio なしで通話イベントを流す

実際に電話をかけると検証にならない（相手が必要で、繰り返せず、課金される）。
**Twilio 側のイベントを自分で作って投げる**のが基本戦略。

```bash
python .claude/skills/outbound-calling-saas/scripts/simulate_call.py --scenario answered
python .claude/skills/outbound-calling-saas/scripts/simulate_call.py --scenario out-of-order
python .claude/skills/outbound-calling-saas/scripts/simulate_call.py --scenario duplicate
```

`out-of-order`（completed が answered より先に届く）と `duplicate`（completed が 3 回届く）
は必ず通す。**実運用で普通に起きる**ので、ここが通らない実装は本番で必ず壊れる。

### 署名が合わないとき

```bash
python .claude/skills/outbound-calling-saas/scripts/verify_twilio_signature.py \
  --url "https://xxxx.trycloudflare.com/voice/status?call_id=abc" \
  --signature "<X-Twilio-Signature>" --params-file ./captured-params.json
```

URL の揺れ（末尾スラッシュ・プロトコル・クエリ）を総当たりして、一致する形を教える。

---

## 症状から引く

| 症状 | 見るところ |
| --- | --- |
| Webhook が全部 403 | `PUBLIC_BASE_URL` と Twilio Console の URL。[`signature.py`](server/app/telephony/signature.py) |
| 同じ通話が 2 行に増える | `provider_call_sid` の unique と upsert。[`repositories/calls.py`](server/app/repositories/calls.py) |
| 同じ相手に 2 回かかる | 予約の部分ユニークインデックス。発信トリガーが completed になっていないか |
| 通話時間が負・状態が巻き戻る | `call_status_rank()` による単調更新 |
| API 全体が通話中だけ遅い | media ワーカーを分けて起動しているか（原則 3） |
| 他テナントのデータが見えた | `SET LOCAL` の抜け。`tenant_tx()` を通しているか |
| リストが少しずつ枯れる | 定期ジョブが動いているか（予約の解放） |
| 断った相手に再架電した | 関門を通らない発信経路。上記の grep |

---

## 未実装（意図的に残している部分）

- **録音の自社ストレージへのコピー** — `app/storage.py` に口はあるが、Twilio からの
  ダウンロードとコピーのジョブは未実装。聴取 API は 501 を返し、**Twilio の URL を
  素通しさせない**ようにしてある
- **通話後の全文文字起こしと要約** — 録音の記録までは動く。バッチ処理は未実装
- **会話メトリクスの計算** — テーブルと KPI の口はあるが、区間の集計は未実装
- **プレディクティブダイヤル** — 放棄呼（相手が出たのに担当者が空いていない）が
  発生し、相手から見れば無言電話になる。導入するなら放棄呼率の監視と上限制御が
  別途必要で、設計の性質が変わるため入れていない

## 法令について

`references/compliance.md`（skill 側）に実装上の要件を整理してある。
**法的助言ではない。** 適用の可否と運用基準は事業内容と商材で変わるので、
自社の法務または弁護士に確認すること。

このプロジェクトが構造として担保しているのは次の 3 点。

- DNC の照合を**設定で無効にできない**（そういう列を作っていない）
- 拒否系の結果コードを選ぶと**自動で DNC に登録**される（担当者の追加操作を挟まない）
- `dnc_entries` と `audit_logs` はアプリユーザーから UPDATE / DELETE を落としてある
"# OutboundCallingSaas" 

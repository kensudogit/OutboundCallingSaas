# 架電特化型SaaS

アウトバウンドコール（インサイドセールス向け発信基盤）の実装。
Python 3.12+ + FastAPI / Next.js 16 + React 19 + TypeScript / PostgreSQL 17 / Twilio。

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
未設定の接続からは**1 行も見えない**。

判定は `current_tenant_id()` に閉じ込めてある。`nullif` を挟んでいるのが要で、
`SET LOCAL` を抜けた後の設定値は NULL ではなく**空文字**になり、素の
`current_setting(...)::uuid` は `''::uuid` で例外を投げる。データは漏れないが
500 エラーになり、接続を使い回したかどうかで挙動が変わる。プールを使う以上必ず踏む。

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
cd server && python -m alembic upgrade head
cd server && python -m app.db.seed
```

スキーマは **Alembic** で管理する。`migrations/versions/` がスキーマの唯一の出所で、
`schema.sql` は持たない（二重管理は Alembic を入れる意味を失わせる）。

| 操作 | コマンド |
| --- | --- |
| 最新まで適用 | `python -m alembic upgrade head` |
| 現在のリビジョン | `python -m alembic current` |
| 新しいリビジョン | `python -m alembic revision -m "説明"` |
| 適用予定の SQL を見る | `python -m alembic upgrade head --sql` |

**autogenerate は使えない。** SQLAlchemy のモデルを持たず DDL を生の SQL で
書いているため（RLS ポリシー・部分ユニークインデックス・不変関数は
SQLAlchemy のスキーマ表現で書けないものが多い）。リビジョンは手で書く。

**接続は必ず migrator ロール**（`DATABASE_MIGRATOR_URL`）で行う。アプリの
ロールで流すと RLS に阻まれて中途半端に失敗する。`env.py` が未設定なら落とす。

**RLS を有効にするテーブルを足したら、同じリビジョンでポリシーも作る。**
テーブルだけ先に入ると、その間だけ他テナントから見える。
`test_全てのアプリテーブルでRLSが有効` がこれを検出する。

デモリストの `+819000000003` は **DNC に登録済み**。キューに出てこないこと、
直接発信しても 403 になることを最初に確認すると、関門が効いている証拠になる。

なお、デモの連絡先は実在しない番号帯なので、**管理画面の取り込みに同じ番号を
貼ると弾かれる**（`phonenumbers` が番号の妥当性まで見るため）。想定どおりの挙動。

### 5. 起動

```bash
cd server && uvicorn app.app:app --port 8000 --reload                       # API
cd server && uvicorn app.realtime.media_app:media_app --port 8001 --reload  # 音声（別プロセス）
cd server && python -m app.jobs.maintenance --loop 60                       # 定期ジョブ
cd web && npm run dev                                                       # 画面
```

**定期ジョブは必ず動かす。** 1 巡で次を順に行う。順序に意味がある。

| 順 | 処理 | 動かさないとどうなるか |
| --- | --- | --- |
| 1 | 予約の解放 | 担当者のブラウザが落ちるたびにリストが 1 件ずつ枯れる |
| 2 | 録音を Twilio から自社ストレージへコピー | 聴取 API が 409 のまま。保存期間の設定も効かない |
| 3 | 全文文字起こし・要約・会話メトリクス | 通話後の記録が残らない |
| 4 | KPI のロールアップ | ダッシュボードが重くなる |
| 5 | 保存期間切れの録音を削除 | 消せない量になってから考えることになる |

2 を 3 より先に置いているのは、コピーが済んでいない録音は文字起こしの対象に
ならないため（逆順だと 1 巡遅れる）。5 を最後に置くのは、同じ巡で入った録音を
すぐ消さないため。個別に動かすこともできる。

```bash
python -m app.jobs.recordings   # コピーのみ
python -m app.jobs.transcribe   # 文字起こしのみ
```

### 録音の保管

`RECORDING_STORAGE` で切り替える。

| 値 | 用途 |
| --- | --- |
| `local`（既定） | 開発・検証。`RECORDING_LOCAL_DIR` に置く |
| `s3` | 本番。S3 互換ストレージ |

**ローカルでも署名付き URL の形にしてある。** ファイルパスをそのまま返すと
S3 構成との挙動が変わり、アクセス制御が「本番でだけ守られている」状態になる。
ローカルで期限切れ・署名改竄を検証できるようにしてある。

聴取 API は、コピーが済むまで **409 を返す**。Twilio の URL を素通しすると、
自社のアクセス制御を通らない URL が外に出ることになる。

---

## 検証

```bash
cd server && python -m pytest
```

172 件。うち 118 件は **Twilio にも DB にも接続しない**（関門の全分岐、署名検証、
状態の単調更新、μ-law 変換と発話区間の検出、Webhook ルートの署名拒否、
会話メトリクスの境界値、WAV のチャンネル分離、録音の署名付き URL）。

残り 54 件は実 DB に対する統合検証で、**コードではなく DB が守っているか**と、
**ジョブを繋いだときに壊れないか**を見る。
`docker compose up -d` していなければ自動でスキップされる。

| 検証 | 落ちたら何が壊れているか |
| --- | --- |
| テナント未設定で 0 行 | RLS が効いていない（他社データが見える） |
| 他テナント ID の INSERT が失敗 | `with check` の書き忘れ |
| トランザクションを抜けたら 0 行 | `SET`/`SET LOCAL` の取り違え、`nullif` 漏れ |
| 2 接続が別々の相手を取る | `SKIP LOCKED` が効かず二重発信 |
| スキップした相手が戻らない | 担当者が前に進めない |
| completed が先でも巻き戻らない | 通話時間の集計が壊れる |
| DNC を app_user が消せない | 断った相手への再架電 |
| 2 回実行しても文字起こしが増えない | ジョブの再実行で記録が二重になる |
| コピー前に Twilio 側を消さない | コピー失敗で録音を完全に失う |
| 扱えない録音を無限に拾い直さない | ジョブが同じ録音で詰まり続ける |
| 保存期間切れで実体ごと消える | 消せないデータが溜まり続ける |

```bash
cd web && npm test
```

フロント 71 件（jsdom + Testing Library）。目視では検出しにくい点を押さえる。

| 検証 | なぜ目視で気付けないか |
| --- | --- |
| StrictMode で Device が 1 つ | 本番ビルドでは再現しない |
| 発信ボタンの連打で 1 回だけ発信 | 速く押さないと再現しない |
| 暫定が確定で置き換わり行が増えない | 一瞬なので見逃す |
| 文字起こしが止まったら画面に出す | 黙ると「誰も喋っていない」と誤解する |
| マイク拒否とデバイス無しを区別 | 対処が違うのに同じに見える |
| 確認を通すまで取り込めない | 押せてしまうと数万件が入る |
| 内容を変えたら確認をやり直す | 確認した内容と違うものが入る |

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

## デプロイ

イメージは 2 つ。**サーバーは 1 イメージで 3 つのプロセスを動かす。**

```bash
docker build -t calling-server .                        # サーバー
docker build -f web/Dockerfile -t calling-web ./web     # フロント
```

| コマンド | 役割 | 備考 |
| --- | --- | --- |
| `api` | FastAPI（既定） | ポート `PORT`（既定 8000） |
| `media` | 音声ワーカー | **必ず別サービス**。ポート `MEDIA_PORT`（既定 8001） |
| `jobs` | 定期ジョブ | 予約の解放・録音のコピーと削除・集計 |
| `migrate` | スキーマ適用 | **リリース時に一度だけ** |

イメージを分けないのは、依存もコードも同じで、分けると「片方だけ古い」が起きるため。
プロセスの分離はデプロイ側のサービス定義で行う。

**`media` を `api` と同じサービスにしない**（原則 3）。Media Streams は 1 通話あたり
毎秒 50 メッセージで、同じイベントループに載せると通話が増えるほど API が遅くなる。
スケールの軸も違う（API は同時ユーザー数、media は同時通話数）。

**`migrate` をコンテナ起動時に流さない。** 複数インスタンスが同時に上がると
同じマイグレーションを並行実行することになる。リリースコマンドとして 1 回だけ実行する。

一式を動かして確かめる場合:

```bash
PUBLIC_BASE_URL=https://xxxx.trycloudflare.com PUBLIC_WSS_URL=wss://xxxx.trycloudflare.com JWT_SECRET=$(openssl rand -hex 32) TWILIO_ACCOUNT_SID=AC... TWILIO_AUTH_TOKEN=... TWILIO_CALLER_ID=+81... docker compose -f docker-compose.prod.yml up --build
```

### コンテナ化で踏んだ点

| 症状 | 原因 |
| --- | --- |
| ビルドが Dockerfile を見つけられない | リポジトリのルートに Dockerfile が無かった |
| `pip install .` が失敗する | `[tool.setuptools] packages` の未指定。app / tests / migrations が並んでおり自動検出できない |
| 依存の解決に失敗する | `pyproject.toml` のピンが実在しないバージョンだった |
| フロントだけ unhealthy になる | Next.js standalone は `HOSTNAME` を bind アドレスに使い、Docker がそこにコンテナ ID を入れる。外部公開は効くのでヘルスチェックだけ落ちる |
| entrypoint が `no such file or directory` | 改行が CRLF。`.gitattributes` で LF に固定してある |

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

## 管理画面

`/admin`（manager / admin 権限のみ）。サーバー側で権限を見ているので、
画面の出し分けは導線を隠すだけ。

| タブ | できること |
| --- | --- |
| 設定 | 事業者名・架電時間帯・曜日・祝日・回数上限・録音の保存期間 |
| DNC | 一括取り込み、登録済みの一覧 |
| リスト | リスト作成、CSV での連絡先取り込み |
| 監査ログ | 操作履歴、**関門で止まった件数**（直近 7 日） |

設計上の要点が 4 つある。

**取り込みは「確認」を先に通させる。** 数万件を入れる前に何件弾かれるかを
見せないと、取り込んでから気付くことになる。弾かれた行は**行番号と理由**を
出す——どこを直せばよいか分からないと取り込みが終わらない。

**連絡先は 1 件でも不正なら 1 件も入れない。** 部分的に入ると、再取り込みで
重複するか、どこから再開するか分からなくなる。一方 **DNC は 1 件不正でも
他を入れる**——入れ過ぎても害がない側なので、全部止めるほうが危険。

**DNC をリストより先に取り込ませる。** 順序を逆にすると、その間の架電が
拒否済みの相手に届く。画面にもその順序を書いてある。DNC を取り込むと、
既存リストの該当行は自動で `ARCHIVED` になる。

**「変更できない項目」を理由付きで見せる。** DNC の照合を無効にする設定は
作っていない。設定項目が無いことに気付かず探し回るより、なぜ無いかを
書いておくほうが早い。

## 会話の定量化

デュアルチャンネル録音（左=担当者 / 右=相手）と文字起こしから、
`GET /api/calls/{id}/summary` で返す。

| 指標 | 何のためか |
| --- | --- |
| トーク比率 | 40–50% が目安。70% を超えると一方的に喋っている |
| 最長連続発話 | 90 秒を超える独白は「相手を置き去りにした説明」の代理指標 |
| 被り | 相手の話を遮った回数の代理指標 |
| 相手の初回発話までの時間 | 応答直後の沈黙は不審に思われている合図 |

**モノラル録音では計算しない。** 話者が分からないのに数字を出すと、
それらしく見えて誤解を招く。不確かな数字を出すより出さないほうがよい。

同じデータでも見せ方で用途が変わる。トーク比率を本人に見せるのが育成、
並べて管理者に見せるのが評価。この API は自分の通話か管理者のみが引ける。

## 未実装（意図的に残している部分）

- **プレディクティブダイヤル** — 放棄呼（相手が出たのに担当者が空いていない）が
  発生し、相手から見れば無言電話になる。導入するなら放棄呼率の監視と上限制御が
  別途必要で、設計の性質が変わるため入れていない
- **担当者の追加・削除** — API と DB には口があるが画面が無い
- **KPI ダッシュボードの画面** — API（`/api/stats/*`）はあるが画面が無い

## 法令について

`references/compliance.md`（skill 側）に実装上の要件を整理してある。
**法的助言ではない。** 適用の可否と運用基準は事業内容と商材で変わるので、
自社の法務または弁護士に確認すること。

このプロジェクトが構造として担保しているのは次の 3 点。

- DNC の照合を**設定で無効にできない**（そういう列を作っていない）
- 拒否系の結果コードを選ぶと**自動で DNC に登録**される（担当者の追加操作を挟まない）
- `dnc_entries` と `audit_logs` はアプリユーザーから UPDATE / DELETE を落としてある
"# OutboundCallingSaas" 

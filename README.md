# tgbot_voxcpm

Telegram Bot：用 [VoxCPM](https://voxcpm.modelbest.cn) 官方 API 做聲音克隆與語音合成。

先傳一段參考語音，再輸入要唸的文字，Bot 會回傳合成音檔（M4A）。沒有參考音時，也可以用風格描述直接設計聲音。參考音色、逐字稿與進階參數會記住，重開 Bot 後仍可繼續使用。

## 三種模式

在 Telegram 按下方按鈕直接選擇其中一種（✓ 代表目前使用中）：

| 模式 | 怎麼用 |
| --- | --- |
| 聲音設計 | 不使用參考音，用風格描述（例如「年輕女性，溫柔甜美」） |
| 可控克隆 | 上傳參考音，可再加風格描述 |
| 極致克隆 | 上傳參考音並提供逐字稿，盡量還原原聲音細節 |

## 使用方式

1. 向 Bot 傳送語音訊息或音檔作為參考音色（可跳過）
2. 輸入要唸的文字
3. Bot 回傳合成語音

| 指令 | 說明 |
| --- | --- |
| `/start` | 說明用法 |
| `/mode 聲音設計` | 選擇生成方式（聲音設計／可控克隆／極致克隆） |
| `/style 年輕女性，溫柔甜美` | 設定說話風格 |
| `/clear` | 清除已記住的聲音、風格與逐字稿 |
| `/status` | 查看目前設定 |
| `/transcript` | 修改參考音逐字稿 |
| `/denoise` | 開關參考音降噪 |
| `/cfg 2.0` | CFG 引導強度（1.0–3.0） |
| `/steps 30` | LocDiT 步數（1–50） |
| `/cancel` | 取消進行中的任務 |

官方示範站可能排隊或限流，生成需數十秒屬正常。

## 準備

1. 在 Telegram 找 [@BotFather](https://t.me/BotFather) 建立 Bot，取得 token
2. 複製環境變數並填入 token：

```bash
cp .env.example .env
```

Windows PowerShell：

```powershell
Copy-Item .env.example .env
```

`.env` 至少需要：

```env
TELEGRAM_BOT_TOKEN=你的token
```

## Docker 部署（建議）

需要 [Docker Desktop](https://www.docker.com/products/docker-desktop/)。

本機建置並啟動：

```bash
docker compose up -d --build
docker compose logs -f
```

或直接使用 GitHub Container Registry 映像（推送到 `main` 後會自動建置）：

```bash
docker pull ghcr.io/wongkino/tgbot_voxcpm:latest
```

`docker-compose.yml` 可用環境變數指定映像：

```bash
DOCKER_IMAGE=ghcr.io/wongkino/tgbot_voxcpm:latest docker compose up -d
```

容器用 long polling，不必對外開 port，只要能連 Telegram 與 `voxcpm.modelbest.cn`。

停止：

```bash
docker compose down
```

## 版本與自動發佈

每次 commit 並 push 到 `main`，GitHub Actions 會：

1. 依上一個 git tag 自動打新版本 `x.y.z`（預設加 patch，例如 `0.1.0` → `0.1.1`）
2. 建置 Docker image
3. 推到 `ghcr.io/wongkino/tgbot_voxcpm:<版本>` 與 `:latest`

在 commit message 裡可指定：

| 寫法 | 結果 |
| --- | --- |
| （預設） | patch：`0.1.0` → `0.1.1` |
| `#minor` | minor：`0.1.5` → `0.2.0` |
| `#major` | major：`0.2.3` → `1.0.0` |
| `#skip-version` | 不打新版本、不發佈映像 |

## 本機直接跑

需要 Python 3.10+（Docker 映像為 3.14）。

```bash
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
python bot.py
```

## 注意事項

- Token 只放在 `.env`，不要提交到 git
- 參考音與生成檔會寫入 `./data`，請勿提交
- 參考音色與設定會存成 `data/<使用者>/state.json`，重開後仍會載入
- 此服務依賴公開示範站，不保證穩定或可用性

# LLM Proxy Bridge

Локальная прослойка (**bridge.py**) между **Claude Code** и **CLIProxyAPI**.

```
Claude Code  →  bridge.py (:4000)  →  CLIProxyAPI (:8317)  →  Antigravity / провайдеры
```

| Компонент | Где берётся | Роль |
|-----------|-------------|------|
| **CLIProxyAPI** | Ставится **отдельно** (не входит в этот репозиторий) | Upstream-прокси, порт **8317** |
| **bridge.py** | Этот репозиторий | Политика моделей, ретраи, cooldown, квоты; порт **4000** |
| **Claude Code** | npm | Клиент — ходит **на bridge** (`:4000`) |
| **Quota Inspector** | Опционально, отдельно | Снимок квот CPA для логов bridge |

Bridge не заменяет CLIProxyAPI: без запущенного CPA bridge ходить некуда.  
Обратно CPA работает и **без** bridge — но тогда не будет rewrite / fallback / cooldown из `bridge.py`.

---

## Что в этом репозитории

| Путь | Назначение |
|------|------------|
| `bridge.py` | FastAPI reverse-proxy |
| `check_models.py` | Smoke-тест моделей через bridge |
| `.env.example` | Шаблон конфига → `copy .env.example .env` |

Бинарники CLIProxyAPI и Quota Inspector **не коммитятся** — ставятся с их релизов.

---

## Что нужно

- Windows 10/11 (инструкция под PowerShell; bridge — обычный Python)
- **CLIProxyAPI** — [релизы](https://github.com/router-for-me/CLIProxyAPI/releases)
- **Google-аккаунт** — логин Antigravity
- **Node.js** LTS — Claude Code
- **Python 3.11+** — bridge

---

## Установка

### 1. Node.js и Claude Code

1. LTS: https://nodejs.org/ → `node --version`
2. Claude Code:

```powershell
npm install -g @anthropic-ai/claude-code
```

### 2. CLIProxyAPI (отдельно от репозитория)

1. Скачайте zip для Windows с [релизов CLIProxyAPI](https://github.com/router-for-me/CLIProxyAPI/releases)  
   (обычно `CLIProxyAPI_*_windows_amd64.zip`).
2. Распакуйте **куда удобно**, например:

```text
C:\tools\CLIProxyAPI\
```

Имя папки и версия могут отличаться — дальше в командах подставьте **свой** путь.

3. В папке CPA:

   - `config.example.yaml` → переименовать в `config.yaml` (если ещё нет).
   - В `config.yaml` **закомментируйте** `api-keys:` (иначе «invalid API key» на примерах):

```yaml
# api-keys:
#   - "your-api-key-1"
```

Порт по умолчанию: **8317** (`port:` в `config.yaml`).

Документация upstream: https://help.router-for.me/ · https://github.com/router-for-me/CLIProxyAPI

### 3. Авторизация Antigravity

Из **папки, куда распаковали CPA** (имя exe может быть `cli-proxy-api.exe`):

```powershell
cd C:\tools\CLIProxyAPI   # ваш путь
.\cli-proxy-api.exe --antigravity-login
```

Браузер → Google-аккаунт → после успеха процесс можно закрыть.

Если браузер не открылся: `--no-browser` и откройте URL вручную.

Перелогин при **Unauthorized**:

```powershell
.\cli-proxy-api.exe --antigravity-login
```

### 4. Этот репозиторий (bridge)

```powershell
cd <корень-этого-репозитория>
pip install fastapi uvicorn httpx rich python-dotenv
copy .env.example .env
```

В `.env` укажите URL **уже установленного** CPA (если не default):

```env
CPA_BASE_URL=http://127.0.0.1:8317
UPSTREAM_API_KEY=sk-antigravity
```

Для квот (опционально) — management key из `remote-management` в `config.yaml` CPA:

```env
CPA_MANAGEMENT_KEY=your-management-key
```

И путь к inspector, если он не лежит рядом с репо (см. [Quota Inspector](#quota-inspector-опционально)):

```env
QUOTA_INSPECTOR_PATH=C:\tools\cpa-quota-inspector.exe
```

`.env` в `.gitignore` — секреты не коммитить.

---

## Ежедневный запуск

Три независимых процесса:

### 1. CLIProxyAPI

```powershell
cd C:\tools\CLIProxyAPI   # ваш путь к CPA
.\cli-proxy-api.exe
```

Слушает **8317**. Окно не закрывать.

```powershell
Invoke-WebRequest -Uri "http://127.0.0.1:8317/v1/models"
```

### 2. bridge (из этого репозитория)

```powershell
cd <корень-этого-репозитория>
python bridge.py
```

Слушает `BRIDGE_HOST:BRIDGE_PORT` (по умолчанию `127.0.0.1:4000`).

```powershell
curl http://127.0.0.1:4000/health
python check_models.py
```

### 3. Claude Code → bridge

```powershell
$env:ANTHROPIC_BASE_URL = "http://127.0.0.1:4000"
$env:ANTHROPIC_API_KEY  = "sk-antigravity"
$env:ANTHROPIC_MODEL    = "claude-opus-4-6-thinking"
claude
```

`ANTHROPIC_BASE_URL` — **bridge (:4000)**, не CPA (:8317).

#### BAT (Claude Code)

`claude-opus.bat`:

```batch
@echo off
set ANTHROPIC_BASE_URL=http://127.0.0.1:4000
set ANTHROPIC_API_KEY=sk-antigravity
set ANTHROPIC_MODEL=claude-opus-4-6-thinking
claude
```

`claude-sonnet.bat`:

```batch
@echo off
set ANTHROPIC_BASE_URL=http://127.0.0.1:4000
set ANTHROPIC_API_KEY=sk-antigravity
set ANTHROPIC_MODEL=claude-sonnet-4-6
claude
```

---

## Политика моделей (bridge)

| Запрос клиента | Поведение |
|----------------|-----------|
| Haiku / id с `haiku` | → `HAIKU_TARGET_MODEL` (default `gemini-3.1-pro-low`) |
| Opus / Sonnet / heavy-алиасы | `CLAUDE_HEAVY_MODELS`: Opus → Sonnet (без Gemini) |
| Остальное | Passthrough |

Ретраи: `SAME_MODEL_RETRIES + 1` на модель. Cooldown — `COOLDOWN_*` в `.env`.

---

## Переменные `.env`

Шаблон — `.env.example`. Главное:

| Переменная | По умолчанию | Описание |
|------------|--------------|----------|
| `CPA_BASE_URL` | `http://127.0.0.1:8317` | URL **внешнего** CLIProxyAPI |
| `UPSTREAM_API_KEY` | `sk-antigravity` | `x-api-key` к CPA |
| `BRIDGE_HOST` / `BRIDGE_PORT` | `127.0.0.1` / `4000` | Listen bridge |
| `CPA_MANAGEMENT_KEY` | — | Management key CPA (квоты) |
| `QUOTA_INSPECTOR_PATH` | рядом с репо `…/cpa-quota-inspector.exe` | Полный путь к binary, если inspector не в репо |
| `QUOTA_POLL_INTERVAL` | `300` | Опрос квот, сек |
| `CLAUDE_HEAVY_MODELS` | opus…, sonnet… | Fallback heavy |
| `HAIKU_TARGET_MODEL` | `gemini-3.1-pro-low` | Куда уходит haiku |
| `SAME_MODEL_RETRIES` | `4` | Доп. попытки на модель |
| `COOLDOWN_*` | 60 / 1800 / 30 / 300 | TTL cooldown, сек |

---

## HTTP API bridge

| Метод | Путь | Описание |
|-------|------|----------|
| `GET` | `/health` | Статус, токены, cooldown, snapshot квот |
| `POST` | `/v1/messages` | Anthropic Messages (stream / non-stream) |

---

## Quota Inspector (опционально)

Не обязателен для работы bridge.

1. Возьмите binary с [релизов CLIProxyAPI-Quota-Inspector](https://github.com/AllenReder/CLIProxyAPI-Quota-Inspector/releases) или соберите из исходников: [github.com/AllenReder/CLIProxyAPI-Quota-Inspector](https://github.com/AllenReder/CLIProxyAPI-Quota-Inspector) (`go build -o cpa-quota-inspector.exe .`).
2. Положите куда угодно и укажите в `.env`:

```env
QUOTA_INSPECTOR_PATH=C:\tools\cpa-quota-inspector.exe
CPA_MANAGEMENT_KEY=your-management-key
```

Если `QUOTA_INSPECTOR_PATH` не задан, bridge ищет  
`<корень-репо>/CLIProxyAPI-Quota-Inspector/cpa-quota-inspector.exe` — удобно, только если вы сами положили exe туда локально.

Нет binary или ключа — warning в логе, bridge без квот.  
Снимок: логи и `GET /health` → `quota`.

---

## Автозапуск CPA (опционально)

1. `Win + R` → `shell:startup`
2. Ярлык на **ваш** `cli-proxy-api.exe`

Bridge и Claude Code — вручную или своим скриптом.

---

## Если что-то не работает

| Симптом | Что сделать |
|---------|-------------|
| **Invalid API key** | В `config.yaml` CPA закомментировать `api-keys:`; перезапустить CPA |
| **Connection refused** на 8317 | CPA не запущен / другой `port` / неверный `CPA_BASE_URL` |
| **Connection refused** на 4000 | Не запущен `python bridge.py` |
| Claude Code без политики bridge | `ANTHROPIC_BASE_URL=http://127.0.0.1:4000` |
| **Unauthorized** | Из папки CPA: `.\cli-proxy-api.exe --antigravity-login` |
| **Rate limit / quota** | Подождать; bridge ретраит; добавить аккаунты в CPA |
| Quota inspector warning | `QUOTA_INSPECTOR_PATH` + `CPA_MANAGEMENT_KEY` в `.env` |
| Брандмауэр | Разрешить CPA / Python на localhost |
| Проверка CPA | `Invoke-WebRequest http://127.0.0.1:8317/v1/models` |
| Проверка bridge | `curl http://127.0.0.1:4000/health` |

---

## Ссылки

- [CLIProxyAPI](https://github.com/router-for-me/CLIProxyAPI) · [релизы](https://github.com/router-for-me/CLIProxyAPI/releases)
- [Документация](https://help.router-for.me/)
- [CLIProxyAPI Quota Inspector](https://github.com/AllenReder/CLIProxyAPI-Quota-Inspector) · [релизы](https://github.com/AllenReder/CLIProxyAPI-Quota-Inspector/releases)

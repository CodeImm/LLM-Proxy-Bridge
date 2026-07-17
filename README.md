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
| **Quota Inspector** | Опционально: `git clone` + `go build` | Снимок квот CPA для логов bridge |

Bridge не заменяет CLIProxyAPI: без запущенного CPA bridge ходить некуда.  
Обратно CPA работает и **без** bridge — но тогда не будет rewrite / fallback / cooldown из `bridge.py`.

---

## Что в этом репозитории

| Путь | Назначение |
|------|------------|
| `bridge.py` | FastAPI reverse-proxy |
| `check_models.py` | Smoke-тест моделей через bridge |
| `opus.bat` | Быстрый запуск Claude Code → bridge (Opus) |
| `.env.example` | Шаблон конфига → `copy .env.example .env` |

CLIProxyAPI и Quota Inspector **не входят** в этот репозиторий: CPA — с [релизов](https://github.com/router-for-me/CLIProxyAPI/releases), inspector — через `git clone` (см. ниже).

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

   - Включите **Management API** (нужен для Quota Inspector).  
     Пустой `secret-key` = Management API **выключен** → все `/v0/management/*` отвечают **404**, даже если `/v1/models` = 200:

```yaml
remote-management:
  allow-remote: false
  secret-key: "your-management-key"   # plaintext; CPA при старте захэширует — это нормально
```

Тот же ключ потом пропишите в `.env` bridge как `CPA_MANAGEMENT_KEY` (см. ниже).  
Без inspector / квот секцию можно оставить с пустым `secret-key`.

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

Для квот (опционально) — **тот же** ключ, что `remote-management.secret-key` в `config.yaml` CPA:

```env
CPA_MANAGEMENT_KEY=your-management-key
```

Ключ только в `.env` недостаточно: если в CPA `secret-key: ""`, Management API выключен и inspector получит **404**.  
Подробнее: [Management API и ключ](#management-api-и-ключ-для-квот).

Путь к собранному inspector (см. [Quota Inspector](#quota-inspector-опционально)):

```env
QUOTA_INSPECTOR_PATH=C:\tools\CLIProxyAPI-Quota-Inspector\cpa-quota-inspector.exe
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

`ANTHROPIC_BASE_URL` — **bridge (:4000)**, не CPA (:8317).

#### Через `opus.bat` (в корне репозитория)

После запуска CPA и bridge:

```powershell
cd <корень-этого-репозитория>
.\opus.bat
```

Скрипт задаёт:

```batch
@echo off
set ANTHROPIC_BASE_URL=http://localhost:4000
set ANTHROPIC_API_KEY=sk-any-dummy
set ANTHROPIC_MODEL=claude-opus-4-6-thinking
set CLAUDE_CODE_DISABLE_PARALLEL_TOOLS=1
claude
```

| Переменная | Значение | Зачем |
|------------|----------|--------|
| `ANTHROPIC_BASE_URL` | `http://localhost:4000` | Claude Code → bridge |
| `ANTHROPIC_API_KEY` | dummy | CC требует непустой ключ; auth делает CPA |
| `ANTHROPIC_MODEL` | `claude-opus-4-6-thinking` | heavy-цепочка Opus → Sonnet |
| `CLAUDE_CODE_DISABLE_PARALLEL_TOOLS` | `1` | без параллельных tool-call |

Можно править `opus.bat` под себя (модель, URL, если сменили `BRIDGE_PORT`).

#### Вручную (PowerShell)

```powershell
$env:ANTHROPIC_BASE_URL = "http://127.0.0.1:4000"
$env:ANTHROPIC_API_KEY  = "sk-antigravity"
$env:ANTHROPIC_MODEL    = "claude-opus-4-6-thinking"
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
| `CPA_MANAGEMENT_KEY` | — | Тот же plaintext, что `remote-management.secret-key` в CPA (квоты) |
| `QUOTA_INSPECTOR_PATH` | `…/CLIProxyAPI-Quota-Inspector/cpa-quota-inspector.exe` | Путь к `.exe` после `git clone` + `go build` |
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

Не обязателен для работы bridge. Нужны **Git** и **Go**.

### Установка

Клонируйте репозиторий (отдельно от bridge) и соберите binary:

```powershell
# куда удобно, например C:\tools
cd C:\tools

git clone https://github.com/AllenReder/CLIProxyAPI-Quota-Inspector.git
cd CLIProxyAPI-Quota-Inspector

# go build только из этой папки (где go.mod), не из корня bridge
go build -o cpa-quota-inspector.exe .
```

Без `cd` в каталог клона Go ответит `cannot find main module` — module живёт только там.

Репозиторий: https://github.com/AllenReder/CLIProxyAPI-Quota-Inspector

### Подключение к bridge

В `.env` bridge укажите путь к собранному `.exe` и management key:

```env
QUOTA_INSPECTOR_PATH=C:\tools\CLIProxyAPI-Quota-Inspector\cpa-quota-inspector.exe
CPA_MANAGEMENT_KEY=your-management-key
```

Если `QUOTA_INSPECTOR_PATH` не задан, bridge ищет  
`<корень-bridge>/CLIProxyAPI-Quota-Inspector/cpa-quota-inspector.exe`  
(удобно, если клон лежит рядом с bridge и вы уже сделали `go build` внутри него).

Нет binary, ключа или Management API в CPA — warning в логе, bridge без квот.  
Снимок: логи и `GET /health` → `quota`.

### Management API и ключ для квот

Inspector ходит в **Management API** CPA (`/v0/management/*`), не в обычный `/v1/...`.

| Endpoint | Если CPA «просто запущен» |
|----------|---------------------------|
| `/v1/models` | обычно **200** |
| `/v0/management/*` | **404**, пока Management API выключен |

В `config.yaml` CPA:

```yaml
remote-management:
  # secret-key: ""          # пусто = Management API отключён → 404 на все /v0/management
  allow-remote: false
  secret-key: "your-management-key"   # тот же, что CPA_MANAGEMENT_KEY в .env bridge
```

**Шаги:**

1. Задайте `secret-key` в `config.yaml` CPA и **тот же** plaintext в `.env` bridge:

```env
CPA_MANAGEMENT_KEY=your-management-key
```

2. **Перезапустите CLIProxyAPI** (остановить процесс → снова `.\cli-proxy-api.exe`).  
   При старте CPA захэширует plaintext `secret-key` (в файле появится hash вида `$2a$...`) — это нормально.

3. Проверка Management API (не 404; **200** = ок, **401** = management включён, но ключ неверный):

```powershell
Invoke-WebRequest -Uri "http://127.0.0.1:8317/v0/management/auth-files" `
  -Headers @{ Authorization = "Bearer your-management-key" }
```

4. Перезапустите bridge — warning про inspector / 404 должен пропасть.

| Ответ | Смысл |
|-------|--------|
| **404** | `secret-key` пустой / Management API выключен |
| **401** | Management включён, но ключ в `.env` ≠ `secret-key` в CPA |
| **200** + JSON | всё ок |

Ключ в `.env` (`CPA_MANAGEMENT_KEY`) и `secret-key` в `config.yaml` CPA должны совпадать.

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
| Quota inspector → **404** | В CPA `secret-key: ""` — Management API выключен; задайте `secret-key`, перезапустите CPA |
| Quota inspector → **401** | Management включён, но `CPA_MANAGEMENT_KEY` ≠ `secret-key` |
| Quota inspector warning | `git clone` + `go build`? `QUOTA_INSPECTOR_PATH`? Ключ в CPA **и** в `.env`? |
| Брандмауэр | Разрешить CPA / Python на localhost |
| Проверка CPA (API) | `Invoke-WebRequest http://127.0.0.1:8317/v1/models` |
| Проверка Management API | Bearer-запрос к `/v0/management/auth-files` (см. выше) |
| Проверка bridge | `curl http://127.0.0.1:4000/health` |

---

## Ссылки

- [CLIProxyAPI](https://github.com/router-for-me/CLIProxyAPI) · [релизы](https://github.com/router-for-me/CLIProxyAPI/releases)
- [Документация](https://help.router-for.me/)
- [CLIProxyAPI Quota Inspector](https://github.com/AllenReder/CLIProxyAPI-Quota-Inspector) — `git clone https://github.com/AllenReder/CLIProxyAPI-Quota-Inspector.git`

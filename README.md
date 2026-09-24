# LLM Proxy Bridge

Локальная прослойка (**bridge.py**) между **Claude Code** и **CLIProxyAPI**.

```
Claude Code  →  bridge.py (:4000)  →  CLIProxyAPI (:8317)  →  Antigravity / провайдеры
```

| Компонент           | Где берётся                                          | Роль                                                     |
| ------------------- | ---------------------------------------------------- | -------------------------------------------------------- |
| **CLIProxyAPI**     | Готовая сборка в репо или скачивание с нуля          | Upstream-прокси, порт **8317**                           |
| **bridge.py**       | Этот репозиторий                                     | Политика моделей, ретраи, cooldown, квоты; порт **4000** |
| **Claude Code**     | npm                                                  | Клиент — ходит **на bridge** (`:4000`)                   |
| **Quota Inspector** | Готовая сборка в репо или `git clone` + `go build`   | Снимок квот CPA для логов bridge                         |

Bridge не заменяет CLIProxyAPI: без запущенного CPA bridge ходить некуда.  
Обратно CPA работает и **без** bridge — но тогда не будет rewrite / fallback / cooldown из `bridge.py`.

---

## Покупка подписки Gemini (аккаунт Google)

Antigravity / доступ к моделям завязан на **Google-аккаунт** с подпиской Gemini. При оформлении и входе соблюдайте безопасность — особенно если аккаунт покупали или заводили «под прокси».

> [!IMPORTANT]
> **Требования к покупке аккаунта и первому входу:**
> - Покупайте Google-аккаунты **только с доменом `@gmail.com`** и **обязательно включённой 2FA**.
> - **Первичный вход:** после покупки первый раз войдите в **официальное приложение Antigravity**. Если система попросит отсканировать QR-код, войдите в аккаунт на телефоне и отсканируйте QR-код.

### Обязательно

1. **VPN всегда включён** при входе в Google / Gemini / Antigravity.  
   Один и тот же регион VPN, не прыгать странами в середине сессии. Без VPN — риск блокировок и «подозрительной активности».

2. **2FA: приложение с одноразовыми кодами** (Google Authenticator, Authy и т.п.), не SMS.  
   Сразу после настройки 2FA сохраните **резервные коды** в надёжном месте.

### Сразу после покупки / получения доступа (по приоритету)

1. **Завершить все сессии** в аккаунте Google — в первую очередь.  
   [Google Account → Безопасность → Ваши устройства / управление сессиями](https://myaccount.google.com/device-activity) (или «Выйти на всех устройствах»).  
   Так отрезаются чужие/старые логины, если аккаунт передавали или логинились с чужой машины.

2. **Отвязать все чужие номера телефонов и резервные email** (в настройках безопасности Google).  
   Удалить привязанный номер телефона для восстановления и резервные адреса электронной почты продавца / предыдущего владельца, чтобы исключить возможность сброса пароля или восстановления доступа.

3. **Сменить пароль** (желательно сразу после сброса сессий и отвязки контактов).  
   Новый длинный уникальный пароль + только ваше приложение 2FA.

4. Проверить, что **2FA** привязана только к вашему приложению / устройству.

5. Уже потом — логин в Antigravity через CPA (`--antigravity-login`), **снова с VPN**.

### Краткий чеклист

| Шаг | Действие                         |
| --- | -------------------------------- |
| 1   | VPN on (один регион)             |
| 2   | Войти в Google                   |
| 3   | 2FA-приложение + резервные коды  |
| 4   | **Завершить все сессии**         |
| 5   | **Отвязать все номера и email**  |
| 6   | Сменить пароль                   |
| 7   | VPN on → Antigravity / CPA login |

> Не храните пароль и 2FA-бэкап в том же чате/репо, что и `CPA_MANAGEMENT_KEY`. `.env` — локально, не в git.

---

## Что в этом репозитории

| Путь              | Назначение                                 |
| ----------------- | ------------------------------------------ |
| `bridge.py`                         | Точка входа bridge (`python bridge.py --help`)                        |
| `llm_bridge/`                       | Код bridge: конфиг, маршрутизация, upstream, квоты, консольный UI     |
| `requirements.txt`                  | Зависимости bridge                                                    |
| `check_models.py`                   | Smoke-тест моделей через bridge                                       |
| `opus.bat`                          | Быстрый запуск Claude Code → bridge (Opus)                            |
| `start.bat`                         | Быстрый запуск всех вкладок в Windows Terminal (CPA + bridge + opus) |
| `CLIProxyAPI_6.9.48_windows_amd64/` | Готовая модифицированная сборка CLIProxyAPI                           |
| `CLIProxyAPI-Quota-Inspector/`      | Готовая модифицированная сборка Quota Inspector                       |
| `.env.example`                      | Шаблон конфига → `copy .env.example .env`                             |

Модифицированные сборки CLIProxyAPI и Quota Inspector **уже включены** в репозиторий. При желании их также можно скачать или собрать с нуля с официальных репозиториев (см. ниже).

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

### 2. CLIProxyAPI

> [!TIP]
> **Варианты установки:**
> - **Вариант А (готовая сборка из репозитория):** вы можете сразу использовать модифицированную сборку из папки `CLIProxyAPI_6.9.48_windows_amd64/`, включённую в данный репозиторий.
> - **Вариант Б (установка с нуля):**
>   1. Скачайте zip для Windows с [релизов CLIProxyAPI](https://github.com/router-for-me/CLIProxyAPI/releases) (обычно `CLIProxyAPI_*_windows_amd64.zip`).
>   2. Распакуйте в нужную папку (например, `C:\tools\CLIProxyAPI\`).

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
  secret-key: "your-management-key" # plaintext; CPA при старте захэширует — это нормально
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

> [!NOTE]
> Перед авторизацией через CPA убедитесь, что первый вход был выполнен в **официальное приложение Antigravity**. Если при входе запрашивается QR-код, авторизуйтесь в аккаунте на телефоне и отсканируйте его.

Браузер → Google-аккаунт → после успеха процесс можно закрыть.

Если браузер не открылся: `--no-browser` и откройте URL вручную.

Перелогин при **Unauthorized**:

```powershell
.\cli-proxy-api.exe --antigravity-login
```

### 4. Этот репозиторий (bridge)

```powershell
cd <корень-этого-репозитория>
pip install -r requirements.txt
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

### Быстрый запуск (одной командой)

В Windows Terminal (`wt.exe`):

```powershell
cd <корень-этого-репозитория>
.\start.bat
```

Скрипт автоматически откроет 3 вкладки в Windows Terminal:
1. **CLIProxyAPI** (из папки CPA)
2. **bridge.py** (из корня репозитория)
3. **Claude Code** (запуск `./opus.bat` в папке проекта)

---

### Запуск вручную (три независимых процесса)

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

При запуске bridge спрашивает профиль моделей. Пропустить вопрос: `python bridge.py -p 1`
(номер или ключ профиля) или `BRIDGE_PROFILE` в `.env`. Все флаги: `python bridge.py --help`.

**Ctrl+C** — корректная остановка: активные запросы получают `BRIDGE_SHUTDOWN_TIMEOUT` секунд
(по умолчанию 5) на завершение, затем обрываются; повторный Ctrl+C — остановка сразу.
В конце печатаются итоги сессии. Коды выхода: `0` — штатно, `1` — ошибка (например, порт занят),
`2` — ошибка конфигурации, `3` — приложение не стартовало.

```powershell
curl http://127.0.0.1:4000/health
python check_models.py
```

Параметры `check_models.py`:
- `--url http://localhost:4000` — URL bridge
- `--api-key sk-antigravity` — API ключ к прокси
- `--concurrency 4` — параллельность проверок
- `--timeout 30.0` — таймаут на probe в секундах
- `--thinking-param` / `--no-thinking-param` — доп. проверки с параметром thinking в body (default: `on`)

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

| Переменная                           | Значение                   | Зачем                                     |
| ------------------------------------ | -------------------------- | ----------------------------------------- |
| `ANTHROPIC_BASE_URL`                 | `http://localhost:4000`    | Claude Code → bridge                      |
| `ANTHROPIC_API_KEY`                  | dummy                      | CC требует непустой ключ; auth делает CPA |
| `ANTHROPIC_MODEL`                    | `claude-opus-4-6-thinking` | heavy-цепочка Opus → Sonnet               |
| `CLAUDE_CODE_DISABLE_PARALLEL_TOOLS` | `1`                        | без параллельных tool-call                |

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

| Запрос клиента               | Поведение                                             |
| ---------------------------- | ----------------------------------------------------- |
| Haiku / id с `haiku`         | → Haiku-модель выбранного профиля                     |
| Opus / Sonnet / heavy-алиасы | Heavy-цепочка профиля (`current`: `CLAUDE_HEAVY_MODELS`, Opus → Sonnet) |
| Остальное                    | Passthrough                                           |

### Сквозной режим (Passthrough)

Если необходимо отключить подмену и fallback моделей (передавать запросы на upstream в исходном виде):
- Установите `BRIDGE_PASSTHROUGH=1` в `.env` (глобально для всех запросов);
- Или передавайте HTTP-заголовок `X-Bridge-Passthrough: 1` для отдельных запросов.

Ретраи: `SAME_MODEL_RETRIES + 1` на модель (таймаут — не больше 2 попыток на модель). Cooldown — `COOLDOWN_*` в `.env`.
Ошибки запроса (400 и т.п.) сразу возвращаются клиенту без fallback. Если CPA недоступен, bridge
повторяет попытки, но не переключает модель (все модели идут через тот же CPA).

---

## Переменные `.env`

Шаблон — `.env.example`. Главное:

| Переменная                    | По умолчанию                                            | Описание                                                           |
| ----------------------------- | ------------------------------------------------------- | ------------------------------------------------------------------ |
| `CPA_BASE_URL`                | `http://127.0.0.1:8317`                                 | URL **внешнего** CLIProxyAPI                                       |
| `UPSTREAM_API_KEY`            | `sk-antigravity`                                        | `x-api-key` к CPA                                                  |
| `BRIDGE_HOST` / `BRIDGE_PORT` | `127.0.0.1` / `4000`                                    | Listen bridge                                                      |
| `CPA_MANAGEMENT_KEY`          | —                                                       | Тот же plaintext, что `remote-management.secret-key` в CPA (квоты) |
| `QUOTA_INSPECTOR_PATH`        | `…/CLIProxyAPI-Quota-Inspector/cpa-quota-inspector.exe` | Путь к `.exe` после `git clone` + `go build`                       |
| `QUOTA_POLL_INTERVAL`         | `300`                                                   | Опрос квот, сек                                                    |
| `CLAUDE_HEAVY_MODELS`         | opus…, sonnet…                                          | Fallback heavy                                                     |
| `BRIDGE_PROFILE`              | —                                                       | Профиль без интерактивного выбора (номер или ключ)                 |
| `BRIDGE_SHUTDOWN_TIMEOUT`     | `5`                                                     | Сколько секунд ждать активные запросы при Ctrl+C                   |
| `QUOTA_SHOW_GROUPS`           | — (по профилю)                                          | Фиксированные группы квот вместо групп выбранного профиля          |
| `QUOTA_MODEL_GROUPS`          | см. `.env.example`                                      | Модель → группа inspector (`gemini-pro-agent=Gemini 3.1 Pro Series`) |
| `BRIDGE_PASSTHROUGH`          | `0`                                                     | `1` — отключает rewrite и fallback моделей                         |
| `QUOTA_LIVE_DISPLAY`          | `1`                                                     | `1` — живой монитор квот в консоли (`rich.Live`)                   |
| `SAME_MODEL_RETRIES`          | `4`                                                     | Доп. попытки на модель                                             |
| `COOLDOWN_*`                  | 60 / 1800 / 30 / 300                                    | TTL cooldown, сек                                                  |

---

## HTTP API bridge

| Метод  | Путь           | Описание                                 |
| ------ | -------------- | ---------------------------------------- |
| `GET`  | `/health`      | Статус, токены, cooldown, snapshot квот  |
| `POST` | `/v1/messages` | Anthropic Messages (stream / non-stream) |
| `POST` | `/v1/messages/count_tokens` | Подсчёт токенов (модель подменяется по политике) |

---

## Quota Inspector (опционально)

Не обязателен для работы bridge.

> [!TIP]
> **Варианты использования:**
> - **Вариант А (готовая сборка из репозитория):** в репозиторий уже включена готовая модифицированная сборка в папке `CLIProxyAPI-Quota-Inspector/`.
> - **Вариант Б (сборка с нуля из исходников):** требуются **Git** и **Go**.

### Сборка с нуля из исходников

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

| Endpoint           | Если CPA «просто запущен»             |
| ------------------ | ------------------------------------- |
| `/v1/models`       | обычно **200**                        |
| `/v0/management/*` | **404**, пока Management API выключен |

В `config.yaml` CPA:

```yaml
remote-management:
  # secret-key: ""          # пусто = Management API отключён → 404 на все /v0/management
  allow-remote: false
  secret-key: "your-management-key" # тот же, что CPA_MANAGEMENT_KEY в .env bridge
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

| Ответ          | Смысл                                                     |
| -------------- | --------------------------------------------------------- |
| **404**        | `secret-key` пустой / Management API выключен             |
| **401**        | Management включён, но ключ в `.env` ≠ `secret-key` в CPA |
| **200** + JSON | всё ок                                                    |

Ключ в `.env` (`CPA_MANAGEMENT_KEY`) и `secret-key` в `config.yaml` CPA должны совпадать.

---

## Автозапуск CPA (опционально)

1. `Win + R` → `shell:startup`
2. Ярлык на **ваш** `cli-proxy-api.exe`

Bridge и Claude Code — вручную или своим скриптом.

---

## Если что-то не работает

| Симптом                         | Что сделать                                                                               |
| ------------------------------- | ----------------------------------------------------------------------------------------- |
| **Invalid API key**             | В `config.yaml` CPA закомментировать `api-keys:`; перезапустить CPA                       |
| **Connection refused** на 8317  | CPA не запущен / другой `port` / неверный `CPA_BASE_URL`                                  |
| **Connection refused** на 4000  | Не запущен `python bridge.py`                                                             |
| Claude Code без политики bridge | `ANTHROPIC_BASE_URL=http://127.0.0.1:4000`                                                |
| **Unauthorized**                | Из папки CPA: `.\cli-proxy-api.exe --antigravity-login`                                   |
| **Rate limit / quota**          | Подождать; bridge ретраит; добавить аккаунты в CPA                                        |
| Quota inspector → **404**       | В CPA `secret-key: ""` — Management API выключен; задайте `secret-key`, перезапустите CPA |
| Quota inspector → **401**       | Management включён, но `CPA_MANAGEMENT_KEY` ≠ `secret-key`                                |
| Quota inspector warning         | `git clone` + `go build`? `QUOTA_INSPECTOR_PATH`? Ключ в CPA **и** в `.env`?              |
| Брандмауэр                      | Разрешить CPA / Python на localhost                                                       |
| Проверка CPA (API)              | `Invoke-WebRequest http://127.0.0.1:8317/v1/models`                                       |
| Проверка Management API         | Bearer-запрос к `/v0/management/auth-files` (см. выше)                                    |
| Проверка bridge                 | `curl http://127.0.0.1:4000/health`                                                       |

---

## Ссылки

- [CLIProxyAPI](https://github.com/router-for-me/CLIProxyAPI) · [релизы](https://github.com/router-for-me/CLIProxyAPI/releases)
- [Документация](https://help.router-for.me/)
- [CLIProxyAPI Quota Inspector](https://github.com/AllenReder/CLIProxyAPI-Quota-Inspector) — `git clone https://github.com/AllenReder/CLIProxyAPI-Quota-Inspector.git`

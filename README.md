# MoM — протоколы встреч из записей

Self-hosted сервис: запись созвона (**webm**, mp4, mp3, wav, m4a, ogg) остаётся на машине. Локальный Whisper или Сбер GigaAM расшифровывает речь, Qwen или OpenAI собирает саммари, поручения и протокол (Minutes of Meeting). В транскрипте спикеров можно назвать и поправить. Пользователи входят локальным паролем или через LDAP/LDAPS.

Данные — SQLite в `data/`. Один процесс FastAPI раздаёт API и интерфейс. Для установки в контуре компании достаточно Docker: Python, ffmpeg и PyTorch уже внутри образа.

## Системные требования

Минимум для развёртывания с моделью Whisper `small` (значение по умолчанию):

| Ресурс | Минимум | Рекомендуется |
|---|---|---|
| ОС | Ubuntu 22.04/24.04 (Debian 12), x86_64 | то же, 4 vCPU |
| CPU | 2 ядра | 4 ядра |
| RAM | 4 ГБ | 8 ГБ |
| Свободное место | 8 ГБ | 20 ГБ и больше под записи и модели |
| Сеть | исходящий HTTPS при установке и для Qwen/OpenAI | без входящего 8000 из интернета |
| GPU | не нужен | — |

ПО, которое ставит `install.sh` или должно быть заранее: **Python 3.10+**, **ffmpeg** и **ffprobe**, **Node.js 18+** (только чтобы собрать интерфейс), компилятор (`build-essential`), git.

На 4 ГБ RAM не ставьте `LOCAL_WHISPER_SIZE=medium` или `large-*` — не хватит памяти. Архитектура ARM64 (Raspberry, Ampere) обычно работает, но колёса faster-whisper могут собираться дольше.

Для доступа с браузера достаточно обычного Chrome/Firefox/Safari. Порт по умолчанию **8000**.

## Установка на Linux без Docker

Нужен исходящий доступ в интернет при установке (пакеты, npm, PyTorch, модель Whisper ~500 МБ).

```bash
sudo apt-get update
sudo apt-get install -y git
git clone https://github.com/Alesnyt/MoM.git
cd MoM
chmod +x install.sh start.sh update.sh
./install.sh
./start.sh
```

`install.sh` ставит Python 3.10+, ffmpeg, компилятор, Node.js 18+, зависимости Python, собирает UI и скачивает Whisper `small` в `data/whisper/`.

Полезные флаги:

```bash
./install.sh --skip-model    # не качать Whisper сразу (скачается при первой расшифровке)
./install.sh --systemd       # установить и запустить сервис mom.service
```

Откройте `http://<IP-виртуальной-машины>:8000`. На первом входе администратора введите **SETUP_TOKEN** из вывода `install.sh` (он же в `.env`), создайте логин админа, сохраните API-ключ и заведите пользователей. Без профиля пользователя архив недоступен. Админка — отдельно: `http://<IP>/admin`.

## Установка через Docker

На машине нужны только Docker и Docker Compose. Python, ffmpeg, Node, PyTorch, Whisper и GigaAM ставятся внутрь образа. Модели и записи лежат в `./data`, настройки — в `./.env`.

```bash
cp .env.example .env
docker compose up -d --build
```

Первый запуск долгий: образ тянет CPU-сборку PyTorch. В логе будет `SETUP_TOKEN`, если в `.env` он пустой. Контейнер считается живым, когда `/api/health` отвечает.

Разметка спикеров в образ по умолчанию не входит. Чтобы поставить pyannote сразу:

```bash
docker compose build --build-arg INSTALL_DIARIZE=1
docker compose up -d
docker compose logs mom
```

Откройте `http://<IP>:8000`. Каталог `data/` и файл `.env` переживают пересборку контейнера. Сервер внутри контейнера работает от пользователя `mom`, не от root. Whisper `small` скачается в `data/whisper/` при первой расшифровке, не при сборке образа.

Образ публикуется в GHCR по тегу `v*`: `ghcr.io/alesnyt/mom:latest`. Обновление из реестра: `docker compose pull && docker compose up -d`. Сборка на месте: `git pull && docker compose up -d --build`.

### Контур, где наружу ходит только GitHub

`docker compose build` на целевой машине снова тянет Docker Hub, PyPI, npm и Hugging Face. Если эти адреса закрыты, образ и веса собирают там, где они открыты (или в GitHub Actions), и привозят файлами.

На машине с доступом к этим адресам:

```bash
docker compose build
docker compose up -d
docker compose exec mom python -c "from faster_whisper import WhisperModel; WhisperModel('small', device='cpu', compute_type='int8', download_root='/app/data/whisper')"
docker compose down
docker save "$(docker compose images -q mom)" -o mom-image.tar
tar czf mom-models.tgz data/whisper data/hf
```

Если в админке выбран GigaAM, на той же машине заранее скачайте веса в `data/hf`. Код модели уже в репозитории, с Hugging Face берутся только `config.json` и `pytorch_model.bin` зафиксированных коммитов `ctc` и `large_ctc`. Иначе первая расшифровка снова пойдёт за весами.

В контуре:

```bash
git clone https://github.com/Alesnyt/MoM.git
cd MoM
cp .env.example .env
docker load -i mom-image.tar
tar xzf mom-models.tgz
```

В `.env` добавьте `HF_HUB_OFFLINE=1` и `TRANSFORMERS_OFFLINE=1`. Запуск без повторной сборки: `docker compose up -d` (без `--build`). Облачный чат Qwen или OpenAI по-прежнему нужен исходящий HTTPS.

Не публикуйте порт 8000 в интернет. Для доступа снаружи поставьте Caddy или nginx с TLS — cookie `Secure` ставится только за HTTPS (или если запрос пришёл с localhost через доверенный прокси).

```Caddyfile
mom.example.com {
    reverse_proxy 127.0.0.1:8000
}
```

Пример nginx: терминация TLS на 443, `proxy_set_header X-Forwarded-Proto $scheme;` и `X-Forwarded-For`, а uvicorn слушает только `127.0.0.1:8000` (`HOST=127.0.0.1` в `.env`). Заголовки прокси MoM принимает только с loopback, иначе клиент не сможет сам объявить HTTPS.

## Переменные окружения

Файл `.env` не коммитится. Образец — `.env.example`.

| Переменная | Смысл |
|---|---|
| `HOST` | По умолчанию `0.0.0.0` (доступ с других машин) |
| `PORT` | По умолчанию `8000` |
| `SETUP_TOKEN` | Токен первого создания администратора (`install.sh` генерирует сам) |
| `OPENAI_API_KEY` | Ключ Qwen (`sk-sp-…` / `sk-ws-…`) или OpenAI |
| `WHISPER_MODEL` | `local-whisper` / `local-whisper-medium` или `gigaam-multilingual` / `gigaam-multilingual-large` |
| `LOCAL_WHISPER_SIZE` | `tiny` / `base` / `small` (по умолчанию) / `medium` / `large-v2` / `large-v3` |
| `MAX_UPLOAD_MB` | Лимит загрузки, по умолчанию 512 |
| `MIN_FREE_MB` | Не принимать запись, если свободно меньше этого запаса. По умолчанию `2048` |
| `UI_THEME` | Тема интерфейса: `classic` (по умолчанию) или `t2` |
| `MAX_JOBS` | Сколько встреч обрабатывать сразу: `1`–`8`, по умолчанию `1`. ffmpeg и Qwen идут параллельно; локальный Whisper/GigaAM в RAM один |
| `SMTP_HOST` | SMTP-сервер для писем «протокол готов» на email пользователя |
| `SMTP_PORT` | Обычно `587` (STARTTLS) или `465` (SSL) |
| `SMTP_USER` / `SMTP_PASSWORD` | Учётная запись SMTP |
| `SMTP_FROM` | Адрес отправителя |
| `SMTP_STARTTLS` | `1` по умолчанию; для порта 465 не нужен |
| `PUBLIC_BASE_URL` | Необязательная ссылка на MoM в письме, например `https://mom.company.ru` |
| `ASR_IDLE_UNLOAD_SECONDS` | Через сколько секунд простоя очереди выгрузить Whisper/GigaAM из RAM, по умолчанию `300` |
| `HF_TOKEN` | Токен Hugging Face для разметки спикеров (pyannote). Без него все реплики — один спикер |
| `DIARIZE_MAX_SPEAKERS` | Верхняя граница числа спикеров, по умолчанию `8` |
| `LDAP_ENABLED` | `1`, чтобы выбранные пользователи входили паролем из каталога |
| `LDAP_URL` | `ldaps://ldap.example.com:636` или `ldap://ldap.example.com:389` |
| `LDAP_BIND_DN` / `LDAP_BIND_PASSWORD` | Служебная учётная запись для поиска |
| `LDAP_BASE_DN` | База поиска, например `ou=people,dc=example,dc=com` |
| `LDAP_USER_FILTER` | Фильтр с `{username}`, по умолчанию `(mail={username})` |
| `LDAP_STARTTLS` | `1` для STARTTLS на `ldap://` |
| `LDAP_TLS_VERIFY` | `1` проверять сертификат, `0` принять самоподписанный |

Ключ можно задать и в веб-интерфейсе администратора.

## Запуск после установки

```bash
./start.sh
```

Если порт занят: остановите другой процесс или `MOM_KILL_PORT=1 ./start.sh`.

Не запускайте несколько worker-процессов uvicorn: модель Whisper/GigaAM живёт в памяти одного процесса. Внутри процесса очередь (`MAX_JOBS`, по умолчанию 1) запускает несколько встреч сразу, если задать больше: вырезание аудио и запросы к Qwen параллелятся, локальное распознавание берёт модель по очереди, чтобы не грузить GigaAM дважды. После рестарта встреча с уже сохранённой расшифровкой продолжает только сборку протокола, а не гоняет Whisper заново. Сессии сохраняются в SQLite.

## Обновление с GitHub

После правок в репозитории на уже установленной ВМ:

```bash
cd MoM
git pull
chmod +x update.sh
./update.sh
```

Дальше достаточно `./update.sh`: он делает `git pull`, ставит Python-зависимости, пересобирает интерфейс и перезапускает `mom.service`, если сервис установлен. Файл `.env`, база и записи не трогаются. Новые переменные из `.env.example` дописываются, уже заданные значения не перезаписываются.

Если процесс запущен через `./start.sh`, после обновления остановите его и запустите снова.

## Разработка на своей машине

Нужны Python 3.10+, Node.js 18+ и ffmpeg.

```bash
./install.sh --skip-model
./start.sh
```

Откройте [http://127.0.0.1:8000](http://127.0.0.1:8000).

## Как это работает

1. Из записи вырезается моно-аудио 16 kHz.
2. Локальный ASR расшифровывает речь: Whisper (faster-whisper на CPU) или Сбер GigaAM Multilingual (2026).
3. Если задан `HF_TOKEN` и установлен `requirements-diarize.txt`, pyannote размечает, кто говорил. В транскрипте это «Спикер 1», «Спикер 2»: имя меняется сразу у всех реплик и в протоколе, чужую реплику можно отдать другому спикеру. Без токена запись всё равно обрабатывается, но как один спикер.
4. LLM собирает саммари, решения, поручения и MoM.
5. Результат можно скачать как Markdown или открыть как черновик письма. Если в админке задан SMTP, готовый протокол уходит на email пользователя (логин в MoM).

Несколько человек могут загрузить записи сразу: сервер ставит их в FIFO-очередь. Сколько идёт сразу, задаёт `MAX_JOBS` (1–8). По умолчанию один слот — безопасно для 4–8 ГБ. Если RAM и CPU позволяют, в админке поднимаете параллельность: ffmpeg и Qwen не ждут друг друга, локальный ASR не клонирует модель. В интерфейсе видно, сколько встреч впереди. Повтор после ошибки встаёт в конец очереди. SMTP задаётся в админке; без него обработка не падает — протокол просто остаётся в MoM.

## Данные

Локально в `data/`: SQLite (пользователи, протоколы, сессии), загрузки, аудио, кэш Whisper и Hugging Face (веса GigaAM). Каталог и `.env` в git не попадают и при `./update.sh` не удаляются.

Снимок базы и записей, без кэша моделей:

```bash
.venv/bin/python -m backend.backup
.venv/bin/python -m backend.backup --check backups/20261009T120000Z
```

В контейнере то же самое: `docker compose exec mom python -m backend.backup`. Каталог `./backups` смонтирован в контейнер. Команда пишет согласованную копию SQLite через `backup()`, плюс `uploads/`, `audio/` и `exports/`. Каталог снимка закрыт от остальных пользователей машины. Сразу после записи она открывает снимок только на чтение и проверяет `integrity_check`. Живую базу копировать через `cp` не нужно: у SQLite включён WAL.

В админке на сводке видно, сколько свободно на диске и сколько занимают записи. Если свободно меньше `MIN_FREE_MB` (по умолчанию 2 ГБ), новая загрузка не принимается.

## Сбер GigaAM Multilingual

По умолчанию стоит Whisper `small`. В админке можно выбрать **Whisper** (и размер) или **Сбер GigaAM Multilingual** 220M / 600M.

PyTorch и остальные пакеты GigaAM ставятся автоматически при `./install.sh` и `./update.sh`. Код модели лежит в репозитории (`backend/vendor/gigaam/modeling_gigaam.py`) и сверяется по SHA-256 перед импортом, поэтому процесс не исполняет скрипт, скачанный с Hugging Face. Веса по-прежнему качаются в `data/hf/` с зафиксированных коммитов, и размер файла сверяется. Длинные записи режутся на фрагменты по 24 с. На 4 ГБ RAM берите Whisper `tiny`/`small`, не GigaAM 600M.

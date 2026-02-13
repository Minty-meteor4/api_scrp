# OMNI-SCANNER v20.0

OSINT-сканер для поиска утекших API-ключей OpenAI и Anthropic на GitHub, GitLab, paste-сайтах и других открытых источниках. Работает 24/7 на VPS, автоматически верифицирует найденные ключи и распределяет по файлам.

## Возможности

- **12 источников поиска**: GitHub (Code, Issues, Commits, Events, Gists), GitLab, DuckDuckGo, NPM, PyPI, HuggingFace, Sourcegraph, grep.app, Pastebin, Replit
- **Real-time мониторинг**: GitHub Events API + Gist Live каждые 10-20 секунд
- **Умный TokenPool**: отслеживает `X-RateLimit-Remaining/Reset` заголовки, выбирает токен с наибольшим остатком, превентивный cooldown
- **Разделение токенов**: 1 выделенный для real-time + остальные для глубокого сканирования
- **Автоверификация**: проверка ключей через API (OpenAI chat completions, Anthropic messages)
- **Периодическая перепроверка**: cron-скрипт каждые 15 минут перепроверяет баланс всех ключей
- **Дедупликация**: ключи не дублируются между файлами

## Структура файлов

```
key_scanner.py       # Основной сканер (~2500 строк)
recheck_empty.py     # Перепроверка баланса ключей (cron)
.env                 # Конфигурация (токены, интервалы)
deploy.sh            # Скрипт деплоя на VPS
scanner.service      # Systemd unit файл
requirements.txt     # Python-зависимости
openai_keys.txt      # Найденные ключи OpenAI с балансом
anthropic_keys.txt   # Найденные ключи Anthropic с балансом
empty_keys.txt       # Ключи без баланса / отозванные
```

## Установка

### Быстрый деплой на VPS

```bash
scp -r . root@YOUR_VPS_IP:~/scanner
ssh root@YOUR_VPS_IP 'cd ~/scanner && bash deploy.sh'
```

### Ручная установка

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

## Настройка

Создайте `.env` файл:

```env
# Обязательно: GitHub токены (через запятую)
GITHUB_TOKENS=ghp_token1,ghp_token2,ghp_token3

# Опционально: GitLab токен
GITLAB_TOKEN=glpat-xxxxx

# Интервалы (секунды)
EVENTS_POLL_INTERVAL=10      # Опрос GitHub Events (default: 30)
GIST_POLL_INTERVAL=20         # Опрос Gists (default: 60)
SLOW_LOOP_INTERVAL=300        # Глубокий скан (default: 600)
TOKEN_COOLDOWN_SECONDS=60     # Cooldown при rate-limit (default: 65)
CONCURRENCY_LIMIT=300         # Макс. параллельных HTTP-запросов (default: 150)
```

## Запуск

```bash
# Прямой запуск
python key_scanner.py

# Через systemd (после deploy.sh)
systemctl start scanner
systemctl status scanner
journalctl -u scanner -f
```

## Cron: перепроверка баланса

```cron
*/15 * * * * /root/apikeys/venv/bin/python /root/apikeys/recheck_empty.py >> /root/apikeys/recheck.log 2>&1
```

Что делает `recheck_empty.py`:
- Ключи в funded-файлах потеряли баланс → перемещает в `empty_keys.txt`
- Ключи в `empty_keys.txt` получили баланс → перемещает в соответствующий funded-файл

## Сканеры

| Сканер | Источник | GitHub-токен | Интервал |
|--------|----------|:------------:|----------|
| GitHubScanner | GitHub Code Search | да | 5 мин |
| GistScanner | GitHub Gist Search | да | 5 мин |
| GitLabScanner | GitLab Search | GitLab | 5 мин |
| GitHubEventsScanner | GitHub Events API | да (1 выделенный) | 10 сек |
| GistEventsScanner | Public Gists Feed | да (1 выделенный) | 20 сек |
| GitHubIssuesScanner | GitHub Issues Search | да | 5 мин |
| GitHubCommitsScanner | GitHub Commits Search | да | 5 мин |
| SourcegraphScanner | Sourcegraph | нет | 5 мин |
| GrepAppScanner | grep.app | нет | 5 мин |
| PastebinRecentScanner | Pastebin Archive | нет | 5 мин |
| ReplitSearchScanner | Replit GraphQL | нет | 5 мин |
| WebScanner | DuckDuckGo | нет | 5 мин |
| NpmScanner | NPM Registry | нет | 5 мин |
| PyPiScanner | PyPI | нет | 5 мин |
| HuggingFaceScanner | HuggingFace Spaces | нет | 5 мин |

## Паттерны ключей

| Провайдер | Паттерн | Пример |
|-----------|---------|--------|
| OpenAI | `sk-proj-...`, `sk-svcacct-...`, `sk-...` | `sk-proj-abc123...` |
| Anthropic | `sk-ant-apiXX-...` | `sk-ant-api03-abc123...` |

## Верификация

- **OpenAI**: POST `/v1/chat/completions` с `max_tokens=1` → проверка HTTP-кода (200=funded, 401=invalid, 429 с `billing_not_active`/`insufficient_quota`=empty, 429 без них=funded rate-limited)
- **Anthropic**: POST `/v1/messages` с `max_tokens=1` → аналогичная логика

## Требования

- Python 3.11+
- Минимум 1 GitHub Personal Access Token (рекомендуется 3+)
- VPS для 24/7 работы (опционально)

# `agy-chat-migration` — Универсальный инструментарий и гайд по миграции чатов и проектов Antigravity

[English version (README.md)](./README.md)

Универсальный набор инструментов (чистый Python 3 stdlib, без внешних зависимостей) и скилл/плагин для Antigravity, который позволяет переносить, привязывать, объединять, делить и синхронизировать диалоги Antigravity (`conversations/*.db`, `annotations/*.pbtxt`, `brain/*`, `agyhub_summaries_proto.pb`) между:

- **Проектами** (перенос чата из Проекта A в Проект B или привязка потерянных чатов из *Outside of Project*)
- **Папками на одном ПК** (перепривязка после переноса или переименования папки проекта)
- **Редакциями Antigravity** (переезд со старого `antigravity-ide` v1 ➔ Desktop v2 `antigravity` ➔ CLI-демон `antigravity-cli` / Remote Control)
- **Операционными системами и машинами** (`Windows` ⇄ `Linux` ⇄ `macOS` через любой транспорт: `scp`, `rsync`, `.tar.gz`/`.zip`, USB, облако или любой файловый синхронизатор)

---

## Зачем нужен этот инструмент

Antigravity хранит историю диалогов **не внутри папки проекта**, а в четырех синхронизированных слоях внутри `~/.gemini/`:

1. `conversations/<cid>.db` (SQLite + Protobuf `TrajectoryMetadata` с URI под конкретную ОС и UUID `project_id`)
2. `annotations/<cid>.pbtxt` (короткий заголовок в боковой панели + время последнего просмотра)
3. `conversation_summaries.db` (SQL-таблица + `CascadeTrajectorySummary` Protobuf `raw_summary` + полнотекстовый индекс `conversations_fts`)
4. `agyhub_summaries_proto.pb` / `jetbox_summaries_proto.pb` (бинарный кэш запуска с таблицей `file_mod_times`)

Простое копирование `.db`-файлов приводит к потере привязки к проекту, исчезновению чатов на Windows из-за ловушки `AdditionalConvoDir` (`Field 20 = 17`), потере коротких заголовков (или замене их многострочным первым промптом) и потере свежих сообщений из открытых SQLite `-wal` журналов. Этот инструментарий безопасно обновляет все 4 слоя и отправляет живое RPC-обновление (`JetboxWriteSummary`) в работающий `language_server`.

---

## Установка

> **Требования:** Python 3.9+ (только стандартная библиотека, без `pip install`).

### Вариант 1: Установка как плагин Antigravity (рекомендуется)
Клонируйте репозиторий в папку плагинов — Antigravity автоматически подхватит скилл для всех проектов:

```bash
git clone https://github.com/MixasikM/agy-chat-migration.git ~/.gemini/config/plugins/agy-chat-migration
```

В Windows PowerShell:
```powershell
git clone https://github.com/MixasikM/agy-chat-migration.git "$env:USERPROFILE\.gemini\config\plugins\agy-chat-migration"
```

### Вариант 2: Установка только глобального скилла
```bash
git clone https://github.com/MixasikM/agy-chat-migration.git
cd agy-chat-migration
python skills/agy-chat-migration/scripts/install_skill.py
```

### Вариант 3: Установка в конкретный проект (`.agents/skills/`)
```bash
python skills/agy-chat-migration/scripts/install_skill.py --workspace /path/to/your/project
```

### Вариант 4: Развертывание на удаленный Linux/macOS сервер (опционально с systemd-таймером)
```bash
python skills/agy-chat-migration/scripts/install_skill.py --remote user@devserver --setup-systemd-timer --projects-root ~/projects
```

---

## Основные сценарии использования

### 1. Найти потерянные чаты (*Outside of Project*, битые пути или старый `antigravity-ide`)
```bash
python scripts/audit_project_for_migration.py --list-orphan-chats
```

### 2. Перенести или привязать чат(ы) в нужный проект
```bash
python scripts/agy_capsule_sync.py move-chat \
  --project-dir "/path/to/target-project" \
  --chat-id "<CONVERSATION_UUID>"
```
Параметр `--chat-id` можно указывать несколько раз. Артефакты из `~/.gemini/antigravity-ide/brain/<cid>` при наличии подтягиваются и объединяются автоматически.

### 3. Перепривязать проект после переноса или переименования папки на том же ПК
```bash
python scripts/agy_capsule_sync.py relink-project --project-dir "/new/path/to/my-app"
```
*(Если папка была переименована и `project_id` не определился по имени, передайте `--project-id "<UUID>"`, который можно заранее узнать через `audit_project_for_migration.py`.)*

### 4. Перенос проекта и всей истории чатов на другую машину / ОС (`Windows` ⇄ `Linux` ⇄ `macOS`)

**На исходной машине:**
```bash
# 1. Read-only аудит перед переездом (размеры .db/brain, чаты без заголовков, открытые -wal, Windows-пути)
python scripts/audit_project_for_migration.py "/source/path/to/my-app"

# 2. Экспорт переносимой капсулы .agy-capsule/ (или в любую внешнюю папку через --capsule-dir)
python scripts/agy_capsule_sync.py export --project-dir "/source/path/to/my-app"
```

**Перенесите папку проекта вместе с `.agy-capsule/`** любым удобным способом (`rsync`, `scp`, `.tar.gz`/`.zip`, флешка, облако или файловый синхронизатор). *(Если проект под Git, добавьте `.agy-capsule/` в `.gitignore`, чтобы бинарные базы не попадали в коммиты.)*

**На целевой машине:**
```bash
# 3. Импорт, пересборка URI под целевую ОС и перепривязка всех 4 слоев
python3 scripts/agy_capsule_sync.py import --project-dir "/target/path/to/my-app" --force

# На Linux-сервере с запущенным antigravity-cli-daemon.service добавьте --restart-service:
python3 scripts/agy_capsule_sync.py import --project-dir "/target/path/to/my-app" --restart-service --force

# 4. Проверка статуса
python3 scripts/agy_capsule_sync.py status --project-dir "/target/path/to/my-app"
```

### 5. Двусторонняя синхронизация между машинами
Если `.agy-capsule/` синхронизируется между вашими машинами любым инструментом:
```bash
python scripts/agy_capsule_sync.py sync --project-dir "/path/to/my-app"
```
Метаданные каждого чата хранятся в отдельном файле `.agy-capsule/summaries/<cid>.json` и объединяются по максимальному `step_count`, исключая конфликты слияния.

---

## Подробная документация и «подводные камни»

- **[Рабочий регламент (SKILL.md)](./skills/agy-chat-migration/SKILL.md)** — Пошаговый runbook и золотые правила.
- **[4-слойная архитектура и 10 подводных камней](./skills/agy-chat-migration/references/architecture-and-pitfalls.md)** — Почему чаты пропадают после перезапуска (`AdditionalConvoDir` на Windows, рассинхрон `mtime` и `agyhub_summaries_proto.pb`), почему заголовки слетают в длинные промпты, ловушки `-wal`/`-shm`, экранирование `%3A` в Windows URI и порча конфигов через PowerShell `ssh`.
- **[Гайд по разделению и обрезке гигантских чатов](./skills/agy-chat-migration/references/conversation-splitting-guide.md)** — Устройство таблиц `<cid>.db`, структура `CHECKPOINT` (`Field 30`), реактивация архивных шагов, обновление `conversations_fts` и фильтрация `transcript*.jsonl` в `brain/<cid>`.

---

## Лицензия

[MIT](./LICENSE)

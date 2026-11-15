"""Operating parameters that an administrator changes in the panel («Настройки»), not in `.env`.

How it fits together
  * `.env` and the defaults in `app/core/config.py` stay the *baseline*: what a parameter is when nobody changed it.
  * A change is a row in `app_settings`. It wins over the baseline; deleting the row ("сбросить") brings the baseline back.
  * Everything in the panel reads `settings.<name>` at the moment it needs the value, so applying a change is just setting that
    attribute. Every process (API, workers, beat) reloads the table every few seconds (`start_refresher`) and the API also applies a
    change at once, so a new value reaches all of them within seconds, with no restart.
  * Not here, on purpose: whatever the panel needs *before* it can read the database or that is a secret (JWT_SECRET,
    SECRET_ENCRYPTION_KEY, DB and Redis addresses, ADMIN_*, ORESHNIK_ENV, CORS, AGENT_TOKEN/URL), paths and binaries, and the
    worker's process counts (SCAN_WORKER_SLOTS, AGENT_CONCURRENCY) — those are read when a process starts.
"""

import json
import logging
import re
import threading
from dataclasses import dataclass, field
from typing import Any, Callable

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import AGENT_TOOLS, STRIX_API_TYPES, STRIX_REASONING_EFFORTS, settings

log = logging.getLogger("oreshnik.settings")

REFRESH_SECONDS = 5.0


@dataclass(frozen=True)
class Spec:
    key: str                      # the attribute of `settings`; the env variable is its upper-case spelling
    group: str
    label: str
    hint: str
    kind: str                     # int | float | bool | choice | text
    minimum: float | None = None
    maximum: float | None = None
    choices: tuple[str, ...] = ()
    unit: str = ""
    restart: str = ""             # when not empty: what must be restarted before a change takes effect
    pattern: str = ""             # text only: the whole value must match (these end up in shell commands)
    check: Callable[[Any], None] | None = field(default=None, compare=False)

    @property
    def env(self) -> str:
        return self.key.upper()


def _scan_concurrency_ceiling(value: Any) -> None:
    if value > settings.scan_worker_slots:
        raise ValueError(
            f"Потолок воркера — {settings.scan_worker_slots} процессов (SCAN_WORKER_SLOTS в .env, нужен перезапуск воркера)"
        )


def _headers_json(value: Any) -> None:
    if not value:
        return
    try:
        parsed = json.loads(value)
    except ValueError:
        raise ValueError('нужен JSON-объект, например {"X-Team": "red"}')
    if not isinstance(parsed, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in parsed.items()):
        raise ValueError('нужен JSON-объект из строк, например {"X-Team": "red"}')


_SIZE = r"(\d{1,6}[bkmgBKMG]?)?"

SPECS: tuple[Spec, ...] = (
    # ── Сканы ──
    Spec("scan_concurrency", "Сканы", "Сканов одновременно", "Сколько сканов выполняется сразу; остальные ждут в очереди и стартуют, как только место освободится. "
         "Каждый скан — Strix, контейнеры песочницы и расход на модель; на один скан нужно около 2,5 ГБ памяти, иначе сервер уйдёт в своп.", "int", 1, 64, check=_scan_concurrency_ceiling),
    Spec("max_active_scans_per_user", "Сканы", "Активных сканов на аккаунт (на одном раннере)", "Очередь + выполняются. У раннера может быть своё число — оно главнее.", "int", 1, 100),
    Spec("scan_timeout_minutes", "Сканы", "Предел времени скана", "0 — без предела. Подстраховка от зависшего процесса; основной ограничитель расходов — бюджет скана.", "int", 0, 10080, unit="мин"),
    Spec("strix_cancel_grace_seconds", "Сканы", "Пауза при остановке скана", "Столько секунд Strix получает на аккуратное завершение, потом процесс завершают принудительно.", "int", 1, 600, unit="с"),
    Spec("scan_stale_seconds", "Сканы", "Скан без признаков жизни", "Выполняющийся скан, воркер которого не подавал сигнала столько секунд, помечается упавшим.", "int", 30, 3600, unit="с"),
    Spec("scan_log_max_bytes", "Сканы", "Размер лога скана", "Лог одного скана обрезается на этом размере, чтобы не заполнить диск.", "int", 1_000_000, 2_000_000_000, unit="байт"),
    # ── Расписания ──
    Spec("schedule_min_interval_minutes", "Расписания", "Минимальный интервал расписания", "Чаще, чем раз в столько минут, расписание с реальным раннером запускаться не может.", "int", 1, 1440, unit="мин"),
    Spec("max_schedules_per_user", "Расписания", "Расписаний на аккаунт", "", "int", 1, 1000),
    # ── Strix ──
    Spec("strix_reasoning_effort", "Strix", "Глубина размышлений модели", "Как долго модель думает над каждым запросом. Влияет только на модели, которые это поддерживают.", "choice", choices=STRIX_REASONING_EFFORTS),
    Spec("strix_api_type", "Strix", "Какой API у модели", "auto: профили custom (шлюзы) идут через chat_completions, остальные — как решит Strix. "
         "Шлюз, отвечающий «This endpoint supports function tools only», требует chat_completions.", "choice", choices=STRIX_API_TYPES),
    Spec("llm_preflight_timeout", "Strix", "Ожидание пробного запроса к модели", "Сколько Strix ждёт ответа на один короткий запрос перед сканом. Поднимите для медленного шлюза.", "int", 1, 600, unit="с"),
    Spec("llm_timeout", "Strix", "Ожидание ответа модели во время скана", "Сколько Strix ждёт один ответ модели. Поднимите для медленного шлюза, если посреди скана видите таймауты или код 499.", "int", 10, 3600, unit="с"),
    Spec("llm_stream_idle_timeout", "Strix", "Тишина в потоке ответа", "Сколько потоковый ответ модели может молчать, прежде чем Strix его бросит. 0 — без ограничения.", "int", 0, 3600, unit="с"),
    Spec("llm_retry_attempts", "Strix", "Повторы при недоступной модели", "Сколько раз скан сам запускается заново (перед стартом) или продолжается с того же места (если упал посреди работы), когда модель не ответила по временной причине "
         "(502/503/504, перегрузка, лимит запросов, таймаут, обрыв связи). Неверный ключ, неизвестная модель и исчерпанная квота не повторяются. 0 — выключено.", "int", 0, 10),
    Spec("llm_retry_codes", "Strix", "Коды ошибок для повтора", "Коды ответа шлюза модели через запятую, после которых скан запускается заново. "
         "499 — шлюз не дождался ответа модели. Коды 400, 401, 402, 403, 404 и 422 не повторяются никогда, даже если их вписать.", "text", pattern=r"\s*(\d{3}\s*(,\s*\d{3}\s*)*)?"),
    Spec("llm_retry_delay_seconds", "Strix", "Пауза перед повтором", "Сколько ждать перед каждым таким повтором.", "int", 5, 3600, unit="с"),
    Spec("strix_telemetry", "Strix", "Телеметрия Strix", "Выключена по умолчанию: пентест-инструмент не должен ни о чём докладывать наружу.", "bool"),
    Spec("strix_update_check", "Strix", "Проверка обновлений Strix", "Раз в сутки Strix спрашивает PyPI о новой версии. Панель закрепляет версию, так что обычно не нужна.", "bool"),
    Spec("strix_version", "Strix", "Версия Strix для установки", "Что ставится на SSH-раннеры кнопкой «Установить». Панель проверена с 1.7.0.", "text", pattern=r"[0-9A-Za-z][0-9A-Za-z.+_-]{0,31}"),
    Spec("strix_sandbox_image", "Strix", "Образ песочницы", "Docker-образ, который ставится на SSH-раннеры.", "text", pattern=r"[A-Za-z0-9][A-Za-z0-9_./:@-]{0,199}"),
    # ── Strix: песочница ──
    Spec("strix_sandbox_mem_limit", "Strix: песочница", "Память контейнера", "Потолок памяти песочницы одного скана, например 4g. Пусто — без ограничения.", "text", pattern=_SIZE),
    Spec("strix_sandbox_cpus", "Strix: песочница", "Процессор", "Сколько ядер может занять песочница одного скана, например 2 или 1.5. Пусто — без ограничения.", "text", pattern=r"(\d{1,3}(\.\d{1,2})?)?"),
    Spec("strix_sandbox_pids_limit", "Strix: песочница", "Процессов в контейнере", "0 — без ограничения.", "int", 0, 1_000_000),
    Spec("strix_sandbox_shm_size", "Strix: песочница", "Общая память (/dev/shm)", "Нужна браузеру внутри песочницы на тяжёлых страницах, например 1g. Пусто — как у Docker.", "text", pattern=_SIZE),
    Spec("strix_sandbox_log_max_size", "Strix: песочница", "Размер файла лога контейнера", "Например 50m; off — без ограничения.", "text", pattern=r"\d{1,6}[bkmgBKMG]?|off"),
    Spec("strix_sandbox_log_max_file", "Strix: песочница", "Файлов лога контейнера", "", "int", 1, 100),
    # ── Strix: модель ──
    Spec("llm_disable_streaming", "Strix: модель", "Отключить потоковые ответы", "Включите, если шлюз рвёт потоковые соединения (обрывы, 499/502 посреди ответа).", "bool"),
    Spec("strix_prompt_cache", "Strix: модель", "Кэширование запросов", "Повторяющаяся часть запроса к модели стоит дешевле. Выключайте только если шлюз его не понимает.", "bool"),
    Spec("strix_force_required_tool_choice", "Strix: модель", "Всегда требовать действие", "Для моделей, которые вместо действий отвечают текстом.", "bool"),
    Spec("llm_max_tool_calls_per_turn", "Strix: модель", "Действий за один ход", "Сколько инструментов агент может вызвать за один ответ модели. 0 — без ограничения.", "int", 0, 1000),
    Spec("llm_extra_headers", "Strix: модель", "Дополнительные HTTP-заголовки", 'JSON-объект, если шлюз требует свои заголовки: {"X-Team": "red"}. Пусто — не нужны. Видны всем администраторам.', "text", pattern=r"[\s\S]{0,4000}", check=_headers_json),
    Spec("strix_dedupe_model", "Strix: модель", "Модель для склейки дублей", "Отдельная, более дешёвая модель для объединения повторяющихся находок. Пусто — та же, что у скана.", "text", pattern=r"[A-Za-z0-9_./:@-]{0,200}"),
    Spec("strix_web_search_provider", "Strix: модель", "Веб-поиск для агентов скана", "Работает, только если в .env задан ключ PERPLEXITY_API_KEY или EXA_API_KEY.", "choice", choices=("auto", "perplexity", "exa")),
    Spec("strix_debug", "Strix: модель", "Подробный лог Strix", "Для разбора сбоев: лог скана становится намного длиннее.", "bool"),
    # ── Strix: контекст ──
    Spec("strix_context_auto_compact", "Strix: контекст", "Автосжатие истории", "Агент сам сжимает историю, когда контекст модели заполняется.", "bool"),
    Spec("strix_context_fallback_tokens", "Strix: контекст", "Окно контекста неизвестной модели", "Какой размер контекста считать у модели, которую Strix не знает (модели через свой шлюз).", "int", 1_000, 10_000_000, unit="токенов"),
    Spec("strix_tool_output_max_tokens", "Strix: контекст", "Вывод одной команды в контексте", "Меньше — экономия токенов, больше — агент видит больше.", "int", 100, 1_000_000, unit="токенов"),
    Spec("strix_max_context_images", "Strix: контекст", "Скриншотов в контексте", "", "int", 0, 100),
    # ── SSH ──
    Spec("ssh_connect_timeout_seconds", "SSH-раннеры", "Ожидание подключения", "", "int", 1, 300, unit="с"),
    Spec("ssh_check_interval_seconds", "SSH-раннеры", "Как часто проверять раннеры", "0 — не проверять.", "int", 0, 86400, unit="с", restart="beat"),
    Spec("ssh_sync_interval_seconds", "SSH-раннеры", "Как часто забирать файлы идущего скана", "Живая стоимость и лента агентов обновляются с этим шагом.", "float", 0.5, 60, unit="с"),
    Spec("ssh_fetch_max_mb", "SSH-раннеры", "Предел данных с раннера", "Сколько мегабайт забирается с удалённого прогона (защита диска от враждебного хоста).", "int", 1, 10000, unit="МБ"),
    Spec("ssh_fetch_max_files", "SSH-раннеры", "Предел файлов с раннера", "", "int", 100, 1_000_000),
    # ── Операции ──
    Spec("ops_host_timeout_seconds", "Операции", "Предел времени на хост", "Сколько секунд скрипт может выполняться на одном хосте.", "int", 1, 7200, unit="с"),
    # ── Агент ──
    Spec("agent_turn_timeout_seconds", "Агент", "Предел времени одного хода", "Один ход — одно сообщение агенту. Дольше — ход прерывается.", "int", 10, 86400, unit="с"),
    Spec("agent_tools", "Агент", "Какие инструменты видит модель", "auto: только обычные функции для шлюзов (custom, Grok) и всё, что умеет Codex, — для OpenAI. Шлюз, отвечающий "
         "«This endpoint supports function tools only», требует functions.", "choice", choices=AGENT_TOOLS),
    Spec("agent_response_fix", "Агент", "Исправлять ответы шлюза", "Некоторые шлюзы присылают итог ответа без поля cached_tokens, и Codex считает такой поток оборванным: переспрашивает "
         "до 5 раз (ответ повторяется, токены тратятся) и падает. Включено — агент ходит к шлюзу через встроенный прокси, который дописывает недостающие числа. "
         "К самому OpenAI не применяется.", "bool"),
    Spec("agent_goal_max_tokens", "Агент", "Потолок бюджета цели", "С целью агент работает сам, ход за ходом, пока не достигнет её или не потратит бюджет токенов. "
         "Больше этого числа бюджет одной цели задать нельзя.", "int", 10_000, 50_000_000, unit="токенов"),
    Spec("max_agent_sessions_per_user", "Агент", "Сессий на аккаунт", "", "int", 1, 1000),
    Spec("agent_stale_seconds", "Агент", "Ход без признаков жизни", "Если воркер хода молчит столько секунд, ход считается потерянным.", "int", 30, 86400, unit="с"),
    # ── Вход ──
    Spec("login_max_failures", "Вход", "Неудачных попыток входа", "Сколько неудач подряд с одного адреса на один email до блокировки.", "int", 1, 1000),
    Spec("login_window_seconds", "Вход", "Окно блокировки", "Сколько секунд длится блокировка после превышения.", "int", 60, 86400, unit="с"),
    Spec("jwt_expire_minutes", "Вход", "Срок сессии", "На сколько минут выдаётся вход. Действует на новые входы.", "int", 5, 43200, unit="мин"),
)

BY_KEY = {s.key: s for s in SPECS}
GROUPS = tuple(dict.fromkeys(s.group for s in SPECS))

# What each parameter is when nobody changed it in the panel: `.env` or the default. Taken once, when this module is first imported
# (before any change from the database is applied), and what "сбросить" returns to.
BASELINE: dict[str, Any] = {s.key: getattr(settings, s.key) for s in SPECS}


def parse(spec: Spec, raw: Any) -> Any:
    """The validated value of `raw` for `spec`, or `ValueError` with a message fit to show the administrator."""
    if spec.kind == "bool":
        if isinstance(raw, bool):
            value: Any = raw
        else:
            raise ValueError("нужно «да» или «нет»")
    elif spec.kind in ("int", "float"):
        if isinstance(raw, bool) or not isinstance(raw, (int, float)):
            raise ValueError("нужно число")
        if spec.kind == "int":
            if isinstance(raw, float) and not raw.is_integer():
                raise ValueError("нужно целое число")
            value = int(raw)
        else:
            value = float(raw)
        if (spec.minimum is not None and value < spec.minimum) or (spec.maximum is not None and value > spec.maximum):
            raise ValueError(f"допустимо от {spec.minimum:g} до {spec.maximum:g}")
    elif spec.kind == "choice":
        value = raw.strip().lower() if isinstance(raw, str) else raw
        if value not in spec.choices:
            raise ValueError(f"допустимо: {', '.join(spec.choices)}")
    else:  # text
        value = raw.strip() if isinstance(raw, str) else raw
        if not isinstance(value, str) or not re.fullmatch(spec.pattern, value):
            raise ValueError("недопустимые символы или слишком длинное значение")
    if spec.check is not None:
        spec.check(value)
    return value


def load_overrides(db: Session) -> dict[str, Any]:
    """The stored changes that are still valid. A row for a parameter that no longer exists, or whose value no longer
    passes validation (a limit was tightened in a newer version), is ignored: the baseline applies."""
    from app.models.app_setting import AppSetting

    out: dict[str, Any] = {}
    for row in db.scalars(select(AppSetting)):
        spec = BY_KEY.get(row.key)
        if spec is None:
            continue
        try:
            out[row.key] = parse(spec, json.loads(row.value))
        except ValueError:
            log.warning("ignoring the stored value of %s: it is not valid any more", row.key)
    return out


def apply(overrides: dict[str, Any]) -> None:
    """Make `settings` hold baseline-plus-these. A parameter not in `overrides` goes back to its baseline."""
    for key, baseline in BASELINE.items():
        setattr(settings, key, overrides.get(key, baseline))


def refresh() -> bool:
    """Reload the table and apply it. False (nothing changed) when the database is not reachable or not migrated yet."""
    from app.core.db import SessionLocal

    try:
        with SessionLocal() as db:
            overrides = load_overrides(db)
    except Exception as exc:  # before the first migration, or the database is briefly away: keep what we have
        log.debug("settings refresh skipped: %s", exc)
        return False
    apply(overrides)
    return True


_refresher: threading.Thread | None = None
_refresher_lock = threading.Lock()


def start_refresher(interval: float = REFRESH_SECONDS) -> None:
    """Keep this process in step with the table, in the background. Safe to call more than once; a forked child starts its own."""
    global _refresher
    with _refresher_lock:
        if _refresher is not None and _refresher.is_alive():
            return

        def loop() -> None:
            stop = threading.Event()
            while not stop.wait(0 if loop.first else interval):
                loop.first = False
                refresh()

        loop.first = True  # type: ignore[attr-defined]
        _refresher = threading.Thread(target=loop, name="runtime-settings", daemon=True)
        _refresher.start()

"""ПУЛЬС — пингер сервисов.

Читает services.json, проверяет доступность каждого сервиса HTTP-запросом,
пишет результат в status.json. Запускается по cron в GitHub Actions
(или локально: python check.py).

Только стандартная библиотека — никаких зависимостей, чтобы GitHub Action
не ставил пакеты.
"""

from __future__ import annotations

import json
import ssl
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

# Порог в секундах: медленнее — считаем "тормозит", но живой.
SLOW_THRESHOLD_S: float = 5.0
# Таймаут запроса: дольше — считаем "лежит".
TIMEOUT_S: float = 15.0
# Браузерный User-Agent: часть сервисов режет запросы без него.
USER_AGENT: str = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)

BASE_DIR: Path = Path(__file__).resolve().parent

# Не проверяем валидность TLS-сертификата: нам важна ДОСТУПНОСТЬ сервера
# (лёг/работает), а не корректность его цепочки. У Python на Windows часто нет
# корневых сертификатов НУЦ Минцифры, которыми подписаны РФ-сайты, — без этого
# живой Сбер/ВК/ВТБ ложно детектились бы как "down".
_SSL_CONTEXT = ssl.create_default_context()
_SSL_CONTEXT.check_hostname = False
_SSL_CONTEXT.verify_mode = ssl.CERT_NONE


def check_one(service: dict[str, str]) -> dict[str, object]:
    """Проверяет один сервис. Возвращает статус: up / slow / down."""
    url = service["url"]
    request = urllib.request.Request(url, method="GET", headers={"User-Agent": USER_AGENT})
    started = time.monotonic()
    status = "down"
    http_code: int | None = None
    error: str | None = None

    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_S, context=_SSL_CONTEXT) as response:
            http_code = response.status
            elapsed = time.monotonic() - started
            if http_code < 400:
                status = "slow" if elapsed > SLOW_THRESHOLD_S else "up"
            else:
                status = "down"
    except urllib.error.HTTPError as exc:
        # Сервер ответил, но кодом ошибки. 4xx (напр. 403/429 антибот) считаем "живой,
        # но огрызается" -> slow, а не down; 5xx -> реально лежит.
        http_code = exc.code
        elapsed = time.monotonic() - started
        status = "down" if exc.code >= 500 else "slow"
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        elapsed = time.monotonic() - started
        error = str(exc)
        status = "down"

    return {
        "id": service["id"],
        "name": service["name"],
        "category": service["category"],
        "status": status,
        "http_code": http_code,
        "response_ms": round(elapsed * 1000),
        "error": error,
    }


def main() -> None:
    services: list[dict[str, str]] = json.loads((BASE_DIR / "services.json").read_text(encoding="utf-8"))

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(check_one, services))

    results.sort(key=lambda r: (r["category"], r["name"]))

    up = sum(1 for r in results if r["status"] == "up")
    slow = sum(1 for r in results if r["status"] == "slow")
    down = sum(1 for r in results if r["status"] == "down")

    payload = {
        "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "summary": {"total": len(results), "up": up, "slow": slow, "down": down},
        "services": results,
    }

    (BASE_DIR / "status.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"Проверено {len(results)}: up={up} slow={slow} down={down}")


if __name__ == "__main__":
    main()

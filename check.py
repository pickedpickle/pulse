"""ПУЛЬС — пингер сервисов.

Читает services.json, проверяет доступность каждого сервиса и пишет
результат в status.json. Запускается по cron в GitHub Actions
(или локально: python check.py).

Только стандартная библиотека — никаких зависимостей, чтобы GitHub Action
не ставил пакеты.

Логика статуса (важно — почему так):
  Пингер бежит с облачного IP (GitHub Actions, США). РФ-сервисы часто режут
  чужие/облачные IP: рвут HTTP-запрос, отдают антибот-код или капчу. Наивно
  это выглядит как "сайт лежит", хотя сайт жив. Чтобы не врать красным:
    1. Сначала обычный HTTP-запрос.
    2. Если HTTP упал (reset/timeout/5xx) — проверяем TCP-коннект к :443.
       TCP встал -> сервер ЖИВ по сети, значит это антибот на HTTP-уровне -> slow.
       TCP не встал (refused/timeout) -> сервер реально недоступен -> down.
  Так "лёг" остаётся честным сигналом, а не срабатывает на каждом антиботе.
"""

from __future__ import annotations

import json
import socket
import ssl
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

# Порог в секундах: медленнее — считаем "тормозит", но живой.
SLOW_THRESHOLD_S: float = 5.0
# Таймаут HTTP-запроса: дольше — считаем неответившим и уходим в TCP-проверку.
TIMEOUT_S: float = 15.0
# Таймаут TCP-коннекта к :443 (ground truth "сервер жив по сети").
TCP_TIMEOUT_S: float = 8.0
# Сколько раз повторить при сетевой ошибке (гасит случайные разрывы).
RETRIES: int = 1
# Браузерный User-Agent: часть сервисов режет запросы без него.
USER_AGENT: str = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)

BASE_DIR: Path = Path(__file__).resolve().parent

# Не проверяем валидность TLS-сертификата: нам важна ДОСТУПНОСТЬ сервера
# (лёг/работает), а не корректность его цепочки. У Python часто нет корневых
# сертификатов НУЦ Минцифры, которыми подписаны РФ-сайты, — без этого живой
# Сбер/ВК/ВТБ ложно детектились бы как "down".
_SSL_CONTEXT = ssl.create_default_context()
_SSL_CONTEXT.check_hostname = False
_SSL_CONTEXT.verify_mode = ssl.CERT_NONE


def tcp_alive(host: str, port: int = 443) -> bool:
    """True, если TCP-коннект к host:port устанавливается.

    Это сетевой ground truth: если рукопожатие TCP прошло, сервер жив и
    отвечает, а любые проблемы поверх (reset HTTP, антибот, 5xx) — не "лёг".
    """
    try:
        with socket.create_connection((host, port), timeout=TCP_TIMEOUT_S):
            return True
    except OSError:
        return False


def http_probe(url: str) -> tuple[str, int | None, float, str | None]:
    """Один HTTP-запрос. Возвращает (исход, http_code, elapsed_s, error).

    Исход: "ok" (2xx/3xx), "antibot" (4xx), "server_error" (5xx),
    "neterror" (сеть не дала ответа).
    """
    request = urllib.request.Request(url, method="GET", headers={"User-Agent": USER_AGENT})
    started = time.monotonic()
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_S, context=_SSL_CONTEXT) as response:
            elapsed = time.monotonic() - started
            code = response.status
            return "ok" if code < 400 else "server_error", code, elapsed, None
    except urllib.error.HTTPError as exc:
        elapsed = time.monotonic() - started
        # Сервер ответил кодом ошибки. 4xx (403/429/498 антибот) — жив, огрызается.
        outcome = "server_error" if exc.code >= 500 else "antibot"
        return outcome, exc.code, elapsed, None
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        elapsed = time.monotonic() - started
        return "neterror", None, elapsed, str(exc)


def check_one(service: dict[str, str]) -> dict[str, object]:
    """Проверяет один сервис. Возвращает статус: up / slow / down."""
    url = service["url"]
    host = urlparse(url).hostname or ""

    outcome, http_code, elapsed, error = http_probe(url)
    for _ in range(RETRIES):
        if outcome != "neterror":
            break
        time.sleep(1.0)
        outcome, http_code, elapsed, error = http_probe(url)

    if outcome == "ok":
        status = "slow" if elapsed > SLOW_THRESHOLD_S else "up"
    elif outcome == "antibot":
        # Сервер ответил (пусть и антибот-кодом) — точно жив.
        status = "slow"
    else:
        # server_error или neterror: HTTP не удался. Спрашиваем TCP — жив ли сервер
        # по сети. Встал коннект -> антибот/сбой поверх, но сервер ЕСТЬ -> slow.
        # Не встал -> реально недоступен -> down.
        status = "slow" if host and tcp_alive(host) else "down"

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

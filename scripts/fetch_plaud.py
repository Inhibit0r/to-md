"""Скачивает mp3 с Plaud в in/ — дальше работает скилл /to-md:convert.

Официальный developer API (platform.plaud.ai), два эндпоинта: список файлов и деталь с presigned_url.
Транскрипцию Plaud не трогаем: квота 300 мин/мес не расходуется.

    python fetch_plaud.py --latest 3
    python fetch_plaud.py --id <file_id>
    python fetch_plaud.py --latest 5 --out D:\\clips

Авторизация: OAuth-токены из ~/.plaud/tokens.json, которые кладёт `plaud login` (npm i -g @plaud-ai/cli).
Протух access_token — обновление делегируем их же CLI, он умеет это штатно через refresh_token.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

import requests

INVALID_FILENAME_CHARS = re.compile(r'[\\/:*?"<>|]+')


def ensure_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


def sanitize_filename(value: str, max_length: int = 180) -> str:
    cleaned = INVALID_FILENAME_CHARS.sub("-", (value or "").strip())
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" .")
    if len(cleaned) > max_length:
        cleaned = cleaned[:max_length].rstrip(" .")
    return cleaned or "untitled"


BASE = "https://platform.plaud.ai/developer/api"
TOKENS = Path.home() / ".plaud" / "tokens.json"
# Относительно текущей рабочей директории: плагин не знает, где лежит проект пользователя.
DEFAULT_OUT = Path("in")


def auth_header() -> str:
    if not TOKENS.exists():
        raise RuntimeError(
            "Нет ~/.plaud/tokens.json. Выполни: npm i -g @plaud-ai/cli && plaud login"
        )
    tokens = json.loads(TOKENS.read_text(encoding="utf-8"))
    # expires_at в миллисекундах; за 5 минут до истечения просим их CLI обновить токен.
    if int(tokens.get("expires_at") or 0) - 300_000 < time.time() * 1000:
        cli = shutil.which("plaud")
        if not cli:
            raise RuntimeError(
                "access_token истёк, а CLI `plaud` не найден в PATH. Выполни: plaud login"
            )
        subprocess.run([cli, "me"], capture_output=True, timeout=120)
        tokens = json.loads(TOKENS.read_text(encoding="utf-8"))
    return f"{tokens.get('token_type') or 'Bearer'} {tokens['access_token']}"


def api_get(path: str, **params: Any) -> Any:
    """Список приходит как {'data': [...]}, деталь файла — плоским объектом. Разворачиваем только обёрнутое."""
    response = requests.get(
        f"{BASE}{path}",
        params=params,
        headers={"Authorization": auth_header()},
        timeout=60,
    )
    if response.status_code == 401:
        raise RuntimeError("401 от Plaud — выполни `plaud login` заново.")
    response.raise_for_status()
    payload = response.json()
    if isinstance(payload, dict) and "data" in payload:
        return payload["data"]
    return payload


def audio_filename(item: dict[str, Any]) -> str:
    date = str(item.get("start_at") or item.get("created_at") or "unknown-date")[:10]
    title = sanitize_filename(item.get("name") or "untitled", max_length=100)
    return f"{date}__{title}__{item.get('id')}.mp3"


def human_duration(ms: Any) -> str:
    seconds = int(ms or 0) // 1000
    return f"{seconds // 60}:{seconds % 60:02d}"


def download(url: str, target: Path) -> int:
    # Presigned S3: без наших заголовков, подпись выписана под голый GET.
    with requests.get(url, timeout=600, stream=True) as response:
        response.raise_for_status()
        with target.open("wb") as handle:
            for chunk in response.iter_content(chunk_size=1 << 20):
                handle.write(chunk)
    return target.stat().st_size


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Скачать mp3 с Plaud для локальной транскрипции"
    )
    parser.add_argument("--latest", type=int, help="сколько последних записей забрать")
    parser.add_argument("--id", help="конкретный file_id")
    parser.add_argument(
        "--out",
        type=Path,
        default=DEFAULT_OUT,
        help=f"куда класть (по умолчанию {DEFAULT_OUT})",
    )
    parser.add_argument(
        "--refresh", action="store_true", help="перекачать, даже если файл уже есть"
    )
    parser.add_argument(
        "--selftest",
        action="store_true",
        help="проверка сборки имени и длительности, без сети",
    )
    args = parser.parse_args()

    if args.selftest:
        name = audio_filename(
            {
                "start_at": "2026-08-05T13:24:40",
                "name": "Созвон: план/итоги",
                "id": "abc123",
            }
        )
        assert name.startswith("2026-08-05__") and name.endswith("__abc123.mp3"), name
        assert "/" not in name and ":" not in name, name
        assert human_duration(2_420_000) == "40:20", human_duration(2_420_000)
        print("ok")
        return 0

    if not args.latest and not args.id:
        parser.error("нужен --latest N или --id <file_id>")

    if args.id:
        selected = [api_get(f"/open/third-party/files/{args.id}")]
    else:
        selected = api_get(
            "/open/third-party/files/", page=1, page_size=max(10, args.latest)
        )[: args.latest]

    out_dir = ensure_dir(args.out)
    for item in selected:
        target = out_dir / audio_filename(item)
        if target.exists() and not args.refresh:
            print(f"= {target.name} (уже есть)")
            continue
        detail = api_get(f"/open/third-party/files/{item['id']}")
        url = detail.get("presigned_url")
        if not url:
            print(
                f"! {item.get('name')}: аудио недоступно (запись не синхронизирована с устройства)"
            )
            continue
        size = download(url, target)
        print(
            f"+ {target.name} [{human_duration(item.get('duration'))}] {size / 1_048_576:.1f} MB"
        )

    print(f"\nГотово. Дальше: /to-md:convert на папку {out_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

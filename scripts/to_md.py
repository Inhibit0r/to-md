"""to-md: один вход для медиа, документов и фотографий.

  to_md.py <in> --out <out> [--json]     основной прогон; --json отдаёт план батчей фото
  to_md.py --doctor [--data <dir>]       JSON о среде
  to_md.py --escalate <out/raw>          файлы с метками [неразборчиво] от порога
  to_md.py --collect <out> [--in <dir>]  пересборка сводного файла и реестра
  to_md.py --selftest                    самопроверка

Ручки основного прогона: --data, --lang, --hotwords, --device, --gap, --vad.
"""

import collections
import datetime
import hashlib
import json
import os
import pathlib
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import time

# ponytail: жёсткий потолок абзаца, чтобы монолог без пауз не стал простынёй
MAX_CHARS = 600

# Начальные значения, поставленные до первой реальной пачки; меняются здесь, а не в промте.
BATCH = 5  # снимков одному субагенту
FAN_OUT_MIN = 5  # с этого числа снимков зовём субагентов
WAVE = 6  # батчей в одной волне
WAVE_FROM = 31  # с этого числа снимков идём волнами
ESCALATE_MARKS = 2  # столько меток отправляют файл на второй проход
MARK = "[неразборчиво]"

# Детектор порчи расшифровки. Пороги откалиброваны на реальных записях
# 07-30..08-24: петли «Угу.»×14 и «Корректор А.Кулакова»×19, титры из
# обучающих данных whisper, хвост, схлопнутый VAD в сегмент 60 с.
LOOP_RUN = 6  # столько подряд одинаковых сегментов — петля, а не диалог
STALL_SEC, STALL_CHARS = 30, 50  # сегмент длиннее и при этом почти пустой — провал
GAP_SEC = 60  # дыра между соседними сегментами
TITLE_ARTIFACTS = (  # титры, вшитые в обучающие данные whisper; список конечный
    "субтитр",
    "dimatorzok",
    "продолжение следует",
    "спасибо за просмотр",
    " корректор а ",
    " редактор а ",
)

MEDIA_EXT = {".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v", ".mp3", ".wav", ".m4a"}
DOC_EXT = {
    ".pdf",
    ".doc",
    ".docx",
    ".docm",
    ".ppt",
    ".pps",
    ".pot",
    ".pptx",
    ".pptm",
    ".ppsx",
    ".ppsm",
    ".xls",
    ".xlsx",
    ".xlsm",
    ".xlsb",
    ".odt",
    ".ods",
    ".odp",
    ".rtf",
    ".epub",
    ".csv",
}
PHOTO_EXT = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tif", ".tiff", ".heic"}
TEXT_EXT = {".txt", ".md", ".html", ".htm", ".json", ".xml", ".yaml", ".yml"}


def paragraphs(segments, gap):
    """[(секунда начала, текст)] — время нужно как якорь для пунктов summary."""
    out, cur, cur_start, prev_end = [], [], None, None
    for seg in segments:
        text = (seg.get("text") or "").strip()
        if not text:
            continue
        long_enough = sum(len(t) + 1 for t in cur) > MAX_CHARS
        if cur and (seg["start"] - prev_end >= gap or long_enough):
            out.append((cur_start, " ".join(cur)))
            cur, cur_start = [], None
        if cur_start is None:
            cur_start = seg["start"]
        cur.append(text)
        prev_end = seg["end"]
    if cur:
        out.append((cur_start, " ".join(cur)))
    return out


def mmss(seconds):
    m, s = divmod(int(round(seconds)), 60)
    h, m = divmod(m, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def norm_text(text):
    return re.sub(r"[^\w]+", " ", (text or "").lower()).strip()


def suspect(segments, hotwords=""):
    """Признаки испорченной расшифровки — список причин с якорями [MM:SS]."""
    segs = [s for s in segments if (s.get("text") or "").strip()]
    reasons, seen_art, seen_echo = [], set(), False
    hw = norm_text(hotwords)
    run_text, run_len, run_start = None, 0, 0.0
    prev_end = None

    def flush_run():
        if run_len >= LOOP_RUN:
            reasons.append(f"петля «{run_text}» ×{run_len} с [{mmss(run_start)}]")

    for s in segs:
        t = norm_text(s["text"])
        padded = f" {t} "
        for a in TITLE_ARTIFACTS:
            if a in padded and a not in seen_art:
                seen_art.add(a)
                reasons.append(
                    f"титры-артефакт «{s['text'].strip()}» на [{mmss(s['start'])}]"
                )
                break
        if hw and hw in t and not seen_echo:
            seen_echo = True
            reasons.append(f"эхо hotwords на [{mmss(s['start'])}]")
        if t == run_text:
            run_len += 1
        else:
            flush_run()
            run_text, run_len, run_start = t, 1, s["start"]
        if s["end"] - s["start"] > STALL_SEC and len(t) < STALL_CHARS:
            reasons.append(
                f"провал: {int(s['end'] - s['start'])} с почти без текста на [{mmss(s['start'])}]"
            )
        if prev_end is not None and s["start"] - prev_end > GAP_SEC:
            reasons.append(
                f"дыра {int(s['start'] - prev_end)} с после [{mmss(prev_end)}]"
            )
        prev_end = s["end"]
    flush_run()
    return reasons[:8]  # ponytail: хвост причин решению уже ничего не добавляет


def add_banner(md_path, reasons, alt_name):
    """Пометка о подозрении — в тело md: фронтматтер остаётся плоским."""
    text = md_path.read_text(encoding="utf-8")
    tail = (
        f" Рядом альтернативный прогон {alt_name} — сведи и удали его."
        if alt_name
        else ""
    )
    banner = "> Подозрение на потери: " + "; ".join(reasons) + "." + tail + "\n\n"
    i = text.index("\n---\n\n") + len("\n---\n\n")
    md_path.write_text(text[:i] + banner + text[i:], encoding="utf-8")


PLAUD_TOKENS = pathlib.Path.home() / ".plaud" / "tokens.json"


def plaud_token_fresh(path=PLAUD_TOKENS):
    """None — логина не было или файл неожиданной формы; False — протух; True — живой."""
    try:
        tokens = json.loads(pathlib.Path(path).read_text(encoding="utf-8"))
        return int(tokens.get("expires_at") or 0) - 300_000 > time.time() * 1000
    except (OSError, ValueError, TypeError, AttributeError):
        return None


EXT_KIND = {
    ext: kind
    for kind, exts in (
        ("media", MEDIA_EXT),
        ("doc", DOC_EXT),
        ("photo", PHOTO_EXT),
        ("text", TEXT_EXT),
    )
    for ext in exts
}


def classify(path):
    """Ветка определяется расширением — это словарь, а не суждение модели."""
    return EXT_KIND.get(pathlib.Path(path).suffix.lower())


def digest(path):
    """Ключ реестра — содержимое: переименованный файл не пересчитывается."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def load_registry(out):
    p = pathlib.Path(out) / ".state.json"
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}


def save_registry(out, reg):
    p = pathlib.Path(out) / ".state.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(reg, ensure_ascii=False, indent=1), encoding="utf-8")


def done(reg, key, out):
    """Запись живая, только пока её md лежит на диске: стёр md — переделается."""
    rec = reg.get(key)
    return bool(rec) and (pathlib.Path(out) / rec["md"]).exists()


def plan_photos(paths):
    """Сколько субагентов и по сколько снимков — считает скрипт, спавнит скилл."""
    paths = list(paths)
    if not paths:
        return {"mode": "inline", "batches": [], "wave_size": 0}
    if len(paths) < FAN_OUT_MIN:
        return {"mode": "inline", "batches": [paths], "wave_size": 0}
    batches = [paths[i : i + BATCH] for i in range(0, len(paths), BATCH)]
    waves = len(paths) >= WAVE_FROM
    return {
        "mode": "waves" if waves else "parallel",
        "batches": batches,
        "wave_size": WAVE if waves else 0,
    }


SOURCE_RE = re.compile(r'^source:\s*"?(.+?)"?\s*$', re.M)


def source_of(text):
    m = SOURCE_RE.search(text)
    return m.group(1) if m else ""


def is_photo_md(text):
    """Склейка и эскалация касаются только фото: расшифровки и документы мимо."""
    return pathlib.Path(source_of(text)).suffix.lower() in PHOTO_EXT


def strip_head(text):
    """Тело без фронтматтера. Пустая строка-разделитель уходит вместе с ним, иначе склейка разъедется."""
    body = text.split("---\n", 2)[2] if text.startswith("---\n") else text
    return body.lstrip("\n")


def strip_omissions(text):
    """Список «не перенесено» — аудит пофайлового md; в сводный файл он не идёт."""
    lines = text.rstrip().splitlines()
    while lines and (
        lines[-1].startswith("> Не перенесено:") or lines[-1].strip() in ("", "---")
    ):
        lines.pop()
    return "\n".join(lines).rstrip()


def escalate(rawdir):
    hot = []
    for md in sorted(pathlib.Path(rawdir).glob("*.md")):
        text = md.read_text(encoding="utf-8")
        if is_photo_md(text) and text.count(MARK) >= ESCALATE_MARKS:
            hot.append(str(md))
    return hot


def collect(out, indir="in"):
    """Сводный файл пересобирается целиком: нет дубля, нет расхождения, порядок предсказуем."""
    out, indir = pathlib.Path(out), pathlib.Path(indir)
    parts, by_source = [], {}
    for md in sorted((out / "raw").glob("*.md")):
        text = md.read_text(encoding="utf-8")
        if not is_photo_md(text):
            continue
        by_source[source_of(text)] = md.name
        parts.append(strip_omissions(strip_head(text)))
    (out / "задачи.md").write_text("\n\n".join(parts) + "\n", encoding="utf-8")

    reg = {k: v for k, v in load_registry(out).items() if (out / v["md"]).exists()}
    if indir.is_dir():
        for f in sorted(indir.iterdir()):
            if f.is_file() and classify(f) == "photo" and f.name in by_source:
                reg[digest(f)] = {
                    "src": f.name,
                    "md": f"raw/{by_source[f.name]}",
                    "at": datetime.date.today().isoformat(),
                }
    save_registry(out, reg)
    return len(parts)


def md_names(files):
    """Один входной файл — один md. Основы совпали (отчёт.pdf и отчёт.docx) — расширение остаётся в имени.

    Считается по содержимому папки, без обращения к реестру: одна и та же папка
    всегда даёт одни и те же имена.
    """
    counts = collections.Counter(f.stem for f in files)
    return {
        f: f"{f.stem}.md"
        if counts[f.stem] == 1
        else f"{f.stem}-{f.suffix.lstrip('.').lower()}.md"
        for f in files
    }


def resolve_data(arg):
    """${CLAUDE_PLUGIN_DATA} может не подставиться — тогда веса живут в ~/.to-md."""
    if arg and "${" not in str(arg):
        return pathlib.Path(arg)
    return pathlib.Path.home() / ".to-md"


def doctor(data):
    """Что установщику надо знать о машине. Истина о среде — сама среда, не файл-маркер."""
    import importlib.util

    def have(mod):
        try:
            return importlib.util.find_spec(mod) is not None
        except (ImportError, ValueError):
            return False

    data = pathlib.Path(data)
    whisper = shutil.which("whisper-ctranslate2") or ""
    anydoc_ok = have("anydoc")
    python_ok = sys.version_info >= (3, 10)
    return {
        "os": platform.system(),
        "arch": platform.machine(),
        "python": platform.python_version(),
        "python_ok": python_ok,
        "whisper": whisper,
        "anydoc": anydoc_ok,
        "cuda_pkgs": have("nvidia.cublas") and have("nvidia.cudnn"),
        # requests нужен только загрузчику Plaud, поэтому в ready не входит.
        # Транзитивно он не приезжает: huggingface-hub требует его лишь под extra gradio.
        "plaud": have("requests"),
        "plaud_token": plaud_token_fresh(),
        "nvidia": bool(shutil.which("nvidia-smi")),
        "data": str(data),
        "weights": (data / "models" / "large-v3-turbo" / "model.bin").exists(),
        "ready": bool(whisper) and anydoc_ok and python_ok,
    }


def pick_device(requested):
    if requested != "auto":
        return requested
    if platform.system() == "Darwin":
        return "cpu"  # у CTranslate2 нет бэкенда Metal, видеокарту Apple он не видит
    return "cuda" if shutil.which("nvidia-smi") else "cpu"


def cuda_dlls():
    """ctranslate2 ищет cublas и cudnn на PATH, а pip кладёт их внутрь пакетов nvidia-*."""
    try:
        import nvidia
    except ImportError:
        return
    for sub in pathlib.Path(list(nvidia.__path__)[0]).iterdir():
        for name in ("bin", "lib"):
            d = sub / name
            if d.is_dir():
                os.environ["PATH"] = f"{d}{os.pathsep}{os.environ['PATH']}"


def media_duration(path):
    import av  # локально: нужен только здесь

    try:
        with av.open(str(path)) as container:
            if container.duration:
                return container.duration / 1_000_000
    except Exception:
        pass
    return 0.0


def head(source_path, **fields):
    """Фронтматтер: source и date общие, остальное — по типу материала."""
    path = pathlib.Path(source_path)
    lines = [
        "---",
        f"source: {json.dumps(path.name, ensure_ascii=False)}",
        f"date: {datetime.date.fromtimestamp(path.stat().st_mtime).isoformat()}",
    ]
    lines += [f"{k}: {v}" for k, v in fields.items()]
    return "\n".join(lines + ["---", "", ""])


def build(result, media, gap):
    paras = paragraphs(result.get("segments", []), gap)
    seg_end = max((s["end"] for s in result.get("segments", [])), default=0.0)
    duration = media_duration(media) or seg_end
    body = "\n\n".join(f"[{mmss(start)}] {text}" for start, text in paras)
    return head(media, duration=json.dumps(mmss(duration))) + body + "\n"


def is_readable_media(path):
    """Битый файл роняет всю партию whisper, поэтому отсеиваем до вызова."""
    import av  # локально: нужен только здесь

    try:
        with av.open(str(path)) as container:
            return bool(container.streams.audio)
    except Exception:
        return False


def build_doc(source_path, text):
    """Документ от anydoc: тот же фронтматтер, что у медиа, но без длительности."""
    text = text.strip()
    return head(source_path, chars=len(text)) + text + "\n"


def run_whisper(
    files, names, rawdir, data, lang, hotwords, device, gap, vad=True, no_speech=None
):
    """Партия уходит одной командой; md собирается здесь же из json, json удаляется."""
    model_dir = pathlib.Path(data) / "models" / "large-v3-turbo"
    if (model_dir / "model.bin").exists():
        model_args = ["--model_directory", str(model_dir)]
    else:
        # Первая загрузка идёт в HuggingFace: их xet-загрузчик виснет, хаб доступен не отовсюду.
        os.environ["HF_HUB_DISABLE_XET"] = "1"
        proxy = os.environ.get("TO_MD_PROXY")
        if proxy and not os.environ.get("HTTPS_PROXY"):
            os.environ["HTTPS_PROXY"] = os.environ["HTTP_PROXY"] = proxy
        model_args = ["--model", "large-v3-turbo"]

    if device == "cuda":
        cuda_dlls()
    cmd = [
        "whisper-ctranslate2",
        *model_args,
        *(["--hotwords", hotwords] if hotwords else []),
        # Перенос контекста между окнами каскадит повторы («Угу.»×20,
        # «Корректор А.Кулакова»×19): с выключенным не было петель ни в одном прогоне.
        "--condition_on_previous_text",
        "False",
        "--language",
        lang,
        "--vad_filter",
        str(bool(vad)),
        *(["--no_speech_threshold", str(no_speech)] if no_speech is not None else []),
        "--output_format",
        "json",
        "--output_dir",
        str(rawdir),
        "--device",
        device,
        *[str(f) for f in files],
    ]
    # Прогресс whisper — в stderr: --json оставляет stdout машинно-читаемым.
    # utf-8 в окружении — иначе Windows-консоль отдаёт кракозябры.
    env = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"}
    code = subprocess.call(cmd, env=env, stdout=sys.stderr)
    if code and device == "cuda":
        print("CUDA не поднялась, повтор на процессоре", file=sys.stderr)
        cmd[cmd.index("--device") + 1] = "cpu"
        code = subprocess.call(cmd, env=env, stdout=sys.stderr)
    if code:
        return []

    written = []
    for f in files:
        js = pathlib.Path(rawdir) / (f.stem + ".json")
        if not js.exists():
            print(f"! {f.name}: whisper не отдал json", file=sys.stderr)
            continue
        result = json.loads(js.read_text(encoding="utf-8"))
        md = pathlib.Path(rawdir) / names[f]
        md.write_text(build(result, f, gap), encoding="utf-8")
        js.unlink()
        written.append((f, md, suspect(result.get("segments", []), hotwords)))
    return written


def dispatch(
    indir, out, data, lang="ru", hotwords="", device="auto", gap=0.7, vad=True
):
    indir, out = pathlib.Path(indir), pathlib.Path(out)
    raw = out / "raw"
    raw.mkdir(parents=True, exist_ok=True)
    files = (
        [indir]
        if indir.is_file()
        else sorted(p for p in indir.iterdir() if p.is_file())
    )
    names = md_names(files)

    reg = load_registry(out)
    report = {"converted": [], "skipped": [], "cached": [], "text": [], "photos": {}}
    todo = {"media": [], "doc": [], "photo": []}

    for f in files:
        kind = classify(f)
        if kind is None:
            continue
        if kind == "text":
            report["text"].append(f.name)
            continue
        key = digest(f)
        if done(reg, key, out):
            report["cached"].append(f.name)
            continue
        if kind == "media" and not is_readable_media(f):
            report["skipped"].append(
                {"src": f.name, "why": "звуковой дорожки нет или файл битый"}
            )
            continue
        todo[kind].append((f, key))

    for f, key in todo["doc"]:
        text, why = convert_doc(f)
        if text is None:
            report["skipped"].append({"src": f.name, "why": why})
            continue
        md = raw / names[f]
        md.write_text(build_doc(f, text), encoding="utf-8")
        reg[key] = {
            "src": f.name,
            "md": f"raw/{md.name}",
            "at": datetime.date.today().isoformat(),
        }
        report["converted"].append(
            {"src": f.name, "md": f"raw/{md.name}", "kind": "doc"}
        )

    if todo["media"]:
        keys = {f: k for f, k in todo["media"]}
        results = run_whisper(
            [f for f, _ in todo["media"]],
            names,
            raw,
            data,
            lang,
            hotwords,
            pick_device(device),
            gap,
            vad=vad,
        )
        # Подозрение на потери → один alt-прогон другим профилем (без VAD и
        # hotwords): профили теряют разное, сведение остаётся за оператором.
        suspects = [f for f, _, why in results if why]
        alt_names = {f: f"{pathlib.Path(names[f]).stem}-alt.md" for f in suspects}
        alts = (
            {
                f: md
                for f, md, _ in run_whisper(
                    suspects,
                    alt_names,
                    raw,
                    data,
                    lang,
                    "",
                    pick_device(device),
                    gap,
                    vad=False,
                    no_speech=0.3,
                )
            }
            if suspects
            else {}
        )
        for f, md, why in results:
            reg[keys[f]] = {
                "src": f.name,
                "md": f"raw/{md.name}",
                "at": datetime.date.today().isoformat(),
            }
            item = {"src": f.name, "md": f"raw/{md.name}", "kind": "media"}
            if why:
                item["suspect"] = why
                alt = alts.get(f)
                if alt is not None:
                    item["alt"] = f"raw/{alt.name}"
                add_banner(md, why, alt.name if alt is not None else None)
            report["converted"].append(item)

    # По фото скрипт не считает ничего: отдаёт план, работу делают субагенты.
    report["photos"] = plan_photos([str(f) for f, _ in todo["photo"]])
    save_registry(out, reg)
    return report


def convert_doc(path):
    """anydoc называет сбой типом исключения: ветвимся по классу, а не по числу символов."""
    import anydoc  # локально: тяжёлое расширение нужно только здесь

    path = pathlib.Path(path)
    try:
        return anydoc.to_markdown(str(path)), ""
    except anydoc.EncryptedError:
        return None, "файл под паролем"
    except anydoc.UnsupportedError:
        if path.suffix.lower() == ".pdf":
            return None, "текста нет, вероятно скан"
        return None, "формат не поддерживается"
    except anydoc.ConvertError as e:
        return None, f"{type(e).__name__}: {e}"
    except OSError as e:
        return None, f"файл не читается: {e}"


def selftest():
    segs = [
        {"start": 0.0, "end": 2.0, "text": " Первая фраза."},
        {"start": 2.1, "end": 4.0, "text": " Вторая рядом."},
        {"start": 65.0, "end": 68.0, "text": " После паузы."},
        {"start": 68.0, "end": 68.5, "text": "  "},
    ]
    got = paragraphs(segs, gap=0.7)
    assert got == [(0.0, "Первая фраза. Вторая рядом."), (65.0, "После паузы.")], got
    assert mmss(65) == "01:05" and mmss(3725) == "1:02:05"
    body = "\n\n".join(f"[{mmss(start)}] {text}" for start, text in got)
    assert body.startswith("[00:00] Первая фраза."), body
    assert "[01:05] После паузы." in body, body
    doc = build_doc(__file__, "Первый абзац.\n\nВторой абзац.")
    assert doc.startswith("---\n"), doc[:40]
    assert "chars: 28" in doc, doc[:200]
    assert doc.rstrip().endswith("Второй абзац."), doc[-40:]

    assert classify("a.MP4") == "media" and classify("b.docm") == "doc"
    assert classify("c.HEIC") == "photo" and classify("d.yaml") == "text"
    assert classify("e.zip") is None

    # Пороги веера: 4 читает основная сессия, 5 зовут субагентов, 31 идёт волнами.
    names = [f"{i}.jpg" for i in range(40)]
    assert plan_photos(names[:4])["mode"] == "inline"
    assert plan_photos(names[:4])["batches"] == [names[:4]]
    assert plan_photos(names[:5])["mode"] == "parallel"
    assert plan_photos(names[:30])["batches"][0] == names[:5]
    assert len(plan_photos(names[:30])["batches"]) == 6
    assert plan_photos(names[:31])["mode"] == "waves"
    assert plan_photos(names[:31])["wave_size"] == 6
    assert len(plan_photos(names[:31])["batches"]) == 7
    assert plan_photos([]) == {"mode": "inline", "batches": [], "wave_size": 0}

    with tempfile.TemporaryDirectory() as tmp:
        tmp = pathlib.Path(tmp)
        src = tmp / "IMG.jpg"
        src.write_bytes(b"one")
        key = digest(src)
        assert key == digest(src) and len(key) == 64
        (tmp / "raw").mkdir()
        reg = {key: {"src": "IMG.jpg", "md": "raw/IMG.md", "at": "2026-08-14"}}
        # Реестр самолечащийся: нет md на диске — запись протухла.
        assert done(reg, key, tmp) is False
        (tmp / "raw" / "IMG.md").write_text("x", encoding="utf-8")
        assert done(reg, key, tmp) is True
        save_registry(tmp, reg)
        assert load_registry(tmp) == reg
        # Другое содержимое под тем же именем — другой ключ.
        src.write_bytes(b"two")
        assert digest(src) != key

    assert resolve_data("${CLAUDE_PLUGIN_DATA}") == pathlib.Path.home() / ".to-md"
    assert resolve_data("") == pathlib.Path.home() / ".to-md"
    assert resolve_data("D:/w") == pathlib.Path("D:/w")

    with tempfile.TemporaryDirectory() as tmp:
        csv = pathlib.Path(tmp) / "t.csv"
        csv.write_text("имя,цена\nчай,120\n", encoding="utf-8")
        md, why = convert_doc(csv)
        assert why == "" and md and "чай" in md, (md, why)
        # anydoc называет сбой типом исключения — своей эвристики «мало символов» больше нет.
        broken = pathlib.Path(tmp) / "t.pdf"
        broken.write_bytes(b"%PDF-1.4 not really a pdf")
        md, why = convert_doc(broken)
        assert md is None and why, (md, why)

    # Один входной файл — один md: совпавшие основы разъезжаются по расширению.
    names = md_names([pathlib.Path(n) for n in ("a.pdf", "проба.pdf", "проба.docx")])
    assert names[pathlib.Path("a.pdf")] == "a.md", names
    assert names[pathlib.Path("проба.pdf")] == "проба-pdf.md", names
    assert names[pathlib.Path("проба.docx")] == "проба-docx.md", names
    assert len(set(names.values())) == 3, names

    assert pick_device("cpu") == "cpu" and pick_device("cuda") == "cuda"
    assert pick_device("auto") in ("cuda", "cpu")
    if platform.system() == "Darwin":
        # У CTranslate2 нет бэкенда Metal — на Маке auto всегда процессор.
        assert pick_device("auto") == "cpu"

    photo_md = (
        '---\nsource: "IMG_1.jpg"\ndate: 2026-08-14\ntype: задача\n---\n\n'
        "## Задача 14\n\nТело массой m = 2 кг.\n\n---\n> Не перенесено: логотип вуза.\n"
    )
    assert is_photo_md(photo_md) is True
    assert is_photo_md('---\nsource: "лекция.mp3"\n---\n\nтекст\n') is False
    body = strip_omissions(strip_head(photo_md))
    assert body.startswith("## Задача 14"), body
    assert "Не перенесено" not in body, body
    assert body.rstrip().endswith("m = 2 кг."), body

    with tempfile.TemporaryDirectory() as tmp:
        tmp = pathlib.Path(tmp)
        (tmp / "in").mkdir()
        (tmp / "out" / "raw").mkdir(parents=True)
        (tmp / "in" / "IMG_1.jpg").write_bytes(b"photo-one")
        (tmp / "out" / "raw" / "IMG_1.md").write_text(photo_md, encoding="utf-8")
        # Метки считает код, а не модель: две и больше — на второй проход.
        marked = photo_md.replace("m = 2 кг", f"m = {MARK} кг").replace(
            "Задача 14", f"Задача {MARK}"
        )
        (tmp / "out" / "raw" / "IMG_2.md").write_text(
            marked.replace("IMG_1.jpg", "IMG_2.jpg"), encoding="utf-8"
        )
        (tmp / "out" / "raw" / "лекция.md").write_text(
            f'---\nsource: "лекция.mp3"\n---\n\n{MARK} {MARK} {MARK}\n',
            encoding="utf-8",
        )
        hot = escalate(tmp / "out" / "raw")
        assert [pathlib.Path(p).name for p in hot] == ["IMG_2.md"], hot

        assert collect(tmp / "out", tmp / "in") == 2
        summary = (tmp / "out" / "задачи.md").read_text(encoding="utf-8")
        assert "Задача 14" in summary and "Не перенесено" not in summary, summary
        assert "лекция" not in summary, summary
        reg = load_registry(tmp / "out")
        assert reg[digest(tmp / "in" / "IMG_1.jpg")]["md"] == "raw/IMG_1.md", reg
        # Пересборка целиком, а не дозапись: второй прогон не удваивает.
        assert collect(tmp / "out", tmp / "in") == 2
        assert (tmp / "out" / "задачи.md").read_text(encoding="utf-8") == summary

    d = doctor(pathlib.Path(tempfile.gettempdir()) / "нет-такой-папки")
    assert set(d) == {
        "os",
        "arch",
        "python",
        "python_ok",
        "whisper",
        "anydoc",
        "cuda_pkgs",
        "plaud",
        "plaud_token",
        "nvidia",
        "data",
        "weights",
        "ready",
    }, sorted(d)
    assert d["python_ok"] is (sys.version_info >= (3, 10))
    assert d["weights"] is False
    assert d["ready"] is (bool(d["whisper"]) and d["anydoc"] and d["python_ok"])

    # Детектор порчи: чистый диалог молчит, каждая патология ловится с якорем.
    ok_segs = [
        {"start": i * 2.0, "end": i * 2.0 + 1.5, "text": f" фраза номер {i} про разное"}
        for i in range(20)
    ]
    assert suspect(ok_segs) == [], suspect(ok_segs)
    loop = [{"start": 41.0 + i, "end": 41.9 + i, "text": " Угу."} for i in range(6)]
    got = suspect(ok_segs + loop)
    assert any("петля" in r and "[00:41]" in r for r in got), got
    assert suspect(ok_segs + loop[:5]) == [], "5 повторов — ещё диалог, не петля"
    art = [{"start": 0.0, "end": 1.0, "text": " Субтитры делал DimaTorzok"}]
    assert any("титры" in r for r in suspect(art)), suspect(art)
    kor = [{"start": 0.0, "end": 1.0, "text": " Корректор А.Кулакова"}]
    assert any("титры" in r for r in suspect(kor)), suspect(kor)
    hw = "Ространснадзор, МТУ, ФГБУ."
    echo = [{"start": 0.0, "end": 5.0, "text": " Ространснадзор, МТУ, ФГБУ."}]
    assert any("hotwords" in r for r in suspect(echo, hw)), suspect(echo, hw)
    assert suspect(echo) == [], "без hotwords та же фраза — обычная речь"
    stall = [{"start": 0.0, "end": 40.0, "text": " я тебе не сомневался"}]
    assert any("провал" in r for r in suspect(stall)), suspect(stall)
    hole = [
        {"start": 0.0, "end": 1.0, "text": " начало"},
        {"start": 70.0, "end": 71.0, "text": " конец"},
    ]
    assert any("дыра" in r for r in suspect(hole)), suspect(hole)
    # 12 провалов и 11 дыр между ними — 23 причины до среза, потолок обязан сработать.
    flood = [
        {"start": i * 200.0, "end": i * 200.0 + 40.0, "text": f" пауза {i}"}
        for i in range(12)
    ]
    assert len(suspect(flood)) == 8, "причины не должны затапливать отчёт"

    # Баннер подозрения: после фронтматтера, до тела, с именем alt-файла.
    with tempfile.TemporaryDirectory() as tmp:
        p = pathlib.Path(tmp) / "запись.md"
        p.write_text(
            head(__file__, duration=json.dumps("01:00")) + "[00:00] тело\n",
            encoding="utf-8",
        )
        add_banner(p, ["петля «угу» ×7 с [01:00]"], "запись-alt.md")
        text = p.read_text(encoding="utf-8")
        assert text.index("Подозрение на потери") < text.index("[00:00] тело"), text
        assert "запись-alt.md" in text, text
        assert strip_head(text).startswith("> Подозрение"), strip_head(text)[:60]
        p2 = pathlib.Path(tmp) / "без-альта.md"
        p2.write_text(
            head(__file__, duration=json.dumps("01:00")) + "[00:00] тело\n",
            encoding="utf-8",
        )
        add_banner(p2, ["дыра 70 с после [00:01]"], None)
        assert "-alt.md" not in p2.read_text(encoding="utf-8")

    # Свежесть токена Plaud: None — логина не было, False — протух, True — живой.
    with tempfile.TemporaryDirectory() as tmp:
        tok = pathlib.Path(tmp) / "tokens.json"
        assert plaud_token_fresh(tok) is None
        tok.write_text('{"expires_at": 1}', encoding="utf-8")
        assert plaud_token_fresh(tok) is False
        tok.write_text(
            json.dumps({"expires_at": int((time.time() + 3600) * 1000)}),
            encoding="utf-8",
        )
        assert plaud_token_fresh(tok) is True
        tok.write_text("[]", encoding="utf-8")
        assert plaud_token_fresh(tok) is None
        tok.write_text('{"expires_at": "2026-01-01T00:00:00Z"}', encoding="utf-8")
        assert plaud_token_fresh(tok) is None

    # Неизвестный флаг первым аргументом — usage и код 2, а не FileNotFoundError.
    assert main(["--help"]) == 2
    print("ok")


def arg_of(argv, name):
    return (
        argv[argv.index(name) + 1]
        if name in argv and argv.index(name) + 1 < len(argv)
        else ""
    )


def main(argv):
    if "--selftest" in argv:
        selftest()
        return 0
    if not argv:
        print(__doc__)
        return 2
    if argv[0] == "--doctor":
        print(
            json.dumps(
                doctor(resolve_data(arg_of(argv, "--data"))),
                ensure_ascii=False,
                indent=1,
            )
        )
        return 0
    if argv[0] == "--escalate":
        for p in escalate(argv[1]):
            print(p)
        return 0
    if argv[0] == "--collect":
        print(collect(argv[1], arg_of(argv, "--in") or "in"))
        return 0
    if argv[0].startswith("-"):
        print(__doc__)
        return 2

    report = dispatch(
        argv[0],
        arg_of(argv, "--out") or "out",
        resolve_data(arg_of(argv, "--data")),
        arg_of(argv, "--lang") or "ru",
        arg_of(argv, "--hotwords") or "",
        arg_of(argv, "--device") or "auto",
        float(arg_of(argv, "--gap") or 0.7),
        arg_of(argv, "--vad").lower() != "false",
    )
    if "--json" in argv:
        print(json.dumps(report, ensure_ascii=False, indent=1))
    else:
        for c in report["converted"]:
            print(f"-> {c['md']}")
            if c.get("suspect"):
                extra = f" | alt: {c['alt']}" if c.get("alt") else ""
                print(f"? {c['md']}: {'; '.join(c['suspect'])}{extra}")
        for s in report["skipped"]:
            print(f"! {s['src']}: {s['why']}")
        for t in report["text"]:
            print(f"= {t}: конвертация не нужна, читается напрямую")
        if report["cached"]:
            print(f"= уже в реестре: {len(report['cached'])}")
        if report["photos"].get("batches"):
            print(
                f"фото: {report['photos']['mode']}, батчей {len(report['photos']['batches'])}"
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

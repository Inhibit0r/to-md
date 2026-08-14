---
name: setup
description: Ставит зависимости to-md под текущую машину и кладёт веса whisper.
disable-model-invocation: true
---

# Установка to-md

Python не ставим — он требование, а не зависимость. Нет Python 3.10+ → скажи об этом и остановись.

## 1. Что за машина

```bash
python "${CLAUDE_PLUGIN_ROOT}/scripts/to_md.py" --doctor --data "${CLAUDE_PLUGIN_DATA}"
```

`python` не нашёлся → повтори с `python3`. Не нашёлся ни один → пользователю нужен Python 3.10+, дальше идти некуда.

Готово, когда: JSON получен и ты знаешь `os`, `arch`, `python_ok`, `whisper`, `anydoc`, `cuda_pkgs`, `nvidia`, `data`, `weights`.

## 2. План под эту машину

Собери список того, чего не хватает, и **покажи его пользователю до установки**:

| Условие из `--doctor` | Что ставится |
|---|---|
| `whisper` пуст | `whisper-ctranslate2` |
| `anydoc: false` | `firecrawl-anydoc` |
| `nvidia: true` и `cuda_pkgs: false` | `nvidia-cublas-cu12`, `nvidia-cudnn-cu12` |
| `os: Darwin` | пакеты `nvidia-*` не ставятся: Metal у CTranslate2 нет, на Маке считается процессор |
| `weights: false` | веса `large-v3-turbo`, ≈1,5 ГБ — **отдельным вопросом** |

Готово, когда: пользователь увидел список и согласился.

## 3. Пакеты

```bash
pip install <согласованный список>
```

Ставятся только имена из таблицы выше — `whisper-ctranslate2`, `firecrawl-anydoc`, `nvidia-cublas-cu12`, `nvidia-cudnn-cu12`. Понадобилось что-то ещё → это разговор с пользователем, а не строка в этой команде.

Установка не пошла из-за прокси → на этой машине помогает `NO_PROXY=*` перед командой.

Готово, когда: повторный `--doctor` даёт `ready: true`.

## 4. Веса

Отдельный вопрос, потому что это полтора гигабайта. Согласился:

```bash
python -c "from faster_whisper.utils import download_model; print(download_model('large-v3-turbo', output_dir=r'<data>/models/large-v3-turbo'))"
```

`<data>` — значение поля `data` из `--doctor`.

Загрузка виснет на нуле байт → это xet-загрузчик HuggingFace: поставь `HF_HUB_DISABLE_XET=1`, а если хаб недоступен — прокси в `TO_MD_PROXY`, конвертор подхватывает её сам.

Отказался → веса скачаются при первом прогоне, скажи об этом.

Готово, когда: `--doctor` даёт `weights: true`, либо пользователь сознательно отложил загрузку.

## 5. Отчёт

Что поставлено · где лежат веса · чем проверить: `/to-md:convert` на одном файле.

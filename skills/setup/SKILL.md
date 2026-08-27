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

Готово, когда: JSON получен и ты знаешь `os`, `arch`, `python_ok`, `whisper`, `anydoc`, `cuda_pkgs`, `nvidia`, `managed`, `whisper_python`, `plaud_token`, `data`, `weights`.

## 2. План под эту машину

Собери список того, чего не хватает, и **покажи его пользователю до установки**:

| Условие из `--doctor` | Что ставится |
|---|---|
| `whisper` пуст | `whisper-ctranslate2` |
| `anydoc: false` | `firecrawl-anydoc` |
| `plaud: false` | `requests` — только если пользователю нужен загрузчик с диктофона Plaud. Транзитивно он не приезжает ни с чем |
| `nvidia: true` и `cuda_pkgs: false` | `nvidia-cublas-cu12`, `nvidia-cudnn-cu12` |
| `os: Darwin` | пакеты `nvidia-*` не ставятся: Metal у CTranslate2 нет, на Маке считается процессор |
| `weights: false` | веса `large-v3-turbo`, ≈1,5 ГБ — **отдельным вопросом** |
| `managed: true` | ничего сверх списка, но ставится иначе — см. шаг 3 |

Готово, когда: пользователь увидел список и согласился.

## 3. Пакеты

`managed: false` — обычная установка:

```bash
pip install <согласованный список>
```

`managed: true` — интерпретатор помечен PEP 668 (штатное состояние для python из Homebrew и большинства дистрибутивов Linux), и голый `pip install` откажет с `externally-managed-environment`. Ставь тем же списком в пользовательский слой:

```bash
pip install --user --break-system-packages <согласованный список>
```

Флаг не проходит политикой окружения → тот же список через uv, он пишет в тот же интерпретатор: `uv pip install --python "$(command -v python3)" --break-system-packages <список>`. Отдельный venv тоже поставит пакеты, но тогда `python` из шага 1 перестанет видеть `anydoc` и шаг 0 скилла convert снова упрётся в `ready: false` — предупреди об этом.

Ставятся только имена из таблицы выше — `whisper-ctranslate2`, `firecrawl-anydoc`, `requests`, `nvidia-cublas-cu12`, `nvidia-cudnn-cu12`. Понадобилось что-то ещё → это разговор с пользователем, а не строка в этой команде.

Установка не пошла из-за прокси → помогает `NO_PROXY=*` перед командой.

Готово, когда: повторный `--doctor` даёт `ready: true`.

## 4. Веса

Отдельный вопрос, потому что это полтора гигабайта. Согласился:

```bash
"<whisper_python>" -c "from faster_whisper.utils import download_model; print(download_model('large-v3-turbo', output_dir=r'<data>/models/large-v3-turbo'))"
```

`<data>` и `<whisper_python>` — одноимённые поля из `--doctor`. Брать именно `whisper_python`, а не текущий `python`: под `managed: true` whisper обычно оказывается в изолированном окружении, и `faster_whisper` из текущего интерпретатора не импортируется. `whisper_python` пуст → whisper не установлен, вернись к шагу 3.

Полтора гигабайта тянутся не мгновенно: долгая команда — не признак зависания, судить о провале можно только по ошибке или по остановке роста `<data>/models/large-v3-turbo`.

Загрузка виснет на нуле байт → это xet-загрузчик HuggingFace: поставь `HF_HUB_DISABLE_XET=1`, а если хаб недоступен — прокси в `TO_MD_PROXY`, конвертор подхватывает её сам.

Отказался → веса скачаются при первом прогоне, скажи об этом.

Готово, когда: `--doctor` даёт `weights: true`, либо пользователь сознательно отложил загрузку.

## 5. Отчёт

Что поставлено · где лежат веса · чем проверить: `/to-md:convert` на одном файле.

# videoworks

Проєкт агентної обробки відео: агент читає відео як текст, планує монтаж і рендерить
результат — з транскрибацією, субтитрами та відтворюваними правками.

Стан: **М0–М5 готові**, М6 — оснастка є, замір чекає українського матеріалу.
Транскрибація на GPU, монтаж по EDL із перемапуванням таймкодів, субтитри з
вжиганням, агентний шар, переклад, бенчмарк ASR.
Деталі — [docs/roadmap.md](docs/roadmap.md).

## Швидкий старт

```powershell
uv sync                                   # venv на Python 3.12 + залежності
.venv\Scripts\python -m videoworks.cli doctor

.venv\Scripts\python -m videoworks.cli init myvideo
# кинути файл у projects\myvideo\media\
.venv\Scripts\python -m videoworks.cli transcribe myvideo --lang uk
.venv\Scripts\python -m videoworks.cli trim myvideo --max-pause 0.6
.venv\Scripts\python -m videoworks.cli cut myvideo
.venv\Scripts\python -m videoworks.cli subtitle myvideo --format srt,ass --burn --soft
```

На виході:

```
projects\myvideo\
  transcript.raw.json      слова, таймкоди — вхід для всього іншого
  edl.json                 план монтажу (від trim або від агента)
  transcript.cut.json      таймкоди, перемаплені під змонтоване відео
  subs\uk.srt · uk.ass     репліки, готові до правки
  out\cut.mp4              змонтоване відео
  out\cut.receipt.json     провенанс: хеші входів, EDL, версії тулів, команда
  out\subtitled.mp4        субтитри вжиті в кадр
  out\subtitled.mkv        ті самі субтитри окремим треком
  out\myvideo.fcpxml       для доведення руками в Resolve / Premiere / FCP
```

`cut --dry-run` проганяє лише preflight. `subtitle` сам бере пару
`transcript.cut.json` + `cut.mp4`, якщо монтаж уже зроблено.

## Монтаж агентом

У репо є skill для Claude Code (`.claude/skills/videoworks/`). Досить сказати
«прибери паузи й зайве з projects/myvideo» — агент прочитає зведення, напише
план, покаже його й дочекається згоди.

```powershell
.venv\Scripts\python -m videoworks.cli scenes myvideo      # межі склейок
.venv\Scripts\python -m videoworks.cli brief myvideo       # стисле зведення
.venv\Scripts\python -m videoworks.cli review myvideo      # що саме зникне
.venv\Scripts\python -m videoworks.cli filmstrip myvideo --start 40 --end 70
```

`brief` — компактна форма транскрипту на рівні фраз: word-level JSON у контекст
не влазить, та й рішення про різку на рівні слів не приймаються. `review` показує
план у словах, а не в числах — його треба бачити до рендеру.

Корисні ключі: `--max-line 42`, `--max-lines 2`, `--cps 17`, `--font "Arial"`,
`--encoder nvenc`.

## Структура

```
src/videoworks/
  models.py         — контракти: Transcript, Edl, Receipt
  paths.py          — розкладка папки проєкту
  ingest.py         — ffprobe + витяг аудіо 16 кГц
  asr/              — ASRBackend + реалізація на faster-whisper
  text.py           — склеювання токенів ASR, відмінювання числівників
  scenes.py         — детекція склейок (PySceneDetect)
  brief.py          — стисле зведення для агента + review плану
  filmstrip.py      — контактні аркуші кадрів на вимогу
  subtitles.py      — репліки, розкладка на рядки, експорт SRT/VTT/ASS
  burn.py           — вжигання через libass, мʼякий мукс у MKV
  edl.py            — побудова EDL, preflight, remap_transcript
  render.py         — EDL → ffmpeg → cut.mp4 + receipt
  fcpxml.py         — експорт EDL у FCPXML
  translate.py      — аркуш перекладу, глосарій, бюджет, перевірка придатності
  metrics.py        — WER, точність меж слів, узгодженість спікерів
  benchmark.py      — порівняння ASR-бекендів на одному матеріалі
  diarize.py        — перенесення спікерів на транскрипт зі словами
  diagnostics.py    — спільний тип Problem для всіх перевірок
  cli.py            — typer CLI
  _cuda.py          — бутстрап cuDNN/cuBLAS DLL для CTranslate2 на Windows
.claude/skills/
  videoworks/       — skill: як агенту монтувати через цей пайплайн
docs/
  landscape.md      — що вже існує, зірки, ліцензії, що з цього брати
  architecture.md   — архітектура пайплайну
  roadmap.md        — наскрізний потік і етапи М0–М6
  transcription.md  — план тулу транскрибації та субтитрів
refs/
  refs.yaml         — каталог зовнішніх репо з поясненням, навіщо кожен
  repos.tsv         — той самий список у машинному вигляді (для скрипта)
scripts/
  fetch-refs.ps1    — shallow-клон референсів у vendor/
vendor/             — зовнішні репо (в git не комітяться)
projects/           — робочі проєкти з медіа (локальні, не комітяться)
```

## Оточення

Перевірено на цій машині: RTX 4060 Laptop 8 ГБ, ffmpeg 9.0.1 (libass + nvenc),
Python 3.12.14, faster-whisper 1.2.1 на `cuda/float16`.

Системний Python 3.14 для ML-стека не годиться — `uv sync` створює venv на 3.12.
`_cuda.py` додає `site-packages/nvidia/*/bin` у шлях пошуку DLL: без цього
CTranslate2 падає з `Could not locate cudnn_ops64_9.dll`.

## Підтягнути референси

```powershell
.\scripts\fetch-refs.ps1 -List            # подивитись каталог
.\scripts\fetch-refs.ps1 -Tier core       # клонувати найважливіше
.\scripts\fetch-refs.ps1 -Group transcription
.\scripts\fetch-refs.ps1 -Name video-use,kinocut -Update
```

Клонує з `--depth 1 --filter=blob:none`, тому навіть великі репо тягнуться швидко.

## Ключові знахідки

- **[video-use](https://github.com/browser-use/video-use)** (24.2k, MIT) — найближчий
  аналог. Text-first підхід: LLM не дивиться відео, а читає word-level транскрипт.
  Стартова точка.
- **[kinocut](https://github.com/KyaniteLabs/mcp-video)** (Apache-2.0) — MCP-сервер над
  FFmpeg із guardrails: preflight-валідація, «Video Receipts», fail-closed. Те, чого
  бракує саморобним обгорткам.
- **[OpenMontage](https://github.com/calesthio/OpenMontage)** (56.4k, AGPL-3.0) — як
  пакувати доменні знання в skills. Ліцензія копілефтна: беремо ідеї, не код.
- **[faster-whisper](https://github.com/SYSTRAN/faster-whisper)** + **[whisperX](https://github.com/m-bain/whisperX)**
  + **[MOSS-Transcribe-Diarize](https://github.com/OpenMOSS/MOSS-Transcribe-Diarize)** —
  три кандидати в ASR-бекенд, вибір після бенчмарку на українській.
- **[OpenCut](https://github.com/OpenCut-app/OpenCut)** (88.8k, MIT) — цільовий редактор,
  але зараз переписується з нуля; MCP і headless ще не готові. Не залежність, а ціль.

Деталі й ліцензійні межі — у [docs/landscape.md](docs/landscape.md).

## Як тримається синхронність

Транскрипт робиться **один раз, до монтажу** — без тексту агент не може
планувати різку. Після різки таймкоди не транскрибуються заново, а перемаплюються
арифметично через EDL:

```
слово лишається, якщо його середина потрапила в інтервал EDL
w.start_new = w.start - seg.src_in + seg.dst_out
```

Повторний ASR по змонтованому відео дав би інші слова й іншу пунктуацію і
зруйнував би звʼязок із рішеннями агента. Через перемапування субтитри синхронні
**за побудовою**, а не завдяки підгонці. `remap_transcript` покрита тестами
першою — це найкритичніша функція в проєкті.

## Переклад

Перекладає агент — окремого MT-рушія немає й не треба. Тул дає круговий обмін:

```powershell
.venv\Scripts\python -m videoworks.cli worksheet myvideo --to en --save
# агент перекладає subs\en.draft.md
.venv\Scripts\python -m videoworks.cli apply myvideo --lang en
.venv\Scripts\python -m videoworks.cli check myvideo --lang uk
```

Аркуш містить правила, глосарій і **бюджет символів** на кожну репліку —
менше з двох обмежень: фізичного розміру (рядки × символи) і швидкості читання
(тривалість × CPS). `apply` підставляє переклад у ті самі таймкоди, перераховує
розкладку і **не запише результат**, який не влазить.

Терміни — у `projects/<name>/glossary.tsv`: `термін<TAB>переклад`, порожній
переклад означає «лишити як є».

## Вибір ASR-бекенда

```powershell
.venv\Scripts\python -m videoworks.cli benchmark myvideo `
    --reference projects\myvideo\reference.txt --models small,large-v3 --lang uk
```

```
бекенд                      пристрій      ×RT      WER   медіана   спікери
--------------------------------------------------------------------------
faster-whisper/small        cuda        13.6×    15.4%    154 мс         —
faster-whisper/large-v3     cuda        12.3×     7.7%    223 мс         —
whisperx/small              cuda         6.9×     7.7%         —         —
whisperx/large-v3           cuda         6.0×     0.0%         —         —
```

Еталон `.txt` дає лише WER; `.json` у форматі `Transcript` — ще й точність меж
слів. Саме вона вирішує долю монтажу: 200 мс похибки — це різ посеред складу.

На пробному матеріалі межі faster-whisper розходяться з forced alignment
whisperX на 154–223 мс — удвічі більше за поріг, за яким шов чути. Тому для
монтажу є `transcribe --backend whisperx`, хоч він і вдвічі повільніший.

Важкі бекенди ставляться окремо: `uv sync --extra whisperx`, `--extra moss`.
`videoworks doctor` показує, що встановлено.

`pyproject.toml` навмисно тягне torch із `download.pytorch.org/whl/cu128`:
колесо з PyPI під Windows — без CUDA (`2.14.0+cpu`), і обидва бекенди мовчки
поїхали б на процесор. `torch` і `torchaudio` названі в екстрах явно, бо
`tool.uv.sources` діє лише на прямі залежності, а сюди вони приходять
транзитивно. MOSS ставиться з git — на PyPI його немає.

**MOSS не дає рівня слова** — ні `TranscriptSegment`, ні `SubtitleSegment` у його
моделі не мають слів. Основним бекендом він бути не може, зате дає діаризацію без
закритих ваг pyannote: `diarize.assign_speakers` зшиває слова з faster-whisper зі
спікерами з MOSS.

Для української у whisperX є штатна фонемна модель
(`Yehor/wav2vec2-xls-r-300m-uk-with-small-lm`), тож forced alignment не деградує.

## Наступний крок

**Замір на реальному українському матеріалі.** Оснастка готова; потрібен запис
на 2–5 хвилин із вивіреним текстом. Синтетика для WER не годиться.

Повний план — [docs/roadmap.md](docs/roadmap.md).

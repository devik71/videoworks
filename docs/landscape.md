# Ландшафт: що вже існує (станом на вересень 2026)

Зірки й ліцензії перевірені по README на момент збору. Перед тим як тягнути код у
продукт — перечитати LICENSE у самому репо, а не покладатись на цю таблицю.

## Зведена таблиця

| Репозиторій | ★ | Ліцензія | Роль у нас |
|---|---:|---|---|
| [browser-use/video-use](https://github.com/browser-use/video-use) | 24.2k | MIT | **core** — базовий патерн агентного монтажу |
| [calesthio/OpenMontage](https://github.com/calesthio/OpenMontage) | 56.4k | AGPL-3.0 | study — як пакувати доменні знання в skills |
| [HKUDS/VideoAgent](https://github.com/HKUDS/VideoAgent) | 1.8k | MIT | study — intent analysis + граф-планування |
| [barefootford/buttercut](https://github.com/barefootford/buttercut) | 592 | PolyForm NC | watch — агент → FCPXML для NLE |
| [assafkip/claude-video-editor](https://github.com/assafkip/claude-video-editor) | 15 | MIT + Commons Clause | watch — UX діалогу з апрувом |
| [KyaniteLabs/kinocut](https://github.com/KyaniteLabs/mcp-video) | 139 | Apache-2.0 | **core** — MCP над FFmpeg із guardrails |
| [wilwaldon/Claude-Code-Video-Toolkit](https://github.com/wilwaldon/Claude-Code-Video-Toolkit) | 79 | MIT | study — структура `.claude/` |
| [SYSTRAN/faster-whisper](https://github.com/SYSTRAN/faster-whisper) | 25.3k | MIT | **core** — ASR-рушій |
| [m-bain/whisperX](https://github.com/m-bain/whisperX) | 23.9k | BSD-2 | **core** — word-level + діаризація |
| [OpenMOSS/MOSS-Transcribe-Diarize](https://github.com/OpenMOSS/MOSS-Transcribe-Diarize) | 1.9k | Apache-2.0 | **core** — ASR+діаризація одним проходом |
| [jianfch/stable-ts](https://github.com/jianfch/stable-ts) | 2.3k | MIT | tool — refine таймкодів (**архів з 30.05.2026**) |
| [absadiki/subsai](https://github.com/absadiki/subsai) | 1.7k | GPL-3.0+ | study — абстракція ASR-бекендів |
| [Breakthrough/PySceneDetect](https://github.com/Breakthrough/PySceneDetect) | 5.2k | BSD-3 | tool — детекція склейок |
| [WyattBlue/auto-editor](https://github.com/WyattBlue/auto-editor) | 5.2k | Unlicense | tool — зріз тиші, експорт у NLE |
| [OpenCut-app/OpenCut](https://github.com/OpenCut-app/OpenCut) | 88.8k | MIT | watch — цільовий редактор/таймлайн |
| [remotion-dev/remotion](https://github.com/remotion-dev/remotion) | 58.4k | Remotion License | watch — моушн-графіка, **платно для компаній** |

## Що з цього справді важливо

**video-use — найближчий аналог.** Його ключова ідея: агент ніколи не «дивиться» відео.
Він читає word-level транскрипт (~12 КБ на проєкт) і лише за потреби генерує PNG-фільмстріпи.
Це те, що робить агентний монтаж узагалі можливим у межах контекстного вікна. Різка йде
по межах слів, тому шви завжди чисті. Стартувати варто звідси.

**kinocut — те, чого бракує всім саморобним обгорткам над FFmpeg.** Preflight-валідація
перед рендером, «Video Receipts» (провенанс: що з чого зроблено), fail-closed на
ризикових операціях. Апач-ліцензія дозволяє брати код. Якщо не брати — то хоча б
скопіювати підхід із receipts і preflight.

**OpenMontage — енциклопедія, а не бібліотека.** 700+ skill-файлів, які вчать агента
користуватись 100+ тулами. AGPLv3 означає, що код у закритий продукт не заходить,
але структура «YAML-маніфести + Markdown skills, Python лише для стану» — правильна,
і її можна відтворити з нуля.

**OpenCut — приманка, яка ще не готова.** 88.8k зірок і MIT, у роадмапі Editor API,
MCP-сервер і headless-рендер — рівно наш стек. Але проєкт зараз переписується з нуля,
і нічого з цього ще немає. Тримаємо як ціль інтеграції на пізніше, не як залежність зараз.

**Remotion — ліцензійна пастка.** Технічно ідеальний для програмних титрів, але
компаніям потрібна платна ліцензія. Якщо продукт комерційний — або платимо, або
робимо титри через FFmpeg `libass` (ASS-стилі покривають 90% потреб).

## Ліцензійні межі

- Вільно вендоримо код: `auto-editor` (public domain), `faster-whisper`, `stable-ts`,
  `video-use`, `VideoAgent`, `Claude-Code-Video-Toolkit` (MIT), `kinocut`,
  `MOSS-Transcribe-Diarize` (Apache-2.0), `whisperX` (BSD-2), `PySceneDetect` (BSD-3).
- Тільки як референс, код не копіюємо: `OpenMontage` (AGPL-3.0), `subsai` (GPL-3.0+).
- Обмеження на комерціалізацію самого інструмента: `buttercut` (PolyForm NC),
  `claude-video-editor` (Commons Clause).
- Потребує платної ліцензії для компаній: `remotion`.

Окремо: ваги `pyannote` (діаризація у whisperX) — gated на HuggingFace, потрібен токен
і згода з умовами. Для продукту це операційна проблема; `MOSS-Transcribe-Diarize`
під Apache-2.0 її знімає.

# Реконструкція звуку (сінк + дакінг) — план реалізації

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Шар, що вирівнює оригінали треків із кімнатним записом, будує огинаючу дакінгу з міток голосу й пише `media/mix.wav`, який `cut` використовує замість звуку з відео.

**Architecture:** Пакет `src/videoworks/soundtrack/` працює на сирому таймлайні, до монтажу. Контракт `soundtrack.json` (pydantic у `models.py`); EDL отримує необовʼязкове `audio_source`, рендер бере його другим входом. Усе DSP — numpy/scipy офлайн; синтетичний стенд з відомим еталоном міряє якість.

**Tech Stack:** Python 3.12, numpy, scipy (нова залежність), pydantic, typer, ffmpeg 9 (декодування, рендер).

**Spec:** `docs/superpowers/specs/2026-11-06-soundtrack-design.md`

## Global Constraints

- Python `>=3.12,<3.13`; ruff `line-length = 100`; `ruff format` — лише на нових файлах, не на всьому дереві.
- Docstrings, коментарі, повідомлення CLI і `Problem` — українською, у стилі наявного коду.
- Контракти розширюються, поля не перейменовуються. Без `audio_source` вихід `build_filtergraph` і `cut` — байт-у-байт як раніше.
- Нова залежність лише `scipy`; `librosa` не тягнемо.
- Файли, які може редагувати людина (`soundtrack.json`), читаються як `utf-8-sig`.
- Граф ffmpeg передається файлом (`-/filter_complex <file>`), як у `render.py`.
- Тести, що викликають ffmpeg, пропускаються, якщо `shutil.which("ffmpeg")` порожній — як у `tests/test_audio.py`.
- Пороги зі спеку: `rec_at` ≤ 1 мс, дрейф ≤ 5 ppm, межі ≤ 0.25 с, повнота голосу ≥ 95%; `alternative`, якщо другий пік ≥ 0.8 головного; бічні піки — поза ±50 мс; вікна точного сінку 4 с кожні 10 с; лірика — ±0.3 с і схожість ≥ 0.6; `suspects` — надлишок ≥ 6 дБ у 300–3400 Гц; перекриття ≤ 15 с; `rate` у 1 ± 500 ppm.
- Дефолти `MixSettings`: `mode="blend"`, `duck_db=-12`, `room_floor_db=-24`, `preroll=0.20`, `hold=0.60`, `attack=0.08`, `release=0.60`, `room_band=(150, 6000)`.

## Review Focus

1. **Запис почався посеред треку** — `track_in > 0`, `rec_at ≈ 0`; сінк має знайти частину, а не відкинути трек. Тест у Task 4.
2. **Жоден трек не знайдено** (не та музика) — `align` повертає `None`, `mix` дає кімнатний звук без змін. Тести в Task 4 і Task 6.
3. **Різні частоти й канали** — трек 44.1 кГц стерео, запис 48 кГц моно; декодування приводить до однієї частоти, мікс не зсувається. Тест у Task 1.
4. **Повторний `sync` після ручної правки** — розміщення з `locked` лишається як було. Тест у Task 4.
5. **Немає транскрипту або слів** — `voice` дає порожні `spans` з попередженням, мікс = чистий трек + підкладка зали, без падіння. Тест у Task 5 і Task 6.

---

### Task 1: Залежність, декодування, контракт `soundtrack.json`

**Files:**
- Modify: `pyproject.toml` (dependencies: `"scipy>=1.14"`)
- Create: `src/videoworks/soundtrack/__init__.py`, `src/videoworks/soundtrack/decode.py`
- Modify: `src/videoworks/models.py` (нові моделі після `Edl`; поле в `Edl`)
- Modify: `src/videoworks/paths.py`
- Test: `tests/test_soundtrack_models.py`

**Interfaces:**
- Produces:
  - `decode.load(path: Path, rate: int, *, channels: int = 1, start: float = 0.0, duration: float | None = None) -> np.ndarray` — float32; форма `(n,)` для 1 каналу, `(n, channels)` інакше. ffmpeg `-f f32le -ac <ch> -ar <rate>` у pipe.
  - `decode.write_wav(path: Path, rate: int, channels: int) -> WavWriter`, де `WavWriter` — контекстний менеджер з `.write(block: np.ndarray)`; 24-біт PCM через stdlib `wave`, кліп до [-1, 1].
  - `models.Placement(track: str, track_sha256: str | None = None, track_in: float, track_out: float, rec_at: float, rate: float = 1.0, gain_db: float = 0.0, confidence: float = 0.0, alternative: float | None = None, sync_error_ms: float | None = None, locked: bool = False)`; властивість `rec_out = rec_at + (track_out - track_in) * rate`; метод `to_rec(t: float) -> float` = `rec_at + (t - track_in) * rate`. Валідатор: `track_out > track_in`.
  - `models.VoiceInfo(source: str = "transcript", spans: list[tuple[float, float]] = [], lyrics_dropped: int = 0, suspects: list[tuple[float, float]] = [])`
  - `models.MixSettings(mode: Literal["blend", "room"] = "blend", …дефолти з Global Constraints…)`
  - `models.Soundtrack(source: str, placements: list[Placement] = [], voice: VoiceInfo, mix: MixSettings)` із `save(path)` / `load(path)` (`utf-8-sig`).
  - `models.Edl.audio_source: str | None = None` — шлях відносно кореня проєкту.
  - `paths.Project.tracks` → `media/tracks`, `.soundtrack` → `soundtrack.json`, `.mix` → `media/mix.wav`, `.sync_ab` → `out/sync-ab.wav`, `.track_files() -> list[Path]` (суфікси `.mp3 .m4a .aac .flac .wav .ogg .opus`, відсортовано).

- [ ] **Step 1: Write the failing tests**

```python
def test_soundtrack_roundtrip(tmp_path):
    st = Soundtrack(source="media/raw.mp4",
                    placements=[Placement(track="media/tracks/a.mp3", track_in=0, track_out=10, rec_at=5, rate=1.00003)])
    loaded = Soundtrack.load(st.save(tmp_path / "soundtrack.json"))
    assert loaded == st
    assert loaded.mix.duck_db == -12 and loaded.mix.room_band == (150, 6000)

def test_soundtrack_load_tolerates_bom(tmp_path): ...  # файл з U+FEFF читається

def test_placement_maps_track_time():
    p = Placement(track="a", track_in=2.0, track_out=12.0, rec_at=5.0, rate=1.0001)
    assert p.to_rec(2.0) == 5.0
    assert p.rec_out == pytest.approx(5.0 + 10 * 1.0001)

def test_placement_rejects_empty(): ...  # track_out <= track_in → ValidationError

def test_edl_without_audio_source_dumps_as_before():
    assert Edl(source="x").audio_source is None

@needs_ffmpeg
def test_load_resamples_and_downmixes(tmp_path):
    # 1 с синуса 1 кГц, 44.1 кГц стерео → load(rate=48000, channels=1)
    x = decode.load(wav, 48000)
    assert x.shape == (48000,) and x.dtype == np.float32

@needs_ffmpeg
def test_wav_writer_roundtrip(tmp_path): ...  # write → load, похибка < 1e-4
```

- [ ] **Step 2: Run tests, expect FAIL** — `uv run pytest tests/test_soundtrack_models.py -v` → ImportError.
- [ ] **Step 3: `uv add "scipy>=1.14"`, implement models, paths, decode.**
- [ ] **Step 4: Run tests, expect PASS**; також `uv run pytest -q` — уся решта зелена.
- [ ] **Step 5: Commit** — `Додати контракт soundtrack.json і декодування звуку`.

---

### Task 2: Ознаки — огинаюча онсетів, кореляція, GCC-PHAT

**Files:**
- Create: `src/videoworks/soundtrack/features.py`
- Test: `tests/test_soundtrack_features.py`

**Interfaces:**
- Produces:
  - `HOP = 0.01` (с).
  - `onset_envelope(x: np.ndarray, rate: int, *, bands: int = 40) -> np.ndarray` — STFT (вікно 2048 при 16 кГц, крок `HOP`), мел-фільтрбанк на numpy, `log1p`, перша різниця по часу, півхвильове випрямлення, сума по смугах, нормування (мінус середнє, поділ на std).
  - `@dataclass(frozen=True) Peak(lag: float, score: float, confidence: float, alternative: float | None)` — `lag` у секундах: де в записі починається референс (може бути відʼємним).
  - `correlate(ref: np.ndarray, rec: np.ndarray, *, frame: float = HOP, exclude: float = 0.05) -> Peak` — повна FFT-кореляція; `confidence = головний / max(|бічний| поза ±exclude)`; `alternative` — зсув другого піку відносно головного, якщо він ≥ 0.8 головного, інакше `None`; субкадрова точність через `parabolic`.
  - `gcc_phat(ref: np.ndarray, rec: np.ndarray, rate: int, *, max_lag: float) -> tuple[float, float]` — (на скільки секунд `rec` запізнюється відносно `ref`, висота піку нормованої PHAT-кореляції ∈ [0, 1]).
  - `parabolic(y: np.ndarray, i: int) -> float` — дробовий індекс вершини.

- [ ] **Step 1: Write the failing tests**

```python
def test_parabolic_finds_true_vertex():
    y = -(np.arange(10) - 4.3) ** 2
    assert parabolic(y, 4) == pytest.approx(4.3, abs=1e-9)

def test_gcc_phat_subsample_delay():
    # білий шум 16 кГц, 2 с; rec = ref, зсунутий на 23.4 семпла (дробово, через FFT-фазу) + шум −20 дБ
    lag, height = gcc_phat(ref, rec, 16000, max_lag=0.1)
    assert lag == pytest.approx(23.4 / 16000, abs=0.1 / 16000)
    assert height > 0.3

def test_correlate_finds_offset_of_envelope():
    # envelope «треку» вставлена в довшу envelope «запису» з 7.37 с + шум
    peak = correlate(ref_env, rec_env)
    assert peak.lag == pytest.approx(7.37, abs=0.01)
    assert peak.confidence > 2

def test_correlate_reports_beat_ambiguity():
    # суто періодична envelope (імпульс кожні 0.45 с) → alternative ≈ ±0.45
    peak = correlate(ref_env, rec_env)
    assert peak.alternative is not None and abs(abs(peak.alternative) - 0.45) < 0.02

def test_onset_envelope_rate():
    env = onset_envelope(np.zeros(16000 * 3, np.float32), 16000)
    assert abs(len(env) - 300) <= 2 and np.isfinite(env).all()
```

- [ ] **Step 2: Run, expect FAIL.** `uv run pytest tests/test_soundtrack_features.py -v`
- [ ] **Step 3: Implement** у `features.py` (scipy.signal.fftconvolve / numpy.fft; GCC-PHAT з `rfft`, вага `1/|X|`, `irfft` з `n` = 8× для чіткішого піку, обрізка до `max_lag`).
- [ ] **Step 4: Run, expect PASS.**
- [ ] **Step 5: Commit** — `Додати ознаки для сінку: онсети, кореляція, GCC-PHAT`.

---

### Task 3: Синтетичний стенд

**Files:**
- Create: `src/videoworks/soundtrack/bench.py`
- Test: `tests/test_soundtrack_bench.py`

**Interfaces:**
- Consumes: `Placement` (Task 1).
- Produces:
  - `@dataclass Truth(placements: list[Placement], voice: list[tuple[float, float]])`
  - `@dataclass Scene(recording: np.ndarray, tracks: dict[str, np.ndarray], rate: int, truth: Truth)` — `recording` моно; треки стерео `(n, 2)`; ключі `"a.wav"`, `"b.wav"` (і `"loop.wav"` у періодичному режимі).
  - `synth_track(seconds: float, rate: int, *, bpm: float = 132, seed: int, periodic: bool = False) -> np.ndarray` — бочка на долях, акорди зі зміною кожні 2 такти, неперіодичні вставки (шумові свіпи у випадкових місцях); `periodic=True` — лише бочка й один акорд.
  - `synth_voice(spans: list[tuple[float, float]], n: int, rate: int, *, seed: int) -> np.ndarray` — шум у смузі 300–3400 Гц, амплітудно модульований ~4 Гц (склади), лише всередині `spans`.
  - `room(x: np.ndarray, rate: int, *, delay: float = 0.023, rt60: float = 0.8, drift_ppm: float = 30, noise_db: float = -40, agc: bool = True, seed: int) -> np.ndarray` — моно; дрейф через `scipy.signal.resample` на `round(n * (1 + ppm·1e-6))`; IR — експоненційно згасаючий шум; бас-шельф +6 дБ < 150 Гц; AGC — повільна (2 с) нормалізація RMS із силою 50%.
  - `make_scene(rate: int = 16000, *, seed: int = 0, periodic: bool = False, start_mid: bool = False) -> Scene` — трек `a` (60 с) з 5.0 с запису, трек `b` (60 с) починається за 6 с до кінця `a` (кросфейд 6 с, рівнопотужний), запис закінчується через 10 с після кінця `b`; голос — 6 фраз по 1.5–3 с, частина поверх музики, одна в тиші перед `a`. `start_mid=True` — запис починається на 20-й секунді `a`. Еталонні `Placement` містять `rate = 1 + 30e-6` і `rec_at` з урахуванням `delay`.
  - `score(found: list[Placement], truth: Truth) -> dict[str, float]` — ключі `rec_at_err_ms`, `drift_err_ppm`, `extent_err_s` (максимум по трекам; ненайдений трек → `inf`).
  - `write_project(project: Project, scene: Scene) -> None` — `media/raw.mp4` (чорний кадр 320×240 + звук запису через ffmpeg), `media/tracks/*.wav`, `transcript.raw.json` зі словами «раз», «два»… у межах `truth.voice` плюс «лірика» (слова «love», «tonight») у музичних ділянках, і кеш `media/tracks/a.transcript.json` із тими самими словами лірики в часі треку.

- [ ] **Step 1: Write the failing tests**

```python
def test_scene_is_deterministic():
    a, b = make_scene(seed=3), make_scene(seed=3)
    assert np.array_equal(a.recording, b.recording)

def test_scene_truth_matches_layout():
    s = make_scene()
    pa, pb = s.truth.placements
    assert pa.rec_at == pytest.approx(5.0 + 0.023, abs=1e-6)
    assert pb.rec_at < pa.rec_out and pa.rec_out - pb.rec_at == pytest.approx(6.0, abs=0.01)

def test_start_mid_truth():
    pa = make_scene(start_mid=True).truth.placements[0]
    assert pa.track_in == pytest.approx(20.0, abs=0.01) and pa.rec_at < 0.1

def test_score_perfect_and_missing():
    s = make_scene()
    assert score(s.truth.placements, s.truth)["rec_at_err_ms"] == 0
    assert score([], s.truth)["rec_at_err_ms"] == float("inf")
```

- [ ] **Step 2: Run, expect FAIL.**
- [ ] **Step 3: Implement** `bench.py` (без `write_project` — його тест у Task 8).
- [ ] **Step 4: Run, expect PASS** (уся сцена генерується < 3 с).
- [ ] **Step 5: Commit** — `Додати синтетичний стенд для звуку з відомим еталоном`.

---

### Task 4: Сінк

**Files:**
- Create: `src/videoworks/soundtrack/sync.py`
- Test: `tests/test_soundtrack_sync.py`

**Interfaces:**
- Consumes: `features.onset_envelope`, `features.correlate`, `features.gcc_phat`, `bench.make_scene`, `bench.score`, `Placement`.
- Produces:
  - `WINDOW = 4.0`, `STEP = 10.0`, `MIN_CONFIDENCE = 1.5`.
  - `align(rec: np.ndarray, track: np.ndarray, rate: int, *, name: str, voice: list[tuple[float, float]] = ()) -> Placement | None` — моно-масиви однієї частоти (трек зводиться до моно викликачем). Кроки:
    1. `correlate(onset_envelope(track), onset_envelope(rec))` → грубий `rec_at`; `confidence < MIN_CONFIDENCE` → `None`.
    2. Вікна треку по `WINDOW` кожні `STEP`, які повністю в записі за грубою оцінкою й не перетинають `voice`; на кожному `gcc_phat(max_lag=0.05)`; вікна з висотою < 0.5·медіани відкинути.
    3. `scipy.stats.theilslopes(lag, t_track)` → `rec_at`, `rate = 1 + slope`; `sync_error_ms` = 1000·std залишків.
    4. Межі: нормована кореляція вирівняних вікон; з країв іти до першого вікна ≥ 0.5·медіани, потім уточнити вікнами по 1 с → `track_in`, `track_out` (обрізано до частини, що в записі).
    5. `gain_db` = 20·log10(RMS запису / RMS вирівняного треку) на прийнятих вікнах.
  - `sync(rec: np.ndarray, tracks: dict[str, np.ndarray], rate: int, *, voice=(), previous: list[Placement] = ()) -> list[Placement]` — `align` для кожного треку, крім тих, що мають `locked` у `previous` (ті переносяться без змін); результат відсортований за `rec_at`.

- [ ] **Step 1: Write the failing tests**

```python
@pytest.fixture(scope="module")
def scene(): return make_scene(seed=1)

def test_meets_spec_thresholds(scene):
    found = sync(scene.recording, mono(scene.tracks), scene.rate, voice=scene.truth.voice)
    s = score(found, scene.truth)
    assert s["rec_at_err_ms"] <= 1.0
    assert s["drift_err_ppm"] <= 5.0
    assert s["extent_err_s"] <= 0.25

def test_recording_starts_mid_track():
    sc = make_scene(start_mid=True)
    assert score(sync(sc.recording, mono(sc.tracks), sc.rate), sc.truth)["rec_at_err_ms"] <= 1.0

def test_unrelated_track_is_not_placed(scene):
    other = synth_track(30, scene.rate, seed=99).mean(axis=1)
    assert align(scene.recording, other, scene.rate, name="x.wav") is None

def test_periodic_track_flags_alternative():
    sc = make_scene(periodic=True)
    found = sync(sc.recording, mono(sc.tracks), sc.rate)
    assert any(p.alternative is not None for p in found if p.track == "loop.wav")

def test_locked_placement_survives_resync(scene):
    locked = scene.truth.placements[0].model_copy(update={"rec_at": 99.0, "locked": True})
    found = sync(scene.recording, mono(scene.tracks), scene.rate, previous=[locked])
    assert next(p for p in found if p.track == locked.track).rec_at == 99.0
```

- [ ] **Step 2: Run, expect FAIL.**
- [ ] **Step 3: Implement** `sync.py`.
- [ ] **Step 4: Run, expect PASS.** Якщо пороги не тримаються — не послаблювати їх, а записати фактичні числа в звіт задачі й зупинитись для рішення.
- [ ] **Step 5: Commit** — `Додати сінк треків: онсети, GCC-PHAT, дрейф і межі`.

---

### Task 5: Мітки голосу

**Files:**
- Create: `src/videoworks/soundtrack/voice.py`
- Test: `tests/test_soundtrack_voice.py`

**Interfaces:**
- Consumes: `Placement.to_rec`, `models.Word`, `models.Transcript`.
- Produces:
  - `lyric_words(track_transcript: Transcript, placement: Placement) -> list[Word]` — слова в межах `[track_in, track_out]`, перенесені в час запису.
  - `similar(a: str, b: str) -> float` — нижній регістр, без пунктуації, `difflib.SequenceMatcher.ratio`.
  - `drop_lyrics(words: list[Word], lyrics: list[Word], *, window: float = 0.3, threshold: float = 0.6) -> tuple[list[Word], int]`
  - `spans(words: list[Word], *, hold: float) -> list[tuple[float, float]]` — злиття при проміжку < `hold`; порожні й пробільні слова ігноруються.
  - `suspects(rec: np.ndarray, predicted: np.ndarray, rate: int, voiced: list[tuple[float, float]], *, excess_db: float = 6.0, min_len: float = 0.3) -> list[tuple[float, float]]` — кадри 50 мс; енергія в 300–3400 Гц; посмугове підсилення predicted→rec — медіана відношень по всіх кадрах; кандидат, де rec перевищує передбачення на ≥ `excess_db` поза `voiced`, тривалістю ≥ `min_len`.

- [ ] **Step 1: Write the failing tests**

```python
def test_lyric_word_near_same_text_is_dropped():
    words = [W("love", 10.0, 10.3), W("раз", 10.1, 10.4)]
    kept, dropped = drop_lyrics(words, [W("love", 10.2, 10.5)])
    assert [w.w for w in kept] == ["раз"] and dropped == 1

def test_speech_over_singing_is_kept():
    kept, _ = drop_lyrics([W("руки", 10.0, 10.3)], [W("tonight", 10.0, 10.4)])
    assert len(kept) == 1

def test_spans_merge_within_hold():
    assert spans([W("a", 1, 1.2), W("b", 1.5, 1.7), W("c", 3, 3.2)], hold=0.6) == [(1, 1.7), (3, 3.2)]

def test_spans_empty_without_words():
    assert spans([], hold=0.6) == []

def test_lyric_words_follow_placement():
    p = Placement(track="a", track_in=0, track_out=60, rec_at=5.0, rate=1.0)
    assert lyric_words(T([W("love", 2.0, 2.3)]), p)[0].start == pytest.approx(7.0)

def test_suspects_find_unlabelled_voice():
    # bench: predicted = вирівняний трек через room() без голосу; rec = з голосом;
    # voiced = усі, крім однієї фрази → саме вона потрапляє в suspects (перекриття ≥ 80%)
```

- [ ] **Step 2: Run, expect FAIL.**
- [ ] **Step 3: Implement** `voice.py`.
- [ ] **Step 4: Run, expect PASS.**
- [ ] **Step 5: Commit** — `Додати мітки голосу з фільтром лірики`.

---

### Task 6: Мікс

**Files:**
- Create: `src/videoworks/soundtrack/mix.py`
- Test: `tests/test_soundtrack_mix.py`

**Interfaces:**
- Consumes: `Soundtrack`, `Placement`, `MixSettings`, `decode.load`, `decode.write_wav`, `bench.make_scene`.
- Produces:
  - `shape(spans: list[tuple[float, float]], n: int, rate: int, s: MixSettings) -> np.ndarray` — ∈ [0, 1]; 1 у `[start − preroll, end]`; рампа атаки (½−½cos) довжиною `attack` **закінчується** в `start − preroll`; рампа релізу довжиною `release` починається в `end`; ділянки, що зливаються рампами, — максимум.
  - `weights(placements: list[Placement], n: int, rate: int) -> np.ndarray` — `(k, n)`; 1 всередині `[rec_at, rec_out)`, рівнопотужні рампи (cos/sin) на перекритті сусідів, 0 поза.
  - `place(track: np.ndarray, p: Placement, n: int, rate: int) -> np.ndarray` — трек (той самий `rate`, моно або стерео) обрізаний до `[track_in, track_out]`, ресемплений `scipy.signal.resample` на `rate` розміщення, помножений на `10**(gain_db/20)`, покладений з `rec_at` у масив довжини `n` (те, що виходить за межі, обрізається).
  - `render(rec: np.ndarray, music: np.ndarray, coverage: np.ndarray, shp: np.ndarray, rate: int, s: MixSettings) -> np.ndarray` — `(n, 2)`. `duck = 10**(duck_db/20)` (у режимі `room` — 0), `floor = 10**(room_floor_db/20)`:
    ```
    clean = music * (1 + shp * (duck - 1))
    bed   = band(rec) if mode == "blend" else rec          # band: Butterworth 4-го порядку, sosfiltfilt
    room  = (1 - coverage) * rec + coverage * (floor + shp * (1 - floor)) * bed
    out   = clean + room[:, None]
    ```
  - `mix_files(recording: Path, root: Path, st: Soundtrack, dest: Path, *, rate: int = 48000, block: float = 30.0, margin: float = 1.0, ab: Path | None = None) -> Path` — треки стерео декодуються й `place`-яться цілком один раз; запис читається блоками `block` з `margin` з обох боків (фільтр по блоку з полями, потім обрізка), пишеться `WavWriter`. `ab`: лівий — запис, правий — сума `place` моно.

- [ ] **Step 1: Write the failing tests**

```python
def test_attack_finishes_before_first_word():
    s = MixSettings(); rate = 1000
    shp = shape([(2.0, 3.0)], 5000, rate, s)
    assert shp[int((2.0 - s.preroll) * rate)] == pytest.approx(1.0)
    assert shp[int((2.0 - s.preroll - s.attack) * rate) - 1] == pytest.approx(0.0, abs=1e-6)

def test_release_after_last_word(): ...  # shp(end) == 1, shp(end + release) == 0

def test_crossfade_is_equal_power():
    w = weights([P(rec_at=0, dur=10), P(rec_at=8, dur=10)], 20000, 1000)
    mid = 9000
    assert w[0, mid] ** 2 + w[1, mid] ** 2 == pytest.approx(1.0, abs=1e-6)

def test_no_music_means_room_untouched():
    rec = noise(1000); out = render(rec, zeros((1000, 2)), zeros(1000), zeros(1000), 1000, MixSettings())
    assert np.allclose(out[:, 0], rec)

def test_room_mode_mutes_track_under_voice(): ...  # mode="room", shp=1 → clean == 0

def test_place_applies_rate_and_offset(): ...  # імпульс у треку на 1.0 с, rec_at=2, rate=1.001 → пік на 3.001 с

@needs_ffmpeg
def test_mix_files_blocks_match_whole(tmp_path):
    # сцена bench, block=5 с проти block=1000 с: різниця < 1e-4 (поля блоків прибирають шви)
```

- [ ] **Step 2: Run, expect FAIL.**
- [ ] **Step 3: Implement** `mix.py`.
- [ ] **Step 4: Run, expect PASS.**
- [ ] **Step 5: Commit** — `Додати мікс: дакінг із передзаглядом, кросфейди, підкладка зали`.

---

### Task 7: Preflight `soundtrack.json` і рендер з `audio_source`

**Files:**
- Create: `src/videoworks/soundtrack/check.py`
- Modify: `src/videoworks/models.py` (`AudioSettings.music()`)
- Modify: `src/videoworks/render.py` (`build_filtergraph`, `cut`)
- Modify: `src/videoworks/audio.py` (`plan` приймає шлях джерела звуку)
- Test: `tests/test_soundtrack_check.py`, доповнити `tests/test_audio.py`

**Interfaces:**
- Consumes: `Soundtrack`, `Problem`, `render.sha256`.
- Produces:
  - `check.preflight(st: Soundtrack, *, duration: float, root: Path) -> list[Problem]` — помилки й попередження строго за розділом «Помилки й перевірки» спеку (пороги: `confidence < 3`, `sync_error_ms > 2`, перекриття > 15 с, `rate` поза 1 ± 500 ppm; хеш не збігається → error; `suspects` не порожні → warning).
  - `AudioSettings.music() -> AudioSettings` — `deesser=None`, `compressor=Compressor(threshold_db=-14, ratio=2.0, attack_ms=20, release_ms=250)`, решта дефолтна.
  - `build_filtergraph(edl, *, with_audio=True, audio_plan=None)` — якщо `edl.audio_source`: `[1:a]atrim=…,asetpts=PTS-STARTPTS,afade=t=in:d=0.01,afade=t=out:st=<dur−0.01>:d=0.01[a{i}]`; інакше рядок не змінюється.
  - `render.cut(..., audio: Path | None = None)` — додає `-i audio` другим входом; `audio.plan(audio or video, edl)`; `Receipt.inputs` включає хеш `audio`. Якщо `edl.audio_source` задано, а `"audio"` не в `edl.model_fields_set`, — `edl.audio = AudioSettings.music()`.

- [ ] **Step 1: Write the failing tests**

```python
def test_graph_without_audio_source_unchanged():
    edl = from_spans("x", [(0, 1), (2, 3)])
    assert build_filtergraph(edl) == GOLDEN   # рядок, знятий з поточної версії ДО змін

def test_graph_with_audio_source_reads_second_input():
    g = build_filtergraph(edl.model_copy(update={"audio_source": "media/mix.wav"}))
    assert "[1:a]atrim" in g and "[0:a]" not in g and "afade=t=out" in g

def test_music_profile_has_no_deesser():
    assert AudioSettings.music().deesser is None

def test_preflight_flags(tmp_path):
    # по одному Soundtrack на кожне правило: поза записом, нема файлу, чужий хеш,
    # rate 1.001, перекриття 20 с → error; confidence 2, alternative, sync_error 3 мс, suspects → warning
```

- [ ] **Step 2:** Перед змінами `render.py` зняти `GOLDEN` поточного виходу в тест. Run, expect FAIL на нових тестах.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** `uv run pytest -q` — уся решта зелена (зокрема старі `test_audio.py`, `test_edl.py`).
- [ ] **Step 5: Commit** — `Рендер бере звук із audio_source; preflight для soundtrack.json`.

---

### Task 8: CLI, стенд як команда, документація

**Files:**
- Modify: `src/videoworks/cli.py` (команди `sync`, `voice`, `mix`, `soundtrack-bench`; `cut` передає `audio`)
- Modify: `src/videoworks/soundtrack/bench.py` (`write_project`)
- Modify: `.claude/skills/videoworks/SKILL.md`, `docs/roadmap.md` (М8), `README.md` (команди)
- Test: `tests/test_soundtrack_cli.py`

**Interfaces:**
- Consumes: усе вище; `registry.build`, `ingest.extract_audio`, `render.sha256`.
- Produces (CLI):
  - `sync PROJECT [--track PATH ...] [--nudge INDEX SECONDS]` — запис із `project.audio` (16 кГц моно; витягти, якщо нема), треки з `project.track_files()`; `voice` — слова транскрипту, якщо він є; `previous` — з наявного `soundtrack.json`; `track` — шлях відносно кореня проєкту, `track_sha256` — `render.sha256`; зберігає, друкує таблицю (трек, `rec_at`, дрейф ppm, впевненість, похибка мс, альтернатива) і preflight. `--nudge` лише зсуває `rec_at` розміщення `INDEX` на `SECONDS`, ставить `locked=True`, без перерахунку.
  - `voice PROJECT [--backend --model]` — кеш `media/tracks/<stem>.transcript.json` (ASR лише якщо кешу нема), `drop_lyrics`, `spans(hold=mix.hold)`, `suspects` (predicted = сума `place` на 16 кГц); без транскрипту — попередження й порожні `spans`.
  - `mix PROJECT [--ab] [--force]` — preflight; при error без `--force` — exit 1, файл не пишеться; пише `project.mix` (і `project.sync_ab`), друкує підказку додати `"audio_source": "media/mix.wav"` в `edl.json`.
  - `soundtrack-bench [--seed N] [--periodic] [--project NAME]` — `make_scene` → `sync` → `score`, таблиця з порогами й ✓/✗; `--project` ще й пише проєкт через `write_project` для наскрізного прогону.
  - `cut` — якщо `plan.audio_source`, передає `audio=project.root / plan.audio_source`.

- [ ] **Step 1: Write the failing tests** (typer `CliRunner`, `@needs_ffmpeg`)

```python
def test_end_to_end_on_bench_project(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    run("soundtrack-bench", "--project", "bench")
    run("sync", "bench"); run("voice", "bench"); run("mix", "bench", "--ab")
    st = Soundtrack.load(tmp_path / "projects/bench/soundtrack.json")
    assert len(st.placements) == 2 and st.voice.lyrics_dropped > 0
    assert (tmp_path / "projects/bench/media/mix.wav").exists()
    # EDL з audio_source → cut проходить, receipt містить хеш mix.wav

def test_nudge_locks_and_shifts(tmp_path): ...

def test_mix_refuses_on_preflight_error(tmp_path): ...  # зіпсований хеш → exit 1, mix.wav не створено
```

- [ ] **Step 2: Run, expect FAIL.**
- [ ] **Step 3: Implement** CLI і `write_project`.
- [ ] **Step 4: Run** `uv run pytest -q` → PASS; вручну `uv run videoworks soundtrack-bench` — усі пороги ✓; прослухати `out/sync-ab.wav` бенч-проєкту не обовʼязково, але цифри з таблиці записати в roadmap.
- [ ] **Step 5: Docs.** `SKILL.md`: крок «реконструкція звуку» — коли пропонувати (музика з колонок, є файли треків), порядок `sync → voice → mix → audio_source → cut`, як читати `alternative` (запропонувати `--nudge` на ± значення, просити людину прослухати `sync-ab.wav`) і `suspects`. Roadmap: розділ М8 зі станом і цифрами стенда. README: рядки команд.
- [ ] **Step 6: Commit** — `Додати команди sync, voice, mix і стенд реконструкції звуку`.

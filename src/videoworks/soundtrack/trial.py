"""Обмежений пілот сінку й дакінгу; не замінює повний погоджений дизайн.

Один трек і короткий уривок без переходу між піснями. Час усередині
артефактів локальний до уривка; start зберігається окремо у звіті.
"""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
import sys
import wave
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from difflib import SequenceMatcher
from pathlib import Path

import numpy as np
from scipy import signal, stats

from videoworks.ingest import _binary
from videoworks.models import Strikes, Transcript

SYNC_RATE = 8_000
FEATURE_RATE = 100
# Рампи викреслення лежать усередині відрізка: запас pad уже їх вміщує.
STRIKE_RAMP = 0.04


def load(
    path: Path, rate: int, *, start: float = 0, duration: float | None = None, channels: int = 1
) -> np.ndarray:
    """Декодує потрібне вікно, не завантажуючи годинне стерео в памʼять."""
    args = [_binary("ffmpeg"), "-nostdin", "-v", "error", "-ss", str(start), "-i", str(path)]
    if duration is not None:
        args += ["-t", str(duration)]
    args += ["-vn", "-ac", str(channels), "-ar", str(rate), "-f", "f32le", "-"]
    result = subprocess.run(args, capture_output=True, check=False)
    if result.returncode:
        raise RuntimeError(result.stderr.decode("utf-8", errors="replace"))
    samples = np.frombuffer(result.stdout, dtype="<f4").copy()
    if not len(samples):
        raise ValueError(f"Порожнє аудіовікно: {path}")
    return samples if channels == 1 else samples.reshape(-1, channels)


def write_wav(path: Path, samples: np.ndarray, rate: int) -> None:
    """PCM 24 біти; захист від непоміченого кліпінгу."""
    if not np.isfinite(samples).all() or np.max(np.abs(samples)) >= 1:
        raise ValueError("Вихід містить кліпінг або нечислові семпли")
    path.parent.mkdir(parents=True, exist_ok=True)
    integers = np.rint(samples * 8388607).astype("<i4").reshape(-1)
    packed = integers.view(np.uint8).reshape(-1, 4)[:, :3].copy().tobytes()
    with wave.open(str(path), "wb") as out:
        out.setnchannels(1 if samples.ndim == 1 else samples.shape[1])
        out.setsampwidth(3)
        out.setframerate(rate)
        out.writeframes(packed)


def onsets(samples: np.ndarray) -> np.ndarray:
    """Логарифмічний спектральний потік із вирівнюванням частотних смуг."""
    _, _, spectrum = signal.stft(samples, fs=SYNC_RATE, nperseg=256, noverlap=176, boundary="zeros")
    power = np.log1p(np.abs(spectrum) * 1000)
    power /= np.maximum(np.std(power, axis=1, keepdims=True), 0.1)
    flux = np.maximum(np.diff(power, axis=1, prepend=power[:, :1]), 0).mean(axis=0)
    return (flux - np.mean(flux)).astype(np.float32)


def coarse_offset(room: np.ndarray, track: np.ndarray) -> tuple[float, float, float]:
    """Нормована кореляція з мінімальним перекриттям: краї не можуть перемогти."""
    r, t = onsets(room), onsets(track)
    cross = signal.correlate(r, t, mode="full", method="fft")
    energy_r = signal.correlate(r * r, np.ones(len(t)), method="fft")
    energy_t = signal.correlate(np.ones(len(r)), t * t, method="fft")
    overlap = signal.correlate(np.ones(len(r)), np.ones(len(t)), method="fft")
    scores = cross / np.sqrt(np.maximum(energy_r * energy_t, 1e-12))
    minimum = min(len(r), len(t)) * 0.8
    scores[overlap < minimum] = -np.inf
    index = int(np.argmax(scores))
    peak = float(scores[index])
    other = scores.copy()
    other[max(0, index - 10) : index + 11] = -np.inf
    runner_up = float(np.max(other))
    lag = index - len(t) + 1
    return lag / FEATURE_RATE, peak, runner_up


@dataclass
class Match:
    track: str
    offset: float
    rate: float
    onset_correlation: float
    runner_up_correlation: float
    waveform_correlation: float
    residual_ms: float
    windows: list[dict]


def refine(room: np.ndarray, track: np.ndarray, offset: float) -> tuple[float, float, list]:
    """Короткі вікна уточнюють зсув; робастна пряма оцінює дрейф."""
    band = signal.butter(3, [250, 3000], btype="bandpass", fs=SYNC_RATE, output="sos")
    r = signal.sosfiltfilt(band, room)
    t = signal.sosfiltfilt(band, track)
    windows = []
    half, search = 2 * SYNC_RATE, int(0.16 * SYNC_RATE)
    for room_time in np.arange(6, len(r) / SYNC_RATE - 4, 4):
        ri = round(room_time * SYNC_RATE)
        ti = round((room_time - offset) * SYNC_RATE)
        if ti - half < 0 or ti + half > len(t):
            continue
        template = t[ti - half : ti + half]
        actual = r[ri - half - search : ri + half + search]
        corr = signal.correlate(actual, template, mode="valid", method="fft")
        energies = signal.convolve(
            actual * actual, np.ones(len(template)), mode="valid", method="fft"
        )
        corr /= np.sqrt(np.maximum(energies * np.sum(template * template), 1e-18))
        fft_size = 1 << (len(actual) + len(template) - 1).bit_length()
        spectrum = np.fft.rfft(actual, fft_size) * np.conj(np.fft.rfft(template, fft_size))
        whitened = spectrum / np.maximum(np.abs(spectrum), 1e-12) ** 0.75
        gcc = np.fft.irfft(whitened, fft_size)[: 2 * search + 1]
        peak = int(np.argmax(np.abs(gcc)))
        strength = float(abs(corr[peak]))
        if peak == 0 or peak == len(corr) - 1 or strength < 0.04:
            continue
        parabola = np.abs(gcc[peak - 1 : peak + 2])
        denom = parabola[0] - 2 * parabola[1] + parabola[2]
        fraction = 0.5 * (parabola[0] - parabola[2]) / denom if abs(denom) > 1e-12 else 0
        delay = (peak + fraction - search) / SYNC_RATE
        windows.append(
            {"track_time": ti / SYNC_RATE, "offset": offset + delay, "correlation": strength}
        )
    if len(windows) < 4:
        raise ValueError("Замало незалежних вікон для перевірки сінку")
    times = np.array([w["track_time"] for w in windows])
    offsets = np.array([w["offset"] for w in windows])
    # Відлуння може дати сильніший пік за прямий сигнал. Шукаємо пряму,
    # підтверджену багатьма вікнами, а не усереднюємо різні акустичні шляхи.
    keep = np.zeros(len(times), dtype=bool)
    best_score = (-1, -1.0)
    for i in range(len(times)):
        for j in range(i + 1, len(times)):
            slope = (offsets[j] - offsets[i]) / (times[j] - times[i])
            if abs(slope) > 0.0005:
                continue
            intercept = offsets[i] - slope * times[i]
            inliers = np.abs(offsets - intercept - slope * times) < 0.003
            score = (
                int(inliers.sum()),
                sum(w["correlation"] for w, ok in zip(windows, inliers, strict=True) if ok),
            )
            if score > best_score:
                keep, best_score = inliers, score
    if np.count_nonzero(keep) < max(4, len(windows) * 0.4):
        raise ValueError("Немає узгодженого сінку хоча б у 40% вікон")
    slope, intercept, _, _ = stats.theilslopes(offsets[keep], times[keep])
    for i, window in enumerate(windows):
        window["used"] = bool(keep[i])
        window["residual_ms"] = float((offsets[i] - intercept - slope * times[i]) * 1000)
    return float(intercept), float(1 + slope), windows


def find_match(room: np.ndarray, tracks: list[Path], progress=print) -> tuple[Match, list[dict]]:
    candidates = []
    for path in tracks:
        track = load(path, SYNC_RATE)
        offset, peak, runner = coarse_offset(room, track)
        candidates.append(
            {"track": str(path), "offset": offset, "correlation": peak, "runner_up": runner}
        )
        progress(f"{path.name}: кореляція {peak:.3f}, зсув {offset:.3f} с")
    candidates.sort(key=lambda c: c["correlation"], reverse=True)
    best = candidates[0]
    if best["correlation"] < 0.08 or (
        len(candidates) > 1 and best["correlation"] < candidates[1]["correlation"] * 1.3
    ):
        raise ValueError("Не знайдено переконливого збігу музики")
    track = load(Path(best["track"]), SYNC_RATE)
    offset, rate, windows = refine(room, track, best["offset"])
    used = [w for w in windows if w["used"]]
    residual_ms = float(np.median([abs(w["residual_ms"]) for w in used]))
    waveform = float(np.median([w["correlation"] for w in used]))
    if abs(rate - 1) > 0.0005 or residual_ms > 5 or waveform < 0.08:
        raise ValueError(
            f"Сінк ненадійний: rate={rate:.8f}, похибка={residual_ms:.2f} мс, "
            f"кореляція={waveform:.3f}; автоматичний мікс зупинено"
        )
    return Match(
        best["track"],
        offset,
        rate,
        best["correlation"],
        best["runner_up"],
        waveform,
        residual_ms,
        windows,
    ), candidates


def save_json(path: Path, data: dict) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def sync_trial(project, *, start: float = 0, seconds: float = 120, progress=print):
    """Зберігає незалежний від основного проєкту звіт короткого тесту."""
    if start < 0 or not 30 <= seconds <= 180:
        raise ValueError("Пілот підтримує 30–180 с, start має бути невідʼємним")
    output = project.out / "soundtrack-test"
    output.mkdir(parents=True, exist_ok=True)
    room = load(project.source_video(), SYNC_RATE, start=start, duration=seconds)
    tracks = sorted((project.media / "tracks").glob("*.m4a"))
    if not tracks:
        raise ValueError("Немає треків у media/tracks")
    match, candidates = find_match(room, tracks, progress)
    report = {
        "experimental": True,
        "source": str(project.source_video()),
        "start": start,
        "seconds": len(room) / SYNC_RATE,
        "match": asdict(match),
        "candidates": candidates,
    }
    save_json(output / "sync.json", report)
    return match, report


def voice_spans(
    room: Transcript, lyrics: Transcript, struck: Sequence[tuple[float, float]] = ()
) -> tuple[list[tuple[float, float]], int]:
    """Збіг слів лірики за текстом і часом не повинен пригинати музику.

    Викреслене слово голосом не вважається: на ньому кімната й так вимкнена,
    а пригинати музику під ним означало б глушити те, що має звучати повністю.
    """

    def text(word):
        return re.sub(r"[^\w]", "", word.w.lower())

    kept, dropped = [], 0
    for word in room.words():
        if not text(word) or word.prob is not None and word.prob < 0.2:
            continue
        middle = (word.start + word.end) / 2
        if any(start <= middle <= end for start, end in struck):
            continue
        if any(
            abs(word.start - lyric.start) <= 0.3
            and SequenceMatcher(None, text(word), text(lyric)).ratio() >= 0.6
            for lyric in lyrics.words()
            if text(lyric)
        ):
            dropped += 1
        else:
            kept.append(word)
    spans = []
    for word in sorted(kept, key=lambda w: w.start):
        if spans and word.start - spans[-1][1] < 0.6:
            spans[-1] = (spans[-1][0], max(spans[-1][1], word.end))
        else:
            spans.append((word.start, word.end))
    return spans, dropped


def no_voice_message(struck: Sequence[tuple[float, float]]) -> str:
    """Чому пілот зупинився без голосу.

    Якщо в уривку є викреслене, голос міг зникнути саме через нього — людина
    має бачити, що причина в її позначках, а не в записі чи ASR.
    """
    message = "Голос не знайдено: тест дакінгу не можна вважати проведеним"
    if struck:
        message += (
            f". В уривку викреслено відрізків: {len(struck)} — можливо, вони "
            "прибрали весь голос; візьми інший --start"
        )
    return message


def duck_curve(length: int, rate: int, spans: list[tuple[float, float]]) -> np.ndarray:
    """Атака закінчується за 200 мс до слова; перетини не відкривають музику."""
    curve = np.zeros(length, dtype=np.float32)
    for start, end in spans:
        attack_start = start - 0.28
        release_start, release_end = end + 0.6, end + 1.2
        lo, hi = max(0, int(attack_start * rate)), min(length, int(release_end * rate) + 1)
        times = np.arange(lo, hi) / rate
        attack = 0.5 - 0.5 * np.cos(np.pi * np.clip((times - attack_start) / 0.08, 0, 1))
        release = 0.5 + 0.5 * np.cos(np.pi * np.clip((times - release_start) / 0.6, 0, 1))
        curve[lo:hi] = np.maximum(curve[lo:hi], np.minimum(attack, release))
    return curve


def local_spans(
    spans: Sequence[tuple[float, float]], start: float, seconds: float
) -> list[tuple[float, float]]:
    """Відрізки сирого таймлайна → координати вікна пілота, обрізані по його краях."""
    result = []
    for lo, hi in spans:
        lo, hi = max(lo - start, 0.0), min(hi - start, seconds)
        if hi > lo:
            result.append((lo, hi))
    return result


def strike_curve(length: int, rate: int, spans: Sequence[tuple[float, float]]) -> np.ndarray:
    """1 усередині відрізка, косинусні рампи лише всередині: назовні нічого не тече.

    Короткий відрізок стискає рампи навпіл, щоб однаково дійти до повної тиші.
    """
    curve = np.zeros(length, dtype=np.float64)
    for start, end in spans:
        ramp = min(STRIKE_RAMP, (end - start) / 2)
        lo, hi = max(0, int(start * rate)), min(length, int(end * rate) + 1)
        times = np.arange(lo, hi) / rate
        edge = np.minimum(times - start, end - times) / ramp
        shape = 0.5 - 0.5 * np.cos(np.pi * np.clip(edge, 0, 1))
        curve[lo:hi] = np.maximum(curve[lo:hi], shape)
    return curve.astype(np.float32)


def gains(
    duck: np.ndarray, covered: np.ndarray, strike: np.ndarray
) -> tuple[np.ndarray, np.ndarray]:
    """(room_gain, music_gain); вхідні масиви не змінюються."""
    music_gain = 10 ** ((-12 * duck) / 20)
    room_gain = 10 ** ((-24 * (1 - duck)) / 20)
    # Поза покриттям треку залишається лише оригінальний запис.
    room_gain[~covered] = 1
    music_gain[~covered] = 0
    # Повна музика лише там, де вона є; кімната в нуль і без покриття.
    music_gain = music_gain + (1 - music_gain) * strike * covered
    room_gain = room_gain * (1 - strike)
    return room_gain, music_gain


def quiet_windows(
    duck: np.ndarray, covered: np.ndarray, strike: np.ndarray, rate: int
) -> list[slice]:
    """Секундні вікна без голосу й без викресленого: за ними підбирається гучність.

    У викресленому вікні кімната ще містить голос, який вилучено зі spans, тож
    воно роздуло б original_rms і підігнало б чисту музику занадто гучною.
    """
    return [
        slice(i, i + rate)
        for i in range(0, len(duck) - rate, rate)
        if np.max(duck[i : i + rate]) < 0.01
        and np.max(strike[i : i + rate]) == 0
        and np.all(covered[i : i + rate])
    ]


def uncovered_strikes(
    covered: np.ndarray, rate: int, spans: Sequence[tuple[float, float]]
) -> list[list[float]]:
    """Стрічки викресленого, де трек не покриває (там тиша), злиті в інтервали."""
    inside = np.zeros(len(covered), dtype=bool)
    for start, end in spans:
        inside[max(0, int(start * rate)) : max(0, int(end * rate))] = True
    gap = np.concatenate([[False], inside & ~covered, [False]]).astype(np.int8)
    edges = np.flatnonzero(np.diff(gap))
    return [[round(a / rate, 3), round(b / rate, 3)] for a, b in zip(edges[::2], edges[1::2])]


def aligned_audio(
    path: Path, match: Match, seconds: float, rate: int, channels: int = 2
) -> tuple[np.ndarray, np.ndarray]:
    """Ресемпл для короткого пілота; поза треком повертає нулі й маску покриття."""
    first = max(0, -match.offset / match.rate - 0.01)
    last = (seconds - match.offset) / match.rate + 0.01
    track = load(path, rate, start=first, duration=last - first, channels=channels)
    coordinates = (np.arange(round(seconds * rate)) / rate - match.offset) / match.rate
    coordinates = (coordinates - first) * rate
    valid = (coordinates >= 0) & (coordinates < len(track) - 1)
    if channels == 1:
        result = np.interp(coordinates, np.arange(len(track)), track, left=0, right=0)
    else:
        result = np.column_stack(
            [
                np.interp(coordinates, np.arange(len(track)), track[:, ch], left=0, right=0)
                for ch in range(channels)
            ]
        )
    return result.astype(np.float32), valid


def _transcribe(path: Path, backend, progress) -> Transcript:
    target = path.with_suffix(".transcript.json")
    key_path = path.with_suffix(".asr-key")
    key = hashlib.sha256(path.read_bytes()).hexdigest() + ":" + backend.model_name
    if target.exists() and key_path.exists() and key_path.read_text() == key:
        return Transcript.load(target)
    transcript = backend.transcribe(
        path, on_segment=lambda s: progress(f"{path.name} {s.start:.1f}–{s.end:.1f}: {s.text}")
    )
    transcript.save(target)
    key_path.write_text(key)
    return transcript


def run_trial(
    project,
    *,
    start: float = 0,
    seconds: float = 120,
    sync_only: bool = False,
    video: bool = False,
    progress=print,
) -> dict:
    """Сінк, незалежний ASR кімнати та лірики, WAV і необовʼязкові відеопроби."""
    # Зламаний файл позначок зупиняє тест до важкої роботи, а не після неї.
    struck_raw = None
    if project.strikes.exists():
        try:
            struck_raw = Strikes.load(project.strikes).spans
        except Exception as error:
            raise ValueError(f"Не вдалося прочитати {project.strikes.name}: {error}") from error
    match, report = sync_trial(project, start=start, seconds=seconds, progress=progress)
    if sync_only:
        return report
    from videoworks import render
    from videoworks.asr.faster_whisper_backend import FasterWhisperBackend
    from videoworks.edl import from_spans
    from videoworks.models import AudioSettings

    output = project.out / "soundtrack-test"
    seconds = report["seconds"]
    room_asr = load(project.source_video(), 16000, start=start, duration=seconds)
    clean_asr, _ = aligned_audio(Path(match.track), match, seconds, 16000, channels=1)
    # Нормалізація лише ASR-копій не впливає на гучність порівняння.
    for name, samples in (("room", room_asr), ("lyrics", clean_asr)):
        samples = samples * min(1, 0.95 / max(float(np.max(np.abs(samples))), 1e-12))
        write_wav(output / f"{name}.asr.wav", samples, 16000)
    backend = FasterWhisperBackend()
    room_text = _transcribe(output / "room.asr.wav", backend, progress)
    lyric_text = _transcribe(output / "lyrics.asr.wav", backend, progress)
    # Слова ASR кімнати вже в координатах вікна, тож і викреслені — ввіконні.
    struck = local_spans(struck_raw or [], start, seconds)
    spans, dropped = voice_spans(room_text, lyric_text, struck)
    if not spans:
        raise ValueError(no_voice_message(struck))
    room = load(project.source_video(), 48000, start=start, duration=seconds, channels=2)
    clean, covered = aligned_audio(Path(match.track), match, seconds, 48000)
    n = min(len(room), len(clean))
    room, clean, covered = room[:n], clean[:n], covered[:n]
    curve = duck_curve(n, 48000, spans)
    strike = strike_curve(n, 48000, struck)
    ratios = []
    for window in quiet_windows(curve, covered, strike, 48000):
        original_rms = float(np.sqrt(np.mean(room[window] ** 2)))
        clean_rms = float(np.sqrt(np.mean(clean[window] ** 2)))
        if clean_rms > 0.001:
            ratios.append(original_rms / clean_rms)
    if len(ratios) < 3:
        raise ValueError("Замало ділянок без голосу для незалежного підбору гучності")
    gain = float(np.median(ratios))
    clean *= gain
    room_gain, music_gain = gains(curve, covered, strike)
    mixed = room * room_gain[:, None] + clean * music_gain[:, None]
    common_gain = min(
        1, 0.89 / max(float(np.max(np.abs(mixed))), float(np.max(np.abs(room))), 1e-12)
    )
    room *= common_gain
    mixed *= common_gain
    clean *= common_gain
    write_wav(output / "before.wav", room, 48000)
    write_wav(output / "after.wav", mixed, 48000)
    write_wav(
        output / "sync-ab.wav", np.column_stack([room.mean(axis=1), clean.mean(axis=1)]), 48000
    )
    report.update(
        voice={
            "spans": spans,
            "words": len(room_text.words()),
            "lyrics_dropped": dropped,
            "language": room_text.language,
            "uncertain_words": [
                w.model_dump()
                for w in room_text.words()
                if (w.prob is not None and w.prob < 0.5) or w.end - w.start > 1.5
            ],
        },
        mix={
            "music_gain_db": float(20 * np.log10(gain)),
            "duck_db": -12,
            "room_floor_db": -24,
            "common_gain_db": float(20 * np.log10(common_gain)),
            "gain_windows": len(ratios),
            "peak": float(np.max(np.abs(mixed))),
        },
        limitations=[
            "Немає еталонних ручних таймкодів: похибка вікон не є абсолютною точністю",
            "ASR-мітки голосу потребують перевірки на слух",
            "Голос не відділяється від кімнатної музики",
            "Один трек: переходи та повний запис не перевірялися",
        ],
    )
    if struck_raw is not None:
        report["strikes"] = {
            "spans": [[round(a, 3), round(b, 3)] for a, b in struck],
            "uncovered": uncovered_strikes(covered, 48000, struck),
        }
    report["inputs"] = {
        str(p): render.sha256(p) for p in (project.source_video(), Path(match.track))
    }
    if video:
        plan = from_spans(
            str(project.source_video()), [(start, start + seconds)], AudioSettings(normalize=None)
        )
        plan.save(output / "edl.json")
        for name in ("before", "after"):
            progress(f"Рендер {name}.mp4…")
            _, receipt = render.cut(
                project.source_video(),
                plan,
                output / f"{name}.mp4",
                # NVENC є лише з картою NVIDIA; на Mac його немає зовсім.
                encoder="nvenc" if sys.platform in ("win32", "linux") else "libx264",
                quality=23,
                audio_override=output / f"{name}.wav",
                audio_origin=start,
            )
            receipt.save(output / f"{name}.receipt.json")
    save_json(output / "report.json", report)
    return report

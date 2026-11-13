"""Bounded recent JPEGs for visual context, without camera or cloud ownership.

Call the public methods on one asyncio event loop. ``offer`` does no decoding:
it only replaces one pending frame. A single worker scores at most two frames
per second in a thread. Times describe receipt by this PC, not sensor exposure.
Quality scores are heuristics: texture, noise and resizing affect sharpness.
"""
from __future__ import annotations

import asyncio
from collections import deque
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
from io import BytesIO
import math
import time
import uuid


MAX_JPEG_BYTES = 2 * 1024 * 1024
MAX_HELD_JPEG_BYTES = 12 * 1024 * 1024
MAX_CACHE_FRAMES = 16
RETENTION_SECONDS = 8.0
SCORE_INTERVAL_SECONDS = 0.5
QUALITY_SIZE = (96, 96)
MAX_SOURCE_PIXELS = 16 * 1024 * 1024
MAX_SOURCE_EDGE = 8192
HEURISTIC_WARNING = 'quality_is_heuristic_texture_and_noise_affect_sharpness'


@dataclass
class Selection:
    images: list[dict]
    candidate_count: int
    reason: str
    warnings: list[str]
    selection_id: str

    def public(self) -> dict:
        """JSON-safe diagnostics; JPEGs and encodings of their contents stay out."""
        return {
            'selected': [deepcopy({key: image[key] for key in (
                'frame_id', 'received_utc', 'age_seconds', 'quality', 'sha256')
                if key in image}) for image in self.images],
            'candidate_count': self.candidate_count,
            'reason': self.reason,
            'warnings': list(self.warnings),
            'selection_id': self.selection_id,
        }


@dataclass(frozen=True)
class _Pending:
    jpeg: bytes
    frame_id: str
    received_monotonic: float
    received_utc: str
    sequence: int


@dataclass(frozen=True)
class _Analysis:
    quality: dict
    sha256: str
    dhash: int
    thumbnail: tuple[int, ...]
    appearance: tuple[int, ...]


@dataclass(frozen=True)
class _Frame:
    source: _Pending
    analysis: _Analysis


def _laplacian_variance(pixels: list[int], width: int, height: int) -> float:
    """Variance of signed 4-neighbour Laplacians, without 8-bit clipping."""
    total = squares = count = 0
    for y in range(1, height - 1):
        row = y * width
        for x in range(1, width - 1):
            index = row + x
            value = (4 * pixels[index] - pixels[index - 1] - pixels[index + 1]
                     - pixels[index - width] - pixels[index + width])
            total += value
            squares += value * value
            count += 1
    return max(0.0, squares / count - (total / count) ** 2) if count else 0.0


def _score_jpeg(jpeg: bytes) -> _Analysis:
    # Import in the worker so a missing optional dependency produces diagnostics,
    # rather than preventing the rest of the console from importing this module.
    from PIL import Image

    with Image.open(BytesIO(jpeg)) as image:
        if image.format != 'JPEG':
            raise ValueError('Expected JPEG data')
        width, height = image.size
        if width < 3 or height < 3 or max(width, height) > MAX_SOURCE_EDGE \
                or width * height > MAX_SOURCE_PIXELS:
            raise ValueError('JPEG dimensions exceed the scoring limits')
        # JPEG draft decoding reduces memory before making the fixed-size probe.
        image.draft('RGB', QUALITY_SIZE)
        with image.convert('RGB') as rgb:
            with rgb.resize((8, 8), Image.Resampling.BILINEAR) as probe:
                appearance = tuple(probe.tobytes())
        with image.convert('L') as converted:
            with converted.resize(QUALITY_SIZE, Image.Resampling.BILINEAR) as gray:
                pixels = list(gray.tobytes())
                histogram = gray.histogram()
                with gray.resize((9, 8), Image.Resampling.BILINEAR) as probe:
                    gradients = list(probe.tobytes())
                with gray.resize((8, 8), Image.Resampling.BILINEAR) as probe:
                    thumbnail = tuple(probe.tobytes())

    count = len(pixels)
    mean = sum(value * frequency for value, frequency in enumerate(histogram)) / count
    variance = (sum(value * value * frequency for value, frequency in enumerate(histogram))
                / count - mean * mean)
    deviation = math.sqrt(max(0.0, variance))
    laplacian = _laplacian_variance(pixels, *QUALITY_SIZE)
    dark = sum(histogram[:26]) / count
    bright = sum(histogram[230:]) / count
    clipped_black = sum(histogram[:6]) / count
    clipped_white = sum(histogram[250:]) / count
    low_texture = deviation < 10.0
    underexposed = mean < 35.0 or dark > 0.8
    overexposed = mean > 225.0 or bright > 0.8
    # Reject only near-blank extremes, not all flat walls or dark scenes.
    very_dark = mean < 12.0 and dark > 0.95
    very_bright = mean > 247.0 and bright > 0.95
    sharpness = min(1.0, math.log1p(laplacian) / math.log1p(2500.0))
    exposure = min(1.0, max(0.0, (mean - 8.0) / 32.0),
                   max(0.0, (247.0 - mean) / 32.0))
    score = (0.2 + 0.8 * sharpness) * exposure
    warnings = []
    if low_texture:
        warnings.append('low_texture_sharpness_is_inconclusive')
    elif laplacian < 30.0:
        warnings.append('low_sharpness')
    if underexposed:
        warnings.append('underexposed')
    if overexposed:
        warnings.append('overexposed')
    dhash = 0
    for y in range(8):
        for x in range(8):
            dhash = (dhash << 1) | (gradients[y * 9 + x] > gradients[y * 9 + x + 1])
    quality = {
        'source_width': width, 'source_height': height,
        'analysis_width': QUALITY_SIZE[0], 'analysis_height': QUALITY_SIZE[1],
        'laplacian_variance': round(laplacian, 3),
        'luma_mean': round(mean, 3), 'luma_stddev': round(deviation, 3),
        'dark_fraction': round(dark, 5), 'bright_fraction': round(bright, 5),
        'clipped_black_fraction': round(clipped_black, 5),
        'clipped_white_fraction': round(clipped_white, 5),
        'low_texture': low_texture, 'underexposed': underexposed,
        'overexposed': overexposed, 'very_dark': very_dark,
        'very_bright': very_bright, 'usable': not (very_dark or very_bright),
        'score': round(score, 5), 'dhash': f'{dhash:016x}',
        'heuristic': True, 'warnings': warnings,
    }
    return _Analysis(quality, hashlib.sha256(jpeg).hexdigest(), dhash, thumbnail, appearance)


def _near_duplicate(left: _Analysis, right: _Analysis) -> bool:
    if left.sha256 == right.sha256:
        return True
    if (left.dhash ^ right.dhash).bit_count() > 4:
        return False
    # dHash alone confuses differently exposed flat pictures. Require a similar
    # thumbnail too; neither fingerprint claims semantic equivalence.
    difference = sum(abs(a - b) for a, b in zip(left.thumbnail, right.thumbnail))
    return difference / len(left.thumbnail) <= 8.0


def _appearance_changed(older: _Analysis, latest: _Analysis) -> bool:
    """Reject obviously changed coarse appearance; this is not scene semantics."""
    difference = sum(abs(a - b) for a, b in zip(older.appearance, latest.appearance))
    mean_difference = difference / len(older.appearance)
    hash_difference = (older.dhash ^ latest.dhash).bit_count()
    return mean_difference > 24.0 or (mean_difference > 10.0 and hash_difference > 24)


class VisionFrames:
    def __init__(self, *, max_cache_frames: int = MAX_CACHE_FRAMES,
                 max_bytes: int = MAX_HELD_JPEG_BYTES,
                 retention_seconds: float = RETENTION_SECONDS):
        if isinstance(max_cache_frames, bool) or not isinstance(max_cache_frames, int) \
                or not 1 <= max_cache_frames <= MAX_CACHE_FRAMES:
            raise ValueError('max_cache_frames must be between 1 and 16')
        if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) \
                or not 1024 <= max_bytes <= MAX_HELD_JPEG_BYTES:
            raise ValueError('max_bytes must be between 1024 and 12 MiB')
        if isinstance(retention_seconds, bool) or not isinstance(retention_seconds, (int, float)) \
                or not math.isfinite(retention_seconds) \
                or not 0 < retention_seconds <= RETENTION_SECONDS:
            raise ValueError('retention_seconds must be positive and at most 8')
        self._max_cache_frames = max_cache_frames
        self._max_bytes = max_bytes
        self._retention_seconds = float(retention_seconds)
        self._frames: deque[_Frame] = deque()
        self._cache_bytes = 0
        self._pending: _Pending | None = None
        self._processing: _Pending | None = None
        self._task: asyncio.Task | None = None
        self._running = False
        self._accepting = True
        self._wake = asyncio.Event()
        self._changed = asyncio.Event()
        self._lifecycle = asyncio.Lock()
        self._next_score_at = 0.0
        self._prefix = uuid.uuid4().hex[:12]
        self._last_received: float | None = None
        self._last_quality: dict | None = None
        self._last_error: str | None = None
        self._last_score_ms: float | None = None
        self._completed_sequence = 0
        self._successful_sequence = 0
        self._counts = {key: 0 for key in (
            'offered', 'accepted', 'scored', 'score_errors', 'decode_errors',
            'dependency_errors', 'superseded_pending', 'rejected_input',
            'rejected_oversize', 'rejected_timestamp', 'rejected_out_of_order',
            'dropped_stopped', 'dropped_expired', 'dropped_budget',
            'evicted_age', 'evicted_count', 'evicted_bytes', 'selections',
            'selection_duplicates_omitted',
        )}

    def _held_bytes(self) -> int:
        return (self._cache_bytes + (len(self._pending.jpeg) if self._pending else 0)
                + (len(self._processing.jpeg) if self._processing else 0))

    def _evict(self, category: str) -> None:
        self._cache_bytes -= len(self._frames.popleft().source.jpeg)
        self._counts[category] += 1

    def _prune(self, now: float) -> None:
        while self._frames and now - self._frames[0].source.received_monotonic > self._retention_seconds:
            self._evict('evicted_age')

    def _enforce_budget(self) -> None:
        while len(self._frames) > self._max_cache_frames:
            self._evict('evicted_count')
        while self._frames and self._held_bytes() > self._max_bytes:
            self._evict('evicted_bytes')
        if self._pending and self._held_bytes() > self._max_bytes:
            self._pending = None
            self._counts['dropped_budget'] += 1

    def offer(self, jpeg: bytes, received_monotonic: float | None = None) -> None:
        """Replace the pending JPEG without decoding or starting background work.

        Frames offered after ``stop`` are ignored until ``start``. Out-of-order,
        future (>250 ms), expired and oversized input is rejected with counters.
        """
        self._counts['offered'] += 1
        if not self._accepting:
            self._counts['dropped_stopped'] += 1
            return
        if not isinstance(jpeg, bytes) or not jpeg:
            self._counts['rejected_input'] += 1
            return
        if len(jpeg) > min(MAX_JPEG_BYTES, self._max_bytes):
            self._counts['rejected_oversize'] += 1
            return
        now = time.monotonic()
        stamp = now if received_monotonic is None else received_monotonic
        if isinstance(stamp, bool) or not isinstance(stamp, (int, float)) \
                or not math.isfinite(stamp) or stamp > now + 0.25:
            self._counts['rejected_timestamp'] += 1
            return
        if self._last_received is not None and stamp < self._last_received:
            self._counts['rejected_out_of_order'] += 1
            return
        self._last_received = float(stamp)
        self._prune(now)
        if now - stamp > self._retention_seconds:
            self._counts['dropped_expired'] += 1
            return
        if self._pending is not None:
            self._counts['superseded_pending'] += 1
        self._counts['accepted'] += 1
        received_utc = datetime.fromtimestamp(time.time() - (now - stamp), timezone.utc)
        self._pending = _Pending(
            jpeg, f'vf-{self._prefix}-{self._counts["accepted"]:08d}', float(stamp),
            received_utc.isoformat(timespec='milliseconds').replace('+00:00', 'Z'),
            self._counts['accepted'])
        self._enforce_budget()
        self._wake.set()

    async def start(self) -> None:
        async with self._lifecycle:
            if self._task is not None and not self._task.done():
                return
            self._running = self._accepting = True
            self._task = asyncio.create_task(self._worker(), name='vision-frame-quality')

    async def stop(self, clear: bool = True) -> None:
        """Wait for an in-flight decoder; it cannot repopulate a cleared cache."""
        async with self._lifecycle:
            self._running = self._accepting = False
            self._pending = None
            self._wake.set()
            self._changed.set()
            task = self._task
            try:
                if task is not None:
                    try:
                        await asyncio.shield(task)
                    except asyncio.CancelledError:
                        # A disconnected HTTP request must still release our
                        # worker and its JPEG before its cancellation propagates.
                        await asyncio.shield(task)
                        raise
            finally:
                self._task = None
                if clear:
                    self._frames.clear()
                    self._cache_bytes = 0
                    self._last_quality = None
                self._changed.set()

    async def _wait(self, delay: float | None = None) -> None:
        self._wake.clear()
        try:
            if delay is None:
                await self._wake.wait()
            else:
                await asyncio.wait_for(self._wake.wait(), max(0.001, delay))
        except asyncio.TimeoutError:
            pass

    async def _worker(self) -> None:
        try:
            while self._running:
                now = time.monotonic()
                self._prune(now)
                if self._pending is None:
                    delay = (self._frames[0].source.received_monotonic
                             + self._retention_seconds - now) if self._frames else None
                    await self._wait(delay)
                    continue
                if now < self._next_score_at:
                    await self._wait(self._next_score_at - now)
                    continue
                pending, self._pending = self._pending, None
                if now - pending.received_monotonic > self._retention_seconds:
                    self._counts['dropped_expired'] += 1
                    self._completed_sequence = pending.sequence
                    self._changed.set()
                    continue
                self._processing = pending
                self._next_score_at = now + SCORE_INTERVAL_SECONDS
                started = time.monotonic()
                try:
                    analysis = await asyncio.to_thread(_score_jpeg, pending.jpeg)
                    self._counts['scored'] += 1
                    self._last_error = None
                    if self._running:
                        self._processing = None
                        self._frames.append(_Frame(pending, analysis))
                        self._cache_bytes += len(pending.jpeg)
                        self._last_quality = analysis.quality
                        self._successful_sequence = pending.sequence
                        self._prune(time.monotonic())
                        self._enforce_budget()
                except Exception as error:
                    self._counts['score_errors'] += 1
                    if isinstance(error, ImportError):
                        self._counts['dependency_errors'] += 1
                        self._last_error = 'Pillow is required for JPEG quality scoring'
                    else:
                        self._counts['decode_errors'] += 1
                        self._last_error = f'JPEG quality scoring failed ({type(error).__name__})'
                finally:
                    self._completed_sequence = pending.sequence
                    self._last_score_ms = round((time.monotonic() - started) * 1000, 2)
                    self._processing = None
                    # Do not keep an expired/evicted JPEG alive in the coroutine
                    # local while the worker waits indefinitely for new input.
                    pending = None
                    self._changed.set()
        finally:
            self._running = False
            self._processing = None
            self._changed.set()

    def _selection(self, candidates: list[_Frame], now: float, window: float,
                   max_frames: int, warnings: list[str], blocked_reason: str | None = None) -> Selection:
        # Anchor on the newest scored appearance BEFORE comparing sharpness. A
        # sharp old room must not beat a changed present merely for being sharp.
        latest = max(candidates, key=lambda frame: frame.source.sequence) if candidates else None
        same_appearance = [frame for frame in candidates if not _appearance_changed(
            frame.analysis, latest.analysis)] if latest else []
        if len(same_appearance) < len(candidates):
            warnings.append('older_different_appearance_frames_omitted')
        usable = [frame for frame in same_appearance if frame.analysis.quality['usable']]
        if blocked_reason:
            usable = []
        usable.sort(key=lambda frame: (
            frame.analysis.quality['score']
            - 0.05 * (now - frame.source.received_monotonic) / window,
            frame.source.received_monotonic), reverse=True)
        selected = []
        duplicates = 0
        for frame in usable:
            if any(_near_duplicate(frame.analysis, other.analysis) for other in selected):
                duplicates += 1
                continue
            selected.append(frame)
            if len(selected) == max_frames:
                break
        if duplicates:
            warnings.append('near_duplicate_frames_omitted')
            self._counts['selection_duplicates_omitted'] += duplicates
        if selected and len(selected) < max_frames:
            warnings.append('fewer_distinct_usable_frames_than_requested')
        images = []
        for frame in selected:
            warnings.extend(frame.analysis.quality['warnings'])
            images.append({
                'jpeg': frame.source.jpeg, 'frame_id': frame.source.frame_id,
                'received_utc': frame.source.received_utc,
                'age_seconds': round(now - frame.source.received_monotonic, 4),
                'quality': deepcopy(frame.analysis.quality), 'sha256': frame.analysis.sha256,
            })
        if blocked_reason:
            reason = blocked_reason
        elif images:
            reason = 'selected'
        elif candidates:
            reason = 'no_usable_frames'
            warnings.extend(warning for frame in candidates
                            for warning in frame.analysis.quality['warnings'])
        elif self._counts['score_errors'] and not self._counts['scored']:
            reason = 'no_decodable_frames'
        elif self._last_received is not None:
            reason = 'no_recent_frames'
        else:
            reason = 'no_frames_received'
        if self._last_error:
            warnings.append('recent_scoring_error')
        if not self._running:
            warnings.append('quality_worker_not_running')
        self._counts['selections'] += 1
        return Selection(images, len(candidates), reason, list(dict.fromkeys(warnings)),
                         'vs-' + uuid.uuid4().hex)

    async def select(self, now: float | None = None, window_seconds: float = 3.0,
                     max_frames: int = 1, timeout_seconds: float = 2.0) -> Selection:
        """Return the best distinct recent originals, or an explicit empty result.

        If a newer input was already pending on entry, await its score before
        selecting, up to ``timeout_seconds``. This fixed target does not keep
        chasing new input from a continuous stream. An explicit ``now`` is a
        fixed monotonic reference; otherwise freshness is checked at return time.
        No camera, worker, or cloud request is started implicitly.
        """
        for name, value in [('window_seconds', window_seconds), ('timeout_seconds', timeout_seconds)]:
            if isinstance(value, bool) or not isinstance(value, (int, float)) \
                    or not math.isfinite(value) or value < 0:
                raise ValueError(f'{name} must be a finite non-negative number')
        if window_seconds == 0:
            raise ValueError('window_seconds must be positive')
        if isinstance(max_frames, bool) or not isinstance(max_frames, int) or max_frames < 1:
            raise ValueError('max_frames must be a positive integer')
        if now is not None and (isinstance(now, bool) or not isinstance(now, (int, float))
                                or not math.isfinite(now)):
            raise ValueError('now must be a finite monotonic timestamp')
        warnings = [HEURISTIC_WARNING]
        window = min(window_seconds, self._retention_seconds)
        if window_seconds > self._retention_seconds:
            warnings.append('window_limited_to_cache_retention')
        max_frames = min(max_frames, self._max_cache_frames)
        deadline = time.monotonic() + timeout_seconds
        target = max((frame.sequence for frame in (self._pending, self._processing)
                      if frame is not None), default=0)
        while True:
            reference = time.monotonic() if now is None else now
            self._prune(reference)
            candidates = [frame for frame in self._frames
                          if 0 <= reference - frame.source.received_monotonic <= window]
            usable = any(frame.analysis.quality['usable'] for frame in candidates)
            remaining = deadline - time.monotonic()
            waiting_for_latest = target > self._completed_sequence
            if waiting_for_latest and self._running and remaining > 0:
                self._changed.clear()
                try:
                    await asyncio.wait_for(self._changed.wait(), remaining)
                except asyncio.TimeoutError:
                    pass
                continue
            if waiting_for_latest:
                warnings.append('latest_frame_scoring_did_not_finish')
                return self._selection(candidates, reference, window, max_frames, warnings,
                                       'latest_frame_pending')
            if self._completed_sequence > self._successful_sequence:
                warnings.append('latest_frame_could_not_be_scored')
                return self._selection(candidates, reference, window, max_frames, warnings,
                                       'latest_frame_unavailable' if candidates else None)
            if usable or not self._running or remaining <= 0 \
                    or (candidates and self._pending is None and self._processing is None):
                return self._selection(candidates, reference, window, max_frames, warnings)
            self._changed.clear()
            try:
                await asyncio.wait_for(self._changed.wait(), remaining)
            except asyncio.TimeoutError:
                pass

    def status(self) -> dict:
        now = time.monotonic()
        self._prune(now)
        age = now - self._last_received if self._last_received is not None else None
        return {
            'running': bool(self._running and self._task and not self._task.done()),
            'cache_frames': len(self._frames), 'cache_bytes': self._cache_bytes,
            'pending': self._pending is not None,
            'pending_bytes': len(self._pending.jpeg) if self._pending else 0,
            'processing': self._processing is not None,
            'processing_bytes': len(self._processing.jpeg) if self._processing else 0,
            'held_jpeg_bytes': self._held_bytes(), 'max_bytes': self._max_bytes,
            'max_cache_frames': self._max_cache_frames,
            'retention_seconds': self._retention_seconds, 'score_fps_limit': 2.0,
            'last_received_age_seconds': round(max(0.0, age), 3) if age is not None else None,
            'last_score_ms': self._last_score_ms, 'last_quality': deepcopy(self._last_quality),
            'error': self._last_error, 'warning': HEURISTIC_WARNING, **self._counts,
        }

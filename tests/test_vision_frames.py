import asyncio
import hashlib
from io import BytesIO
import json
import threading
import time
import unittest
from unittest.mock import patch

from PIL import Image, ImageDraw, ImageFilter

from pc_backend.vision_frames import (
    HEURISTIC_WARNING, MAX_JPEG_BYTES, VisionFrames, _laplacian_variance, _score_jpeg,
)


def checker_image():
    image = Image.new('L', (256, 256), 35)
    draw = ImageDraw.Draw(image)
    for y in range(0, 256, 16):
        for x in range(0, 256, 16):
            if (x // 16 + y // 16) % 2:
                draw.rectangle((x, y, x + 15, y + 15), fill=220)
    return image


def jpeg(image, quality=95):
    with BytesIO() as target:
        image.save(target, 'JPEG', quality=quality)
        return target.getvalue()


class VisionQualityTests(unittest.TestCase):
    def test_laplacian_uses_signed_unclipped_differences(self):
        pixels = [0] * 25
        pixels[12] = 255
        # One +1020, four -255, four zero: E[L]=0, E[L^2]=144500.
        self.assertEqual(_laplacian_variance(pixels, 5, 5), 144500.0)

    def test_synthetic_blur_reduces_measured_sharpness_at_fixed_size(self):
        with checker_image() as clear, clear.filter(ImageFilter.GaussianBlur(5)) as blurred:
            sharp = _score_jpeg(jpeg(clear)).quality
            soft = _score_jpeg(jpeg(blurred)).quality
        self.assertGreater(sharp['laplacian_variance'], soft['laplacian_variance'] * 3)
        self.assertGreater(sharp['score'], soft['score'])
        self.assertEqual((sharp['analysis_width'], sharp['analysis_height']), (96, 96))
        self.assertEqual((sharp['source_width'], sharp['source_height']), (256, 256))
        self.assertTrue(sharp['heuristic'])

    def test_exposure_extremes_and_flat_texture_are_separate(self):
        with Image.new('L', (128, 128), 0) as black, \
                Image.new('L', (128, 128), 255) as white, \
                Image.new('L', (128, 128), 128) as gray:
            dark = _score_jpeg(jpeg(black)).quality
            bright = _score_jpeg(jpeg(white)).quality
            flat = _score_jpeg(jpeg(gray)).quality
        self.assertTrue(dark['very_dark'])
        self.assertTrue(bright['very_bright'])
        self.assertFalse(dark['usable'])
        self.assertFalse(bright['usable'])
        self.assertTrue(flat['usable'])
        self.assertTrue(flat['low_texture'])
        self.assertIn('low_texture_sharpness_is_inconclusive', flat['warnings'])
        self.assertNotIn('low_sharpness', flat['warnings'])

    def test_non_jpeg_input_is_not_accepted_as_a_jpeg(self):
        with Image.new('RGB', (20, 20)) as image, BytesIO() as data:
            image.save(data, 'PNG')
            with self.assertRaises(ValueError):
                _score_jpeg(data.getvalue())

    def test_excessive_source_edge_is_rejected_before_decoding(self):
        with Image.new('L', (8193, 3), 128) as image:
            with self.assertRaisesRegex(ValueError, 'dimensions'):
                _score_jpeg(jpeg(image))


class VisionFramesTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        with checker_image() as image:
            self.clear = jpeg(image)
            self.reencoded = jpeg(image, quality=85)
            with image.filter(ImageFilter.GaussianBlur(5)) as blurred:
                self.blurred = jpeg(blurred)
            with image.rotate(13) as rotated:
                self.different = jpeg(rotated)
        self.buffers = []

    def frames(self, **kwargs):
        frames = VisionFrames(**kwargs)
        self.buffers.append(frames)
        return frames

    async def asyncTearDown(self):
        for frames in self.buffers:
            await frames.stop()

    async def until(self, predicate, timeout=2):
        async with asyncio.timeout(timeout):
            while not predicate():
                await asyncio.sleep(0.01)

    async def offer_scored(self, frames, image, stamp=None):
        count = frames.status()['scored']
        frames.offer(image, stamp)
        await self.until(lambda: frames.status()['scored'] > count)

    async def test_offer_only_keeps_latest_pending_and_does_not_decode(self):
        frames = self.frames()
        with patch('pc_backend.vision_frames._score_jpeg', side_effect=AssertionError('decoded inline')):
            frames.offer(self.clear)
            frames.offer(self.blurred)
            frames.offer(self.different)
        self.assertEqual(frames.status()['scored'], 0)
        self.assertEqual(frames.status()['pending_bytes'], len(self.different))
        self.assertEqual(frames.status()['superseded_pending'], 2)
        await frames.start()
        await frames.start()
        chosen = await frames.select()
        self.assertEqual(chosen.images[0]['jpeg'], self.different)
        self.assertEqual(chosen.images[0]['sha256'], hashlib.sha256(self.different).hexdigest())
        self.assertEqual(frames.status()['scored'], 1)

    async def test_select_prefers_clear_recent_original_to_newer_blurred_frame(self):
        frames = self.frames()
        await frames.start()
        await self.offer_scored(frames, self.clear, time.monotonic() - 1)
        await self.offer_scored(frames, self.blurred)
        chosen = await frames.select()
        self.assertEqual(chosen.candidate_count, 2)
        self.assertEqual(chosen.images[0]['jpeg'], self.clear)
        self.assertGreaterEqual(chosen.images[0]['age_seconds'], 1)
        self.assertLessEqual(chosen.images[0]['age_seconds'], 3)
        self.assertTrue(chosen.images[0]['received_utc'].endswith('Z'))
        chosen.images[0]['provider_private_data_url'] = 'data:image/jpeg;base64,private-fixture'
        public = chosen.public()
        self.assertNotIn('jpeg', json.dumps(public))
        self.assertNotIn('base64', json.dumps(public))
        self.assertEqual(public['selected'][0]['frame_id'], chosen.images[0]['frame_id'])
        self.assertIn(HEURISTIC_WARNING, public['warnings'])
        public['selected'][0]['quality']['score'] = -100
        self.assertNotEqual(chosen.images[0]['quality']['score'], -100)

    async def test_new_different_appearance_beats_sharp_old_background(self):
        frames = self.frames()
        await frames.start()
        await self.offer_scored(frames, self.clear)
        with Image.new('RGB', (256, 256), (30, 140, 210)) as changed:
            draw = ImageDraw.Draw(changed)
            draw.rectangle((0, 0, 110, 256), fill=(180, 40, 20))
            with changed.filter(ImageFilter.GaussianBlur(6)) as blurred:
                current = jpeg(blurred)
        # select must first score this already-arrived pending picture, rather
        # than immediately returning the sharper but different old background.
        frames.offer(current)
        chosen = await frames.select()
        self.assertEqual(chosen.candidate_count, 2)
        self.assertEqual(chosen.images[0]['jpeg'], current)
        self.assertIn('older_different_appearance_frames_omitted', chosen.warnings)

    async def test_pending_timeout_does_not_misrepresent_old_image_as_latest_scene(self):
        frames = self.frames()
        await frames.start()
        await self.offer_scored(frames, self.clear)
        frames.offer(self.different)
        chosen = await frames.select(timeout_seconds=0)
        self.assertEqual(chosen.images, [])
        self.assertEqual(chosen.reason, 'latest_frame_pending')

    async def test_latest_decode_failure_does_not_fall_back_to_different_old_scene(self):
        frames = self.frames()
        await frames.start()
        await self.offer_scored(frames, self.clear)
        frames.offer(b'new arrival with corrupt jpeg')
        chosen = await frames.select()
        self.assertEqual(chosen.images, [])
        self.assertEqual(chosen.reason, 'latest_frame_unavailable')
        self.assertEqual(chosen.candidate_count, 1)

    async def test_window_excludes_old_clear_frame_instead_of_calling_it_current(self):
        frames = self.frames()
        await frames.start()
        stamp = time.monotonic()
        await self.offer_scored(frames, self.clear, stamp)
        chosen = await frames.select(now=stamp + 3.01, timeout_seconds=0)
        self.assertEqual(chosen.images, [])
        self.assertEqual(chosen.reason, 'no_recent_frames')
        self.assertEqual(chosen.candidate_count, 0)

    async def test_latest_frame_timestamp_cannot_be_used_before_receipt(self):
        frames = self.frames()
        await frames.start()
        stamp = time.monotonic()
        await self.offer_scored(frames, self.clear, stamp)
        self.assertEqual((await frames.select(now=stamp - 0.01, timeout_seconds=0)).images, [])

    async def test_reencoded_near_duplicates_omitted_but_distinct_scene_retained(self):
        frames = self.frames()
        await frames.start()
        for image in (self.clear, self.reencoded, self.different):
            await self.offer_scored(frames, image)
        chosen = await frames.select(max_frames=3)
        self.assertEqual(chosen.candidate_count, 3)
        self.assertEqual(len(chosen.images), 2)
        self.assertIn(self.different, [image['jpeg'] for image in chosen.images])
        self.assertIn('near_duplicate_frames_omitted', chosen.warnings)
        self.assertEqual(frames.status()['selection_duplicates_omitted'], 1)

    async def test_receiving_same_static_scene_refreshes_receipt_time(self):
        frames = self.frames()
        await frames.start()
        await self.offer_scored(frames, self.clear, time.monotonic() - 4)
        await self.offer_scored(frames, self.clear)
        chosen = await frames.select(max_frames=2)
        self.assertEqual(len(chosen.images), 1)
        self.assertLess(chosen.images[0]['age_seconds'], 1)
        self.assertEqual(chosen.images[0]['jpeg'], self.clear)

    async def test_black_and_white_images_return_explicit_empty_quality_result(self):
        frames = self.frames()
        await frames.start()
        with Image.new('L', (128, 128), 0) as black, Image.new('L', (128, 128), 255) as white:
            await self.offer_scored(frames, jpeg(black))
            await self.offer_scored(frames, jpeg(white))
        chosen = await frames.select()
        self.assertEqual(chosen.images, [])
        self.assertEqual(chosen.reason, 'no_usable_frames')
        self.assertEqual(chosen.candidate_count, 2)
        self.assertIn('underexposed', chosen.warnings)
        self.assertIn('overexposed', chosen.warnings)

    async def test_count_bound_and_expiry_release_old_jpegs(self):
        frames = self.frames(max_cache_frames=2)
        await frames.start()
        for image in (self.clear, self.blurred, self.different):
            await self.offer_scored(frames, image)
        self.assertEqual(frames.status()['cache_frames'], 2)
        self.assertEqual(frames.status()['evicted_count'], 1)
        chosen = await frames.select(now=time.monotonic() + 9, timeout_seconds=0)
        self.assertFalse(chosen.images)
        self.assertEqual(frames.status()['cache_bytes'], 0)
        self.assertEqual(frames.status()['evicted_age'], 2)

    async def test_jpeg_budget_includes_pending_and_processing_frames(self):
        sizes = list(map(len, (self.clear, self.blurred, self.different)))
        frames = self.frames(max_bytes=max(sizes) + min(sizes) - 1)
        await frames.start()
        for image in (self.clear, self.blurred, self.different):
            await self.offer_scored(frames, image)
            self.assertLessEqual(frames.status()['held_jpeg_bytes'], frames.status()['max_bytes'])
        frames.offer(self.clear)
        self.assertLessEqual(frames.status()['held_jpeg_bytes'], frames.status()['max_bytes'])
        self.assertGreater(frames.status()['evicted_bytes'], 0)

    async def test_bad_input_and_decode_error_do_not_kill_worker(self):
        frames = self.frames()
        await frames.start()
        frames.offer(b'x' * (MAX_JPEG_BYTES + 1))
        frames.offer(b'')
        frames.offer(self.clear, float('nan'))
        frames.offer(self.clear, time.monotonic() + 10)
        frames.offer(b'not a jpeg')
        await self.until(lambda: frames.status()['decode_errors'] == 1)
        await self.offer_scored(frames, self.clear)
        chosen = await frames.select()
        status = frames.status()
        self.assertTrue(chosen.images)
        self.assertTrue(status['running'])
        self.assertEqual(status['rejected_oversize'], 1)
        self.assertEqual(status['rejected_input'], 1)
        self.assertEqual(status['rejected_timestamp'], 2)
        self.assertEqual(status['score_errors'], 1)
        self.assertIsNone(status['error'])

    async def test_out_of_order_and_already_expired_input_are_counted(self):
        frames = self.frames()
        frames.offer(self.clear, time.monotonic() - 9)
        frames.offer(self.clear)
        frames.offer(self.blurred, time.monotonic() - 1)
        self.assertEqual(frames.status()['dropped_expired'], 1)
        self.assertEqual(frames.status()['rejected_out_of_order'], 1)
        self.assertEqual(frames.status()['pending_bytes'], len(self.clear))

    async def test_worker_never_scores_burst_at_more_than_two_fps(self):
        frames = self.frames()
        starts = []

        def score(data):
            starts.append(time.monotonic())
            return _score_jpeg(data)

        with patch('pc_backend.vision_frames._score_jpeg', side_effect=score):
            await frames.start()
            await self.offer_scored(frames, self.clear)
            frames.offer(self.blurred)
            frames.offer(self.different)
            await self.until(lambda: frames.status()['scored'] == 2)
        self.assertGreaterEqual(starts[1] - starts[0], 0.49)
        self.assertEqual(frames.status()['superseded_pending'], 1)
        self.assertIn(self.different, [image['jpeg'] for image in
                                      (await frames.select(max_frames=2)).images])

    async def test_stop_waits_for_decoder_and_prevents_late_cache_repopulation(self):
        frames = self.frames()
        entered, release = threading.Event(), threading.Event()

        def blocked_score(data):
            entered.set()
            if not release.wait(2):
                raise TimeoutError('test did not release the worker')
            return _score_jpeg(data)

        with patch('pc_backend.vision_frames._score_jpeg', side_effect=blocked_score):
            await frames.start()
            frames.offer(self.clear)
            await self.until(entered.is_set)
            stop = asyncio.create_task(frames.stop())
            try:
                await asyncio.sleep(0.02)
                self.assertFalse(stop.done())
                frames.offer(self.blurred)
            finally:
                release.set()
                await stop
        self.assertEqual(frames.status()['held_jpeg_bytes'], 0)
        self.assertEqual(frames.status()['cache_frames'], 0)
        self.assertFalse(frames.status()['running'])
        self.assertEqual(frames.status()['dropped_stopped'], 1)
        await frames.start()
        self.assertFalse((await frames.select(timeout_seconds=0)).images)

    async def test_empty_selection_waits_only_while_worker_is_running(self):
        frames = self.frames()
        before = time.monotonic()
        chosen = await frames.select()
        self.assertLess(time.monotonic() - before, 0.1)
        self.assertEqual(chosen.reason, 'no_frames_received')
        self.assertIn('quality_worker_not_running', chosen.warnings)
        await frames.start()
        before = time.monotonic()
        chosen = await frames.select(timeout_seconds=0.05)
        self.assertGreaterEqual(time.monotonic() - before, 0.04)
        self.assertLess(time.monotonic() - before, 0.5)
        self.assertEqual(chosen.images, [])
        self.assertEqual(chosen.reason, 'no_frames_received')

    async def test_select_rejects_nonfinite_and_invalid_arguments(self):
        frames = self.frames()
        for kwargs in ({'window_seconds': 0}, {'window_seconds': float('inf')},
                       {'timeout_seconds': -1}, {'max_frames': False}, {'now': float('nan')}):
            with self.assertRaises(ValueError):
                await frames.select(**kwargs)


if __name__ == '__main__':
    unittest.main()

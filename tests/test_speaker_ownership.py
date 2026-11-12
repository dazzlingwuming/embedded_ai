"""No network: a failed speaker handshake must not mute another owner."""
import asyncio
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock

from pc_backend.speaker_client import BoardSpeaker
from pc_backend.voice_protocol import VoiceError


class FakeSocket:
    def __init__(self, hello=None, receive_error=None):
        self.closed = False
        self.hello, self.receive_error = hello, receive_error
        self.send_json = AsyncMock()
        self.close = AsyncMock(side_effect=self._close)
        self.hold = asyncio.Event()

    async def receive_json(self, timeout):
        if self.receive_error:
            raise self.receive_error
        return self.hello

    async def _close(self):
        self.closed = True
        self.hold.set()

    def __aiter__(self):
        return self

    async def __anext__(self):
        await self.hold.wait()
        raise StopAsyncIteration


class Response:
    status = 200

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    async def read(self):
        return b'{}'


def client(socket):
    return SimpleNamespace(ws_connect=AsyncMock(return_value=socket),
                           post=Mock(side_effect=lambda *args, **kwargs: Response()))


class SpeakerOwnershipTests(unittest.IsolatedAsyncioTestCase):
    async def test_busy_handshake_close_only_closes_its_socket(self):
        socket = FakeSocket({'type': 'speaker_busy', 'sample_rate': 48000})
        http = client(socket)
        speaker = BoardSpeaker(http, 'http://192.168.2.38')
        with self.assertRaises(VoiceError):
            await speaker.connect()
        self.assertFalse(speaker.acquired)
        await speaker.close()
        http.post.assert_not_called()
        socket.send_json.assert_not_awaited()
        socket.close.assert_awaited_once()

    async def test_failed_handshake_cannot_send_global_http_cancel(self):
        socket = FakeSocket(receive_error=TimeoutError())
        http = client(socket)
        speaker = BoardSpeaker(http, 'http://192.168.2.38')
        with self.assertRaises(TimeoutError):
            await speaker.connect()
        await speaker.close()
        http.post.assert_not_called()
        socket.send_json.assert_not_awaited()
        self.assertTrue(socket.closed)

    async def test_owned_close_cancels_playback_and_releases_its_connection(self):
        socket = FakeSocket({'type': 'hello', 'sample_rate': 48000})
        http = client(socket)
        speaker = BoardSpeaker(http, 'http://192.168.2.38')
        await speaker.connect()
        self.assertTrue(speaker.acquired)
        await speaker.close()
        socket.send_json.assert_awaited_once_with({'type': 'cancel'})
        self.assertEqual(http.post.call_args.args[0], 'http://192.168.2.38/api/speaker/cancel')
        self.assertFalse(speaker.acquired)
        self.assertTrue(socket.closed)
        self.assertTrue(speaker.reader.done())

    async def test_hello_with_wrong_rate_still_requires_owned_cleanup(self):
        socket = FakeSocket({'type': 'hello', 'sample_rate': 16000})
        http = client(socket)
        speaker = BoardSpeaker(http, 'http://192.168.2.38')
        with self.assertRaises(VoiceError):
            await speaker.connect()
        self.assertTrue(speaker.acquired)
        await speaker.close()
        socket.send_json.assert_awaited_once_with({'type': 'cancel'})
        http.post.assert_called_once()
        self.assertFalse(speaker.acquired)

    async def test_closed_owned_socket_does_not_cancel_a_later_owner(self):
        socket = FakeSocket({'type': 'hello', 'sample_rate': 48000})
        http = client(socket)
        speaker = BoardSpeaker(http, 'http://192.168.2.38')
        await speaker.connect()
        await socket.close()
        await speaker.close()
        http.post.assert_not_called()
        socket.send_json.assert_not_awaited()

    async def test_cancel_before_connect_has_no_board_side_effect(self):
        http = client(FakeSocket())
        speaker = BoardSpeaker(http, 'http://192.168.2.38')
        await speaker.cancel()
        await speaker.close()
        http.ws_connect.assert_not_awaited()
        http.post.assert_not_called()


if __name__ == '__main__':
    unittest.main()

"""ChatFXVideoService against a stand-in ChatFX API, a stand-in Daily API and a stand-in Daily call client (no accounts,
no GPU). The stand-in avatar joins when the API is asked for a session, sends its face 25 times a second and its voice
every 20 ms, and says "played" when a reply's done.

    uv run pytest -q
"""

import asyncio
import sys
import base64
import io
import json
import threading
import time
from types import SimpleNamespace

import pytest
from aiohttp import web
from PIL import Image
from pipecat.frames.frames import (
    InterruptionFrame,
    OutputImageRawFrame,
    OutputTransportReadyFrame,
    SpeechOutputAudioRawFrame,
    TTSAudioRawFrame,
    TTSStartedFrame,
    TTSStoppedFrame,
)
from pipecat.tests.utils import SleepFrame, run_test

import pipecat_chatfx.video as video
from pipecat_chatfx import ChatFXException, ChatFXVideoService

RATE = 24000


def tts_sound(seconds: float) -> TTSAudioRawFrame:
    return TTSAudioRawFrame(audio=b"\x10\x00" * int(RATE * seconds), sample_rate=RATE, num_channels=1)


class StandInAvatar:
    """The avatar's side of the room: joins, sends its face and voice, answers "played"."""

    def __init__(self, client: "StandInCallClient") -> None:
        self.client = client
        self.running = True
        self.heard: list[dict] = []

    def arrive(self) -> None:
        self.client.events.on_participant_joined({"id": "avatar-1", "info": {"userName": "ChatFX", "isLocal": False}})
        threading.Thread(target=self._send, daemon=True).start()

    def _send(self) -> None:
        frames = 0
        while self.running:
            renderer = self.client.video.get("avatar-1")
            if renderer and frames % 2 == 0:
                renderer("avatar-1", SimpleNamespace(buffer=b"\x80" * (8 * 6 * 3), width=8, height=6, color_format="RGB"), "camera")
            sound = self.client.audio.get("avatar-1")
            if sound:
                sound("avatar-1", SimpleNamespace(audio_frames=b"\x00\x00" * (RATE // 50), sample_rate=RATE, num_channels=1), "microphone")
            frames += 1
            time.sleep(0.02)

    def leave(self) -> None:
        self.running = False
        self.client.events.on_participant_left({"id": "avatar-1"}, "leftCall")

    def message(self, message: dict) -> None:
        self.heard.append(message)
        if message.get("chatfx") == "voice" and message.get("done"):
            threading.Timer(0.05, self.client.events.on_app_message, ({"chatfx": "played", "seconds": 1.0, "interrupted": False}, "avatar-1")).start()


class StandInCallClient:
    made: list["StandInCallClient"] = []

    def __init__(self, event_handler) -> None:
        self.events = event_handler
        self.video: dict = {}
        self.audio: dict = {}
        self.joined_with: tuple | None = None
        self.left = False
        self.released = False
        self.avatar = StandInAvatar(self)
        StandInCallClient.made.append(self)

    def set_user_name(self, name):
        self.name = name

    def update_subscriptions(self, participant_settings=None, profile_settings=None, completion=None):
        self.subscribed = participant_settings

    def join(self, url, meeting_token=None, client_settings=None, completion=None):
        self.joined_with = (url, meeting_token, client_settings)
        threading.Timer(0.01, completion, ({"participants": {"local": {"id": "bot-1"}}}, None)).start()

    def participants(self):
        return {"local": {"id": "bot-1", "info": {"isLocal": True}}}

    def set_video_renderer(self, participant_id, callback, video_source="camera", color_format="RGB"):
        self.video[participant_id] = callback

    def set_audio_renderer(self, participant_id, callback, audio_source="microphone", sample_rate=16000, callback_interval_ms=20):
        self.audio[participant_id] = callback

    def send_app_message(self, message, participant_id=None):
        assert participant_id == "avatar-1"
        self.avatar.message(message)

    def leave(self, completion=None):
        self.left = True
        self.avatar.running = False
        threading.Timer(0.01, completion, (None,)).start()

    def release(self):
        self.released = True


# The real one, before the fixture below swaps it for a stand-in.
REAL_DAILY_LIBRARY = video.daily_library


@pytest.fixture(autouse=True)
def stand_in_daily(monkeypatch):
    StandInCallClient.made = []
    library = SimpleNamespace(EventHandler=object, CallClient=StandInCallClient, Daily=SimpleNamespace(init=lambda: None))
    monkeypatch.setattr(video, "daily_library", lambda: library)
    monkeypatch.setattr(video, "FACE_SECONDS", 2.0)
    yield


def picture_png() -> bytes:
    out = io.BytesIO()
    Image.new("RGB", (101, 61), (200, 120, 90)).save(out, "PNG")
    return out.getvalue()


@pytest.fixture
async def api():
    """The stand-in ChatFX API (at /) and Daily API (at /daily)."""
    state = SimpleNamespace(sessions=[], ended=[], rooms_made=[], rooms_deleted=[], tokens=[], start_status=201, start_body={"id": "ses_1"}, avatar_comes=True)

    async def rooms(request):
        state.rooms_made.append(await request.json())
        return web.json_response({"name": "room1", "url": "https://chatfx.daily.co/room1"})

    async def tokens(request):
        properties = (await request.json())["properties"]
        state.tokens.append(properties)
        return web.json_response({"token": f"tok-{properties['user_name']}"})

    async def delete_room(request):
        state.rooms_deleted.append(request.match_info["name"])
        return web.json_response({"deleted": True})

    async def sessions(request):
        state.sessions.append(await request.json())
        if state.start_status == 201 and state.avatar_comes:
            asyncio.get_running_loop().call_later(0.05, StandInCallClient.made[-1].avatar.arrive)
        return web.json_response(state.start_body, status=state.start_status)

    async def keepalive(request):
        return web.json_response({"ok": True})

    async def end(request):
        state.ended.append(request.match_info["id"])
        return web.json_response({"ended": True})

    async def picture(request):
        return web.Response(body=picture_png(), content_type="image/png")

    app = web.Application()
    app.router.add_post("/daily/rooms", rooms)
    app.router.add_post("/daily/meeting-tokens", tokens)
    app.router.add_delete("/daily/rooms/{name}", delete_room)
    app.router.add_post("/v1/sessions", sessions)
    app.router.add_post("/v1/sessions/{id}/keepalive", keepalive)
    app.router.add_post("/v1/sessions/{id}/end", end)
    app.router.add_get("/v1/avatars/{id}/picture", picture)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    state.url = f"http://127.0.0.1:{port}"
    yield state
    await runner.cleanup()


def service(api, **options) -> ChatFXVideoService:
    return ChatFXVideoService(avatar_id="chatfx-lois", api_key="cfx_test", daily_api_key="daily_test", api_url=api.url, daily_api_url=api.url + "/daily", **options)


def kinds(frames):
    return [type(frame) for frame in frames]


def voice_pieces(client):
    return [base64.b64decode(m["audio"]) for m in client.avatar.heard if m.get("chatfx") == "voice" and m.get("audio")]


async def test_a_reply_goes_to_the_face_and_the_face_and_voice_come_back(api):
    avatar = service(api)
    down, _ = await run_test(
        avatar,
        frames_to_send=[OutputTransportReadyFrame(), SleepFrame(sleep=0.6), TTSStartedFrame(), tts_sound(0.25), TTSStoppedFrame(), SleepFrame(sleep=0.4)],
    )
    client = StandInCallClient.made[-1]
    # The room: private, two seats, in the customer's Daily account; a token each, neither an owner's.
    assert api.rooms_made[0]["privacy"] == "private" and api.rooms_made[0]["properties"]["max_participants"] == 2
    assert sorted(t["user_name"] for t in api.tokens) == ["ChatFX", "chatfx-pipecat-bot"] and not any(t["is_owner"] for t in api.tokens)
    assert client.joined_with[:2] == ("https://chatfx.daily.co/room1", "tok-chatfx-pipecat-bot")
    assert client.joined_with[2]["publishing"] == {"camera": {"isPublishing": False}, "microphone": {"isPublishing": False}}
    # Every try in one call carries the same request_key, so asking again never starts a second face.
    assert len(api.sessions) == 1 and len(api.sessions[0].pop("request_key")) == 32
    assert api.sessions == [{"avatar_id": "chatfx-lois", "connect": "daily", "daily": {"room_url": "https://chatfx.daily.co/room1", "token": "tok-ChatFX"}}]
    # The bot's voice went to the avatar in 100 ms pieces, then the reply's end; none of it reached the visitor directly.
    assert [len(p) for p in voice_pieces(client)] == [4800, 4800, 2400]
    assert {"chatfx": "voice", "reply": client.avatar.heard[0]["reply"], "done": True} in client.avatar.heard
    assert TTSAudioRawFrame not in kinds(down) and TTSStartedFrame not in kinds(down) and TTSStoppedFrame not in kinds(down)
    # The face and the avatar's voice came back as the bot's video and audio.
    assert any(isinstance(f, OutputImageRawFrame) and f.size == (8, 6) for f in down)
    assert SpeechOutputAudioRawFrame in kinds(down)
    # The end: the avatar is told, the session ended (billing stops), the bot left, the room is gone.
    assert client.avatar.heard[-1] == {"chatfx": "end"}
    assert api.ended == ["ses_1"] and client.left and client.released and api.rooms_deleted == ["room1"]


async def test_until_the_face_is_in_the_bot_speaks_with_its_own_voice_over_the_avatars_picture(api):
    api.avatar_comes = False
    avatar = service(api, face_first_seconds=0)
    down, _ = await run_test(
        avatar,
        frames_to_send=[OutputTransportReadyFrame(), SleepFrame(sleep=0.5), TTSStartedFrame(), tts_sound(0.2), TTSStoppedFrame(), SleepFrame(sleep=0.1)],
    )
    assert kinds(down).count(TTSAudioRawFrame) == 1 and TTSStartedFrame in kinds(down) and TTSStoppedFrame in kinds(down)
    # The avatar's picture (101 x 61, made even) stands in for the face.
    assert any(isinstance(f, OutputImageRawFrame) and f.size == (100, 60) and f.format == "RGB" for f in down)
    assert voice_pieces(StandInCallClient.made[-1]) == []
    # The session that never sent a face is ended.
    assert api.ended == ["ses_1"]


async def test_the_first_words_wait_for_the_face(api):
    avatar = service(api, face_first_seconds=5)
    down, _ = await run_test(
        avatar,
        frames_to_send=[OutputTransportReadyFrame(), TTSStartedFrame(), tts_sound(0.1), TTSStoppedFrame(), SleepFrame(sleep=0.4)],
    )
    assert TTSAudioRawFrame not in kinds(down)
    assert [len(p) for p in voice_pieces(StandInCallClient.made[-1])] == [4800]


async def test_talking_over_the_avatar_stops_it_at_once(api):
    avatar = service(api)
    down, _ = await run_test(
        avatar,
        frames_to_send=[
            OutputTransportReadyFrame(),
            SleepFrame(sleep=0.6),
            TTSStartedFrame(),
            tts_sound(0.3),
            SleepFrame(sleep=0.05),
            InterruptionFrame(),
            SleepFrame(sleep=0.2),
        ],
    )
    client = StandInCallClient.made[-1]
    assert {"chatfx": "interrupt"} in client.avatar.heard
    # Nothing from the avatar's voice still on its way is played after the interruption, for a moment.
    after = kinds(down)[kinds(down).index(InterruptionFrame) + 1 :]
    assert SpeechOutputAudioRawFrame not in after


async def test_a_refusal_leaves_the_bot_speaking_with_its_own_voice(api):
    api.start_status = 402
    api.start_body = {"error": {"code": "no_minutes", "message": "No minutes left."}}
    avatar = service(api)
    down, _ = await run_test(
        avatar,
        frames_to_send=[OutputTransportReadyFrame(), SleepFrame(sleep=0.4), TTSStartedFrame(), tts_sound(0.1), TTSStoppedFrame()],
    )
    assert avatar.failure is not None and avatar.failure.code == "no_minutes" and avatar.failure.status == 402
    assert TTSAudioRawFrame in kinds(down)
    # The room was removed straight away.
    assert api.rooms_deleted == ["room1"] and StandInCallClient.made[-1].left


async def test_when_the_avatar_leaves_the_next_reply_is_the_bots_own_voice(api):
    avatar = service(api)
    asyncio.get_running_loop().call_later(0.7, lambda: StandInCallClient.made[-1].avatar.leave())
    down, _ = await run_test(
        avatar,
        frames_to_send=[OutputTransportReadyFrame(), SleepFrame(sleep=0.9), TTSStartedFrame(), tts_sound(0.1), TTSStoppedFrame()],
    )
    assert TTSAudioRawFrame in kinds(down) and TTSStoppedFrame in kinds(down)
    assert voice_pieces(StandInCallClient.made[-1]) == []


async def test_keys_are_required():
    with pytest.raises(ChatFXException) as missing:
        ChatFXVideoService(avatar_id="chatfx-lois", api_key="", daily_api_key="daily_test")
    assert missing.value.code == "missing_api_key"
    with pytest.raises(ChatFXException) as missing:
        ChatFXVideoService(avatar_id="chatfx-lois", api_key="cfx_test", daily_api_key="")
    assert missing.value.code == "missing_daily_key"


def test_a_voice_piece_fits_in_a_daily_message():
    piece = b"\x00" * int(48000 * video.PIECE_SECONDS) * 2
    message = {"chatfx": "voice", "reply": "0123456789ab", "audio": base64.b64encode(piece).decode(), "rate": 48000}
    # Daily takes app messages well past this; Pipecat's Tavus service sends the same size.
    assert len(json.dumps(message)) < 13_000


def test_daily_starts_once_however_many_calls_and_whoever_starts_it(monkeypatch):
    # A second Daily.init() stops the whole program (daily-python 0.38.0): a bot's second call used to do that.
    from pipecat.transports.daily.transport import DailyTransportClient

    starts = []
    monkeypatch.setitem(sys.modules, "daily", SimpleNamespace(Daily=SimpleNamespace(init=lambda: starts.append(1))))
    monkeypatch.setattr(video, "_daily_started", False)
    monkeypatch.setattr(DailyTransportClient, "_daily_initialized", False)
    for _ in range(3):
        REAL_DAILY_LIBRARY()
    assert starts == [1]
    # DailyTransport, made after us, won't start it again either.
    assert DailyTransportClient._daily_initialized is True

    # And when DailyTransport started it first, we don't.
    starts.clear()
    monkeypatch.setattr(video, "_daily_started", False)
    REAL_DAILY_LIBRARY()
    assert starts == []

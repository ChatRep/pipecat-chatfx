"""ChatFX Avatar for Pipecat: a real-time talking face for your Pipecat bot.

Put ChatFXVideoService right after your TTS service. Your bot's voice goes to the avatar, and the avatar's face and voice,
drawn together so the lips match, come back into your pipeline as your bot's video and audio. Your transport stays as it
is (Daily, SmallWebRTC, anything Pipecat supports), and so does your page: it shows the bot's video as before.

How it connects (the way Pipecat's own Tavus service does): the avatar and your bot meet in a private Daily room in your
Daily account, made for the call with your Daily API key and removed when it ends. POST https://api.chatfx.com/v1/sessions
starts the avatar there with a meeting token for it alone. Your bot sends it each reply's sound as Daily app messages and
takes its face and voice back. A check-in every 20 seconds keeps the session going, and the end of your pipeline ends it,
so billing stops.

The call never depends on the face: until the avatar is in (a few seconds), or if it can't come or leaves, your bot speaks
with its own voice as if the avatar weren't there.
"""

from __future__ import annotations

import asyncio
import base64
import io
import os
import time
import uuid
from typing import Any

import aiohttp
from loguru import logger
from pipecat.frames.frames import (
    BotStartedSpeakingFrame,
    CancelFrame,
    EndFrame,
    Frame,
    InterruptionFrame,
    OutputImageRawFrame,
    OutputTransportReadyFrame,
    SpeechOutputAudioRawFrame,
    StartFrame,
    TTSAudioRawFrame,
    TTSStartedFrame,
    TTSStoppedFrame,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessorSetup
from pipecat.services.ai_service import AIService
from pipecat.services.settings import ServiceSettings

DEFAULT_API_URL = "https://api.chatfx.com"
DAILY_API_URL = "https://api.daily.co/v1"
BOT_NAME = "chatfx-pipecat-bot"
AVATAR_NAME = "ChatFX"
KEEPALIVE_SECONDS = 20
REQUEST_SECONDS = 15
# How long one face request may take before it's asked again (Nov 19, 2026: a call's answer was lost on the way back and
# the call waited 60 seconds for its face). Every try in one call carries the same request_key, so asking again joins the
# start already under way (a GPU waking can take up to 45 seconds) or gets the face it started, never a second face. Past
# that the API says when to try again (a 503 with Retry-After).
ATTEMPT_SECONDS = 10.0
RETRY_SECONDS = 3.0
# After a try that got no answer: ask again almost at once.
LOST_RETRY_SECONDS = 1.0
# After a start, the avatar's face arrives within seconds; if it doesn't, that try is ended (so it isn't billed) and
# another is made.
FACE_SECONDS = 30.0
# The room and its tokens last this long (the API takes tokens that expire within 6 hours).
ROOM_SECONDS = 5 * 3600
# A reply's sound goes to the avatar in pieces this long, as Pipecat's Tavus service sends a replica its voice.
PIECE_SECONDS = 0.1
# At the end of a call, the avatar's last words get this long to finish before it hangs up.
LAST_WORDS_SECONDS = 15.0
# When the visitor talks over the avatar, what was already on its way from it is dropped for this long, so it stops at once.
CUT_SECONDS = 0.5
# The avatar's voice starts at full quality instead of ramping up from Daily's usual 1 Mbps.
RECEIVE_BITRATE = 2_500_000
# Until the live face is in, your bot's video is the avatar's picture, sent this many times a second.
STILL_FPS = 5
STILL_LONGEST_SIDE = 912


class ChatFXException(Exception):
    """A ChatFX refusal (wrong key, no minutes left, no such avatar) or a missing setting, with the API's code."""

    def __init__(self, message: str, *, code: str = "error", status: int = 0) -> None:
        super().__init__(message)
        self.code = code
        self.status = status


def still_frame(picture: bytes, text: str | None) -> OutputImageRawFrame:
    """The avatar's picture as a video frame, with the words in a soft band near the bottom."""
    from PIL import Image, ImageDraw, ImageFont

    image = Image.open(io.BytesIO(picture)).convert("RGBA")
    image.thumbnail((STILL_LONGEST_SIDE, STILL_LONGEST_SIDE))
    # Video encoders need even sizes.
    image = image.crop((0, 0, image.width - image.width % 2, image.height - image.height % 2))
    if text:
        size = max(14, round(min(image.width, image.height) * 0.05))
        font = ImageFont.load_default(size=size)
        band = Image.new("RGBA", image.size, (0, 0, 0, 0))
        draw = ImageDraw.Draw(band)
        left, top, right, bottom = draw.textbbox((0, 0), text, font=font)
        x, y = (image.width - (right - left)) // 2, image.height - size * 3
        pad = size // 2
        draw.rounded_rectangle((x - pad * 2, y - pad + top, x + right - left + pad * 2, y + bottom + pad), radius=size, fill=(12, 10, 40, 170))
        draw.text((x - left, y), text, font=font, fill=(255, 255, 255, 255))
        image = Image.alpha_composite(image, band)
    image = image.convert("RGB")
    return OutputImageRawFrame(image=image.tobytes(), size=image.size, format="RGB")


def daily_library():
    """Daily's Python library (pipecat-ai[daily]), started once for the program if your transport hasn't already."""
    import daily

    try:
        daily.Daily.init()
    except Exception:
        # Already started (DailyTransport does it too).
        pass
    return daily


class DailyRooms:
    """A private Daily room in your Daily account for one call, with a meeting token for your bot and one for the avatar."""

    def __init__(self, api_key: str, http: aiohttp.ClientSession, api_url: str = DAILY_API_URL) -> None:
        self._key = api_key
        self._http = http
        self._url = api_url.rstrip("/")

    async def _request(self, method: str, path: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
        async with self._http.request(
            method,
            self._url + path,
            json=body,
            headers={"Authorization": f"Bearer {self._key}"},
            timeout=aiohttp.ClientTimeout(total=REQUEST_SECONDS),
        ) as response:
            try:
                data = await response.json(content_type=None)
            except Exception:
                data = {}
            data = data if isinstance(data, dict) else {}
            if response.status >= 400:
                reason = data.get("info") or data.get("error") or response.status
                raise ChatFXException(f"Daily refused {method} {path}: {reason}", code="daily_refused", status=response.status)
            return data

    async def create(self) -> tuple[str, str, str, str]:
        """(the room's address, its name, your bot's token, the avatar's token)."""
        expires = int(time.time()) + ROOM_SECONDS
        room = await self._request(
            "POST",
            "/rooms",
            {"privacy": "private", "properties": {"exp": expires, "eject_at_room_exp": True, "max_participants": 2, "enable_chat": False}},
        )
        name, url = room["name"], room["url"]

        async def token(user_name: str) -> str:
            made = await self._request("POST", "/meeting-tokens", {"properties": {"room_name": name, "user_name": user_name, "exp": expires, "is_owner": False}})
            return made["token"]

        bot, avatar = await asyncio.gather(token(BOT_NAME), token(AVATAR_NAME))
        return url, name, bot, avatar

    async def delete(self, name: str) -> None:
        try:
            await self._request("DELETE", f"/rooms/{name}")
        except Exception:
            logger.warning(f"ChatFX: couldn't remove the Daily room {name}; it expires by itself")


class BotSeat:
    """Your bot's place in the avatar's room (daily-python): it publishes nothing, sends app messages, and takes the
    avatar's camera and microphone. Daily's callbacks arrive on its own threads and are handed to the pipeline's loop."""

    def __init__(self, loop: asyncio.AbstractEventLoop, out_rate: int, on_video, on_audio, on_message, on_avatar) -> None:
        self._loop = loop
        self._out_rate = out_rate
        self._on_video = on_video
        self._on_audio = on_audio
        self._on_message = on_message
        self._on_avatar = on_avatar
        self.avatar_id: str | None = None
        self.local_id: str | None = None
        self._client = None

    async def join(self, url: str, token: str) -> None:
        daily = daily_library()
        seat = self

        class Events(daily.EventHandler):
            def on_participant_joined(self, participant):
                seat._loop.call_soon_threadsafe(seat._joined, participant)

            def on_participant_left(self, participant, reason):
                seat._loop.call_soon_threadsafe(seat._left, participant)

            def on_app_message(self, message, sender):
                seat._loop.call_soon_threadsafe(seat._on_message, message, sender)

        self._events = Events()
        self._client = daily.CallClient(event_handler=self._events)
        self._client.set_user_name(BOT_NAME)
        joined = self._loop.create_future()

        def done(data, error):
            def settle():
                if joined.done():
                    return
                if error:
                    joined.set_exception(ChatFXException(f"Couldn't join the avatar's Daily room: {error}", code="daily_refused"))
                else:
                    joined.set_result(data)

            self._loop.call_soon_threadsafe(settle)

        self._client.join(
            url,
            meeting_token=token,
            client_settings={
                "inputs": {"camera": {"isEnabled": False}, "microphone": {"isEnabled": False}},
                "publishing": {"camera": {"isPublishing": False}, "microphone": {"isPublishing": False}},
                "connection": {"initialReceiveBitrate": RECEIVE_BITRATE},
            },
            completion=done,
        )
        data = await asyncio.wait_for(joined, REQUEST_SECONDS)
        self.local_id = ((data or {}).get("participants") or {}).get("local", {}).get("id")
        for pid, participant in (self._client.participants() or {}).items():
            if pid != "local":
                self._joined(participant)

    def _joined(self, participant: dict[str, Any]) -> None:
        # The room is private and has two seats: anyone else in it is the avatar.
        pid = participant.get("id")
        if self.avatar_id or not pid or pid == self.local_id or participant.get("info", {}).get("isLocal"):
            return
        self.avatar_id = pid
        loop = self._loop

        def video(participant_id, frame, source):
            loop.call_soon_threadsafe(self._on_video, frame)

        def audio(participant_id, data, source):
            loop.call_soon_threadsafe(self._on_audio, data)

        self._client.update_subscriptions(participant_settings={pid: {"media": {"camera": "subscribed", "microphone": "subscribed"}}})
        self._client.set_video_renderer(pid, video, video_source="camera", color_format="RGB")
        self._client.set_audio_renderer(pid, audio, audio_source="microphone", sample_rate=self._out_rate, callback_interval_ms=20)
        self._on_avatar(True)

    def _left(self, participant: dict[str, Any]) -> None:
        if participant.get("id") == self.avatar_id:
            self.avatar_id = None
            self._on_avatar(False)

    def send(self, message: dict[str, Any]) -> None:
        if self._client and self.avatar_id:
            try:
                self._client.send_app_message(message, self.avatar_id)
            except Exception:
                logger.debug("ChatFX: a message to the avatar didn't go", exc_info=True)

    async def leave(self) -> None:
        if not self._client:
            return
        left = self._loop.create_future()
        self._client.leave(completion=lambda *_: self._loop.call_soon_threadsafe(lambda: left.done() or left.set_result(True)))
        try:
            await asyncio.wait_for(left, 10)
        except asyncio.TimeoutError:
            pass
        self._client.release()
        self._client = None
        self.avatar_id = None


class ChatFXVideoService(AIService):
    """Your bot's face. Put it right after your TTS service:

        avatar = ChatFXVideoService(avatar_id="chatfx-lois")  # CHATFX_AVATAR_API_KEY and DAILY_API_KEY from the environment
        pipeline = Pipeline([transport.input(), stt, user_aggregator, llm, tts, avatar, transport.output(), assistant_aggregator])

    and turn on your transport's video out at the avatar's size (912 x 512 landscape, 512 x 512 square, 384 x 512 portrait).
    """

    def __init__(
        self,
        *,
        avatar_id: str,
        api_key: str | None = None,
        daily_api_key: str | None = None,
        api_url: str | None = None,
        daily_api_url: str = DAILY_API_URL,
        session: aiohttp.ClientSession | None = None,
        face_first_seconds: float = 10.0,
        start_timeout: float = 120.0,
        waiting_picture: bool = True,
        waiting_text: str | None = "Video starting...",
        **kwargs: Any,
    ) -> None:
        super().__init__(settings=ServiceSettings(model=None), **kwargs)
        self._avatar_id = avatar_id
        self._api_key = api_key or os.environ.get("CHATFX_AVATAR_API_KEY") or ""
        self._daily_key = daily_api_key or os.environ.get("DAILY_API_KEY") or ""
        if not self._api_key:
            raise ChatFXException("Set CHATFX_AVATAR_API_KEY, or pass api_key. Create a key on the API keys page.", code="missing_api_key")
        if not self._daily_key:
            raise ChatFXException("Set DAILY_API_KEY, or pass daily_api_key: the avatar meets your bot in a private room in your Daily account.", code="missing_daily_key")
        self._api_url = (api_url or os.environ.get("CHATFX_AVATAR_API_URL") or DEFAULT_API_URL).rstrip("/")
        self._daily_api_url = daily_api_url
        self._session = session
        self._own_session: aiohttp.ClientSession | None = None
        self._face_first_seconds = face_first_seconds
        self._start_timeout = start_timeout
        self._waiting_picture = waiting_picture
        self._waiting_text = waiting_text
        self._started_at = 0.0
        self._stills: tuple[OutputImageRawFrame, OutputImageRawFrame] | None = None
        self._showing: asyncio.Task | None = None
        self._out_rate = 24000
        self._seat: BotSeat | None = None
        self._rooms: DailyRooms | None = None
        self._room_name: str | None = None
        self._session_id: str | None = None
        self._connecting: asyncio.Task | None = None
        self._keepalive: asyncio.Task | None = None
        self._pushing: asyncio.Task | None = None
        # What comes back from the avatar, pushed in the order it came.
        self._back: asyncio.Queue[Frame] = asyncio.Queue()
        self._transport_ready = False
        # The face is in when its first frame arrives; from the next reply on, replies go to it.
        self._face_in = False
        self._face_arrived = asyncio.Event()
        # The reply being spoken: "face" (sent to the avatar), "own" (the bot's own voice), or None.
        self._speaking: str | None = None
        self._reply: str | None = None
        self._held = bytearray()
        self._held_rate = 24000
        # Replies the avatar hasn't finished saying, so the call's end can wait for its last words.
        self._unplayed = 0
        self._all_played = asyncio.Event()
        self._all_played.set()
        self._cut_until = 0.0
        self.failure: ChatFXException | None = None

    def can_generate_metrics(self) -> bool:
        return True

    # ---- Lifecycle -------------------------------------------------------------------------------------------------------

    async def setup(self, setup: FrameProcessorSetup) -> None:
        await super().setup(setup)
        # The avatar's voice comes back at your pipeline's output rate.
        self._out_rate = setup.audio_out_sample_rate or self._out_rate

    async def start(self, frame: StartFrame) -> None:
        await super().start(frame)
        self._started_at = time.monotonic()
        self._pushing = self.create_task(self._push_back())
        self._connecting = self.create_task(self._connect())
        if self._waiting_picture:
            self._showing = self.create_task(self._show_still())

    async def stop(self, frame: EndFrame) -> None:
        await super().stop(frame)
        # Let the avatar finish its last words (a goodbye) before it leaves.
        if self._face_in and self._unplayed:
            try:
                await asyncio.wait_for(self._all_played.wait(), LAST_WORDS_SECONDS)
                # The end of its voice is still on its way back to the bot.
                await asyncio.sleep(0.3)
            except asyncio.TimeoutError:
                pass
        await self._close()

    async def cancel(self, frame: CancelFrame) -> None:
        await super().cancel(frame)
        await self._close()

    def _http(self) -> aiohttp.ClientSession:
        if self._session is not None:
            return self._session
        if self._own_session is None or self._own_session.closed:
            self._own_session = aiohttp.ClientSession()
        return self._own_session

    async def _connect(self) -> None:
        try:
            self._rooms = DailyRooms(self._daily_key, self._http(), self._daily_api_url)
            url, self._room_name, bot_token, avatar_token = await self._rooms.create()
            self._seat = BotSeat(asyncio.get_running_loop(), self._out_rate, self._video, self._audio, self._message, self._avatar)
            await self._seat.join(url, bot_token)
            await self._bring_face(url, avatar_token)
        except ChatFXException as error:
            self.failure = error
            logger.warning(f"ChatFX: no face on this call ({error.code}): {error}. Your bot carries on with its own voice.")
            await self._close()
        except asyncio.CancelledError:
            raise
        except Exception as error:
            self.failure = ChatFXException(str(error))
            logger.exception("ChatFX: no face on this call. Your bot carries on with its own voice.")
            await self._close()

    async def _bring_face(self, room_url: str, avatar_token: str) -> None:
        # The same for every try in this call (see ATTEMPT_SECONDS).
        body = {"avatar_id": self._avatar_id, "connect": "daily", "daily": {"room_url": room_url, "token": avatar_token}, "request_key": uuid.uuid4().hex}
        deadline = time.monotonic() + self._start_timeout
        while time.monotonic() < deadline:
            wait = RETRY_SECONDS
            try:
                status, data, wait = await self._call("POST", "/v1/sessions", body, ATTEMPT_SECONDS)
            except (aiohttp.ClientError, asyncio.TimeoutError):
                logger.debug("ChatFX: the API isn't reachable yet", exc_info=True)
                status, data, wait = 0, {}, LOST_RETRY_SECONDS
            if status == 201 and data.get("id"):
                self._session_id = str(data["id"])
                self._keepalive = self.create_task(self._keep_alive())
                try:
                    await asyncio.wait_for(self._face_arrived.wait(), FACE_SECONDS)
                    logger.info(f"ChatFX: avatar {self._avatar_id} is on (session {self._session_id})")
                    return
                except asyncio.TimeoutError:
                    logger.warning("ChatFX: the avatar took the call but sent no face; trying again")
                    await self._end_session()
            elif status not in (0, 503) and status < 500 and not (status == 429 and (data.get("error") or {}).get("code") == "rate_limited"):
                error = data.get("error") or {}
                raise ChatFXException(error.get("message") or f"ChatFX Avatar answered {status}.", code=error.get("code") or "error", status=status)
            await asyncio.sleep(wait)
        raise ChatFXException("The avatar couldn't start in time.", code="start_timeout")

    async def _keep_alive(self) -> None:
        while self._session_id:
            await asyncio.sleep(KEEPALIVE_SECONDS)
            session_id = self._session_id
            if not session_id:
                return
            try:
                status, data, _ = await self._call("POST", f"/v1/sessions/{session_id}/keepalive")
            except (aiohttp.ClientError, asyncio.TimeoutError):
                logger.warning("ChatFX: a check-in didn't go through; trying again")
                continue
            if status != 200:
                # Ended, gone, or out of minutes: the face leaves, and the bot carries on with its own voice.
                logger.warning(f"ChatFX: the avatar's session ended ({(data.get('error') or {}).get('code', status)})")
                self._session_id = None
                return

    async def _end_session(self) -> None:
        if self._keepalive and asyncio.current_task() is not self._keepalive:
            await self.cancel_task(self._keepalive)
        self._keepalive = None
        session_id, self._session_id = self._session_id, None
        if session_id:
            try:
                await self._call("POST", f"/v1/sessions/{session_id}/end")
            except Exception:
                logger.warning("ChatFX: couldn't end the session; it ends by itself a minute after its last check-in")

    async def _close(self) -> None:
        if self._connecting and not self._connecting.done() and asyncio.current_task() is not self._connecting:
            await self.cancel_task(self._connecting)
        self._connecting = None
        if self._seat:
            self._seat.send({"chatfx": "end"})
        await self._end_session()
        if self._seat:
            await self._seat.leave()
            self._seat = None
        if self._rooms and self._room_name:
            await self._rooms.delete(self._room_name)
            self._room_name = None
        self._face_in = False
        if self._pushing:
            await self.cancel_task(self._pushing)
            self._pushing = None
        if self._showing and asyncio.current_task() is not self._showing:
            await self.cancel_task(self._showing)
        self._showing = None
        if self._own_session is not None:
            await self._own_session.close()
            self._own_session = None

    async def _call(self, method: str, path: str, body: dict[str, Any] | None = None, seconds: float = REQUEST_SECONDS) -> tuple[int, dict[str, Any], float]:
        async with self._http().request(
            method,
            self._api_url + path,
            json=body,
            headers={"Authorization": f"Bearer {self._api_key}"},
            timeout=aiohttp.ClientTimeout(total=seconds),
        ) as response:
            try:
                data = await response.json(content_type=None)
            except Exception:
                data = {}
            try:
                retry = float(response.headers.get("Retry-After", str(RETRY_SECONDS)))
            except ValueError:
                retry = RETRY_SECONDS
            return response.status, data if isinstance(data, dict) else {}, max(0.5, min(retry, 30.0))

    # ---- The avatar's face and voice, back into your pipeline ----------------------------------------------------------

    async def _show_still(self) -> None:
        """The avatar's picture as your bot's video until the live face is in (with the words while it's on its way)."""
        try:
            if self._stills is None:
                async with self._http().get(
                    f"{self._api_url}/v1/avatars/{self._avatar_id}/picture",
                    headers={"Authorization": f"Bearer {self._api_key}"},
                    timeout=aiohttp.ClientTimeout(total=REQUEST_SECONDS),
                ) as response:
                    if response.status != 200:
                        logger.debug(f"ChatFX: no waiting picture (the API answered {response.status})")
                        return
                    picture = await response.read()
                words = still_frame(picture, self._waiting_text)
                self._stills = (words, still_frame(picture, None) if self._waiting_text else words)
            while not self._face_in:
                if self._transport_ready:
                    # With no face coming (or gone), the picture stays without the words.
                    coming = self.failure is None and self._seat is not None
                    self._back.put_nowait(self._stills[0] if coming else self._stills[1])
                await asyncio.sleep(1 / STILL_FPS)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.debug("ChatFX: couldn't show the waiting picture", exc_info=True)

    async def _face_first(self) -> None:
        """Holds the bot's first words (up to face_first_seconds from the start) until the face is in."""
        left = self._started_at + self._face_first_seconds - time.monotonic()
        connecting = self._connecting
        if self._face_in or left <= 0 or connecting is None or connecting.done():
            return
        await asyncio.wait({connecting}, timeout=left)

    async def _push_back(self) -> None:
        while True:
            frame = await self._back.get()
            await self.push_frame(frame)

    def _avatar(self, here: bool) -> None:
        if not here:
            self._face_in = False
            self._face_arrived.clear()
            self._unplayed = 0
            self._all_played.set()
            logger.warning("ChatFX: the avatar left the call; your bot carries on with its own voice")
            if self._stills and self._waiting_picture and (self._showing is None or self._showing.done()):
                self._showing = self.create_task(self._show_still())

    def _video(self, frame: Any) -> None:
        if not self._transport_ready or not self._seat:
            return
        if not self._face_in:
            self._face_in = True
            self._face_arrived.set()
        self._back.put_nowait(OutputImageRawFrame(image=frame.buffer, size=(frame.width, frame.height), format=frame.color_format))

    def _audio(self, data: Any) -> None:
        # While the bot speaks with its own voice (before the face came in), the avatar's quiet stays out of the way.
        if not self._face_in or self._speaking == "own" or time.monotonic() < self._cut_until:
            return
        self._back.put_nowait(SpeechOutputAudioRawFrame(audio=data.audio_frames, sample_rate=data.sample_rate, num_channels=data.num_channels))

    def _message(self, message: Any, sender: str) -> None:
        # {"chatfx": "played", "seconds": 2.4, "interrupted": false}: the avatar finished saying a reply.
        if isinstance(message, dict) and message.get("chatfx") == "played" and not message.get("interrupted"):
            self._unplayed = max(0, self._unplayed - 1)
            if not self._unplayed:
                self._all_played.set()

    # ---- Your bot's voice, to the avatar --------------------------------------------------------------------------------

    def _send_piece(self, pcm: bytes, rate: int) -> None:
        if self._seat and self._reply:
            self._seat.send({"chatfx": "voice", "reply": self._reply, "audio": base64.b64encode(pcm).decode(), "rate": rate})

    def _send_sound(self, frame: TTSAudioRawFrame) -> None:
        if self._held and self._held_rate != frame.sample_rate:
            self._flush()
        self._held_rate = frame.sample_rate
        self._held.extend(frame.audio)
        piece = int(frame.sample_rate * PIECE_SECONDS) * 2
        while len(self._held) >= piece:
            self._send_piece(bytes(self._held[:piece]), frame.sample_rate)
            del self._held[:piece]

    def _flush(self) -> None:
        if self._held:
            self._send_piece(bytes(self._held), self._held_rate)
            self._held.clear()

    def _begin_reply(self) -> None:
        # A reply goes to the face only if the face is in when it starts, so a sentence is never split between voices.
        if self._face_in and self._seat is not None:
            self._speaking = "face"
            self._reply = uuid.uuid4().hex[:12]
        else:
            self._speaking = "own"

    def _end_reply(self) -> None:
        if self._speaking == "face" and self._reply:
            self._flush()
            if self._seat:
                self._seat.send({"chatfx": "voice", "reply": self._reply, "done": True})
                self._unplayed += 1
                self._all_played.clear()
        self._speaking = None
        self._reply = None

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)
        if isinstance(frame, TTSStartedFrame):
            if self._speaking:
                self._end_reply()
            await self._face_first()
            self._begin_reply()
            if self._speaking == "face":
                await self.start_ttfb_metrics()
            else:
                await self.push_frame(frame, direction)
        elif isinstance(frame, TTSAudioRawFrame):
            if not self._speaking:
                # Some TTS services send sound without a TTSStartedFrame first.
                await self._face_first()
                self._begin_reply()
            if self._speaking == "face" and not self._face_in:
                # The avatar left mid-reply: the rest of it is the bot's own voice.
                self._speaking, self._reply = "own", None
                self._held.clear()
            if self._speaking == "face":
                self._send_sound(frame)
            else:
                await self.push_frame(frame, direction)
        elif isinstance(frame, TTSStoppedFrame):
            # For a reply sent to the face, the avatar's own voice coming back says when the bot stops speaking.
            spoke = self._speaking
            self._end_reply()
            if spoke != "face":
                await self.push_frame(frame, direction)
        elif isinstance(frame, InterruptionFrame):
            if self._speaking == "face" or self._unplayed:
                self._cut_until = time.monotonic() + CUT_SECONDS
            self._held.clear()
            self._speaking = None
            self._reply = None
            self._unplayed = 0
            self._all_played.set()
            if self._seat:
                self._seat.send({"chatfx": "interrupt"})
            await self.push_frame(frame, direction)
        elif isinstance(frame, OutputTransportReadyFrame):
            self._transport_ready = True
            await self.push_frame(frame, direction)
        elif isinstance(frame, BotStartedSpeakingFrame):
            # The avatar's first words reached the visitor: that's the time to first sound.
            await self.stop_ttfb_metrics()
            await self.push_frame(frame, direction)
        else:
            await self.push_frame(frame, direction)

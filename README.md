# ChatFX Avatar for Pipecat

A real-time talking face for your Pipecat bot. Your bot's voice goes to the avatar, and its face and voice come back,
drawn together so the lips match, as your bot's own video and audio. You keep your own listening, AI and voice; ChatFX
adds the face, billed by the minute.

Tested with Pipecat v1.15.0 and v1.16.0 (Python 3.12). Built and maintained by the ChatFX Avatar team: questions and
issues go to [this repository's issues](https://github.com/ChatRep/pipecat-chatfx/issues) or service@chatfx.com.

## Install

```bash
pip install pipecat-chatfx
```

Requires Python 3.10+ and `pipecat-ai` 1.8 or newer (it brings `pipecat-ai[daily]`).

## Add your keys

- `CHATFX_AVATAR_API_KEY`: create one under **API keys** at [avatar.chatfx.com](https://avatar.chatfx.com).
- `DAILY_API_KEY`: from your [Daily dashboard](https://dashboard.daily.co/developers). Running on Pipecat Cloud, you already
  have one. The avatar meets your bot in a private Daily room in your account, made for each call and removed when it
  ends, the way Pipecat's own Tavus service works.

Put both in your bot's environment (your `.env`, or your Pipecat Cloud secret set).

## Use it

Put `ChatFXVideoService` right after your TTS service, and turn on your transport's video out at the avatar's size:

```python
from pipecat_chatfx import ChatFXVideoService

avatar = ChatFXVideoService(avatar_id="chatfx-lois")  # copy the id from the Avatars page

pipeline = Pipeline([
    transport.input(),
    stt,
    user_aggregator,
    llm,
    tts,
    avatar,
    transport.output(),
    assistant_aggregator,
])
```

```python
DailyParams(  # Daily, for local testing and real calls
    audio_in_enabled=True,
    audio_out_enabled=True,
    video_out_enabled=True,
    video_out_is_live=True,
    video_out_width=912,     # 912 x 512 landscape, 512 x 512 square, 384 x 512 portrait
    video_out_height=512,
    video_out_framerate=25,
)
```

**Use Daily as your transport, for local testing and real calls.** Your Daily key is already set for the avatar. Over
SmallWebRTC, the browser shows her lips about a third of a second behind her voice.

That's all the code. When your pipeline ends (or is cancelled when the visitor leaves), the avatar's session ends too, so
billing stops. Your page shows the bot's video as before: the avatar is the bot.

Step by step: [ChatFX Avatar for Pipecat](https://chatfx.com/support/avatar-pipecat/).

## Run the example

A complete bot (Deepgram listening, OpenAI, Cartesia voice, ChatFX Avatar face) is in
[`examples/bot.py`](examples/bot.py):

```bash
cd examples
cp .env.example .env   # add your keys
uv run bot.py -t daily
```

Then open http://localhost:7860/client, choose **Daily** at the top, click Connect and allow the microphone. The avatar
appears and says the first message; talk over it to see it stop.

## What it does for you

- **Shows a waiting picture.** Until the live face is in, your bot's video is the avatar's picture with "Video starting..."
  on it.
- **Greets with the face.** Your bot's first words wait up to 10 seconds for the face, so they come with it.
- **Never depends on the face.** If the face can't come, or leaves mid-call, your bot carries on with its own voice.
- **Stops at once when talked over.** An interruption stops the avatar mid-word.
- **Lets the last words finish.** At the end of a call, the avatar finishes its goodbye before it leaves.

## Options

```python
ChatFXVideoService(
    avatar_id="chatfx-lois",
    api_key=None,                    # defaults to CHATFX_AVATAR_API_KEY
    daily_api_key=None,              # defaults to DAILY_API_KEY
    api_url=None,                    # defaults to https://api.chatfx.com
    face_first_seconds=10.0,         # how long the first words wait for the face
    start_timeout=120.0,             # how long to keep trying to start the avatar
    waiting_picture=True,            # show the avatar's picture until the live face is in
    waiting_text="Video starting...",  # the words on it, in your language (None: no words)
)
```

If the avatar can't start (a wrong key, no minutes left), `avatar.failure` says why, with the API's `code`.

## Changelog

See [CHANGELOG.md](CHANGELOG.md).

## License

BSD 2-Clause, like Pipecat.

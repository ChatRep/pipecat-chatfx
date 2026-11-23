# Changelog

## 0.1.5

- Speech-to-speech services now drive the avatar too: a service that streams its voice as `SpeechOutputAudioRawFrame`
  (such as `OpenAILiveLLMService`) is sent to the avatar like TTS audio, so her lips move with it. Before, that voice
  went straight to the visitor and the avatar stayed silent. (Pipecat 1.17 made the same fix for Tavus and HeyGen.)
  TTS services, and speech-to-speech services that send `TTSAudioRawFrame`, work as before.
- Tested with Pipecat v1.17.0 (and v1.15.0, v1.16.0). The example uses Pipecat 1.17.0.

## 0.1.4

- Docs: use Daily as the transport, for local testing and real calls (over SmallWebRTC the browser shows her lips
  about a third of a second behind her voice). The example runs with `-t daily`.

## 0.1.3

- Fixed: a bot's second call stopped the whole program with daily-python 0.38.0 ("Execution context already exists").
  Daily's library is now started once per program, sharing Pipecat's DailyTransport flag, so neither starts it twice.

## 0.1.2

- Tested with Pipecat v1.16.0 (and daily-python 0.38.0). The example uses Pipecat 1.16.0.
- Source now public at https://github.com/ChatRep/pipecat-chatfx; the guide link points to
  https://chatfx.com/support/avatar-pipecat/.

## 0.1.1

- A face request that gets no answer within 10 seconds is asked again, as the same call, so a reply lost on the network
  costs seconds instead of a minute and never starts a second face. Needs nothing from you.

## 0.1.0

- First release: `ChatFXVideoService`, a ChatFX Avatar face and voice for any Pipecat bot. Put it right after your TTS
  service. Tested with Pipecat v1.15.0 and daily-python 0.37.0.

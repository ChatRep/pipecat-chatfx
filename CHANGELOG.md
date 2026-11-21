# Changelog

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

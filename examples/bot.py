"""A Pipecat bot with a ChatFX Avatar face.

Speech to text: Deepgram. LLM: OpenAI. Voice: Cartesia. Face: ChatFX Avatar, right after the voice.

Put your keys in .env (see .env.example), then:

    uv run bot.py -t daily

and open http://localhost:7860/client. Choose Daily at the top, click Connect and allow the microphone: the avatar appears and says the first message.
"""

import os

from dotenv import load_dotenv
from loguru import logger
from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.frames.frames import TTSSpeakFrame
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.worker import PipelineParams, PipelineWorker
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.aggregators.llm_response_universal import LLMContextAggregatorPair, LLMUserAggregatorParams
from pipecat.runner.types import RunnerArguments
from pipecat.runner.utils import create_transport
from pipecat.services.cartesia.tts import CartesiaTTSService
from pipecat.services.deepgram.stt import DeepgramSTTService
from pipecat.services.openai.llm import OpenAILLMService
from pipecat.transports.base_transport import BaseTransport, TransportParams
from pipecat.transports.daily.transport import DailyParams
from pipecat.workers.runner import WorkerRunner

from pipecat_chatfx import ChatFXVideoService

load_dotenv(override=True)

SYSTEM_PROMPT = "You are a friendly assistant on a video call. Keep answers short, with no special characters, emojis or markdown: they are spoken."
FIRST_MESSAGE = "Hello! What can I help you with today?"
# Copy the id from the Avatars page at avatar.chatfx.com.
AVATAR_ID = "chatfx-lois"
# The avatar's size: 912 x 512 for a landscape avatar, 512 x 512 square, 384 x 512 portrait.
VIDEO_WIDTH = 912
VIDEO_HEIGHT = 512

# Daily, for testing on your machine and for real calls (uv run bot.py -t daily). SmallWebRTC works too, but the browser
# shows her lips about a third of a second behind her voice. The avatar sends 25 pictures a second.
transport_params = {
    "daily": lambda: DailyParams(
        audio_in_enabled=True,
        audio_out_enabled=True,
        video_out_enabled=True,
        video_out_is_live=True,
        video_out_width=VIDEO_WIDTH,
        video_out_height=VIDEO_HEIGHT,
        video_out_framerate=25,
    ),
    "webrtc": lambda: TransportParams(
        audio_in_enabled=True,
        audio_out_enabled=True,
        video_out_enabled=True,
        video_out_is_live=True,
        video_out_width=VIDEO_WIDTH,
        video_out_height=VIDEO_HEIGHT,
        video_out_framerate=25,
    ),
}


async def run_bot(transport: BaseTransport, runner_args: RunnerArguments):
    stt = DeepgramSTTService(api_key=os.environ["DEEPGRAM_API_KEY"])
    llm = OpenAILLMService(api_key=os.environ["OPENAI_API_KEY"], settings=OpenAILLMService.Settings(system_instruction=SYSTEM_PROMPT))
    tts = CartesiaTTSService(
        api_key=os.environ["CARTESIA_API_KEY"],
        # Any Cartesia voice id.
        settings=CartesiaTTSService.Settings(voice="71a7ad14-091c-4e8e-a314-022ece01c121"),
    )
    # CHATFX_AVATAR_API_KEY and DAILY_API_KEY come from .env.
    avatar = ChatFXVideoService(avatar_id=AVATAR_ID)

    context = LLMContext()
    user_aggregator, assistant_aggregator = LLMContextAggregatorPair(
        context,
        user_params=LLMUserAggregatorParams(vad_analyzer=SileroVADAnalyzer()),
    )

    pipeline = Pipeline(
        [
            transport.input(),
            stt,
            user_aggregator,
            llm,
            tts,
            avatar,
            transport.output(),
            assistant_aggregator,
        ]
    )

    worker = PipelineWorker(
        pipeline,
        params=PipelineParams(enable_metrics=True),
        idle_timeout_secs=runner_args.pipeline_idle_timeout_secs,
    )

    @transport.event_handler("on_client_connected")
    async def on_client_connected(transport, client):
        logger.info("Client connected")
        await worker.queue_frames([TTSSpeakFrame(FIRST_MESSAGE)])

    @transport.event_handler("on_client_disconnected")
    async def on_client_disconnected(transport, client):
        # Ends the avatar's session too, so billing stops.
        await worker.cancel()

    runner = WorkerRunner(handle_sigint=runner_args.handle_sigint)
    await runner.add_workers(worker)
    await runner.run()


async def bot(runner_args: RunnerArguments):
    """Entry point for the local runner and for Pipecat Cloud."""
    transport = await create_transport(runner_args, transport_params)
    await run_bot(transport, runner_args)


if __name__ == "__main__":
    from pipecat.runner.run import main

    main()

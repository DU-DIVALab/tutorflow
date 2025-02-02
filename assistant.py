import logging
import pickle
from typing import List, Optional

from livekit.agents import AutoSubscribe, JobContext, WorkerOptions, cli, llm
from livekit.agents.pipeline import VoicePipelineAgent
from livekit.plugins import deepgram, openai, rag, silero

logger = logging.getLogger("philosophy-tutor")
annoy_index = rag.annoy.AnnoyIndex.load("vdb_data")

embeddings_dimension = 1536
with open("vector.pkl", "rb") as f:
    paragraphs_by_uuid = pickle.load(f)

class PhilosophyTutor:
    def __init__(self):
        self.covered_paragraphs = set()
        self.current_topic: Optional[str] = None
        self.teaching_queue: List[str] = []

    async def get_next_relevant_content(self, current_context: str) -> tuple[str, str]:
        """Get the next most relevant paragraph that hasn't been covered yet"""
        embedding = await openai.create_embeddings(
            input=[current_context],
            model="text-embedding-3-small",
            dimensions=embeddings_dimension,
        )
        
        results = annoy_index.query(embedding[0].embedding, n=5)
        
        # Find first uncovered paragraph
        for result in results:
            paragraph_id = result.userdata
            if paragraph_id not in self.covered_paragraphs:
                paragraph = paragraphs_by_uuid[paragraph_id]
                self.covered_paragraphs.add(paragraph_id)
                return paragraph_id, paragraph
                
        # If all nearby paragraphs are covered, get the least covered area
        all_paragraphs = set(paragraphs_by_uuid.keys())
        uncovered = all_paragraphs - self.covered_paragraphs
        if not uncovered:
            print("TESTING 123 123 123")
            return None, "We have covered all the material. Thank you for your participation"
            
        # Get a random uncovered paragraph
        next_id = uncovered.pop()
        self.covered_paragraphs.add(next_id)
        return next_id, paragraphs_by_uuid[next_id]

async def entrypoint(ctx: JobContext):
    tutor = PhilosophyTutor()
    
    async def _teaching_enrichment(agent: VoicePipelineAgent, chat_ctx: llm.ChatContext):
        user_msg = chat_ctx.messages[-1]
        
        # Get next relevant content based on user's message
        para_id, paragraph = await tutor.get_next_relevant_content(user_msg.content)
        
        if paragraph:
            logger.info(f"Teaching content: {paragraph}")
            context_msg = llm.ChatMessage.create(
                text=f"""Teaching Context:
Content: {paragraph}
Instructions: Use ONLY the information from the above content to respond to the user. 
Do not add any external knowledge. If the user asks something not covered in the content,
suggest exploring related topics from our materials instead.""",
                role="system",
            )
            chat_ctx.messages[-1] = context_msg
            chat_ctx.messages.append(user_msg)

    initial_ctx = llm.ChatContext().append(
        role="system",
        text=(
            "You are a philosophy tutor created by LiveKit. Your interface with users will be voice. "
            "You will teach philosophy using ONLY the content provided in the Teaching Context. "
            "Do not use any external knowledge. Keep responses concise and clear for voice interaction. "
            "If asked about topics not in your teaching materials, encourage exploring the topics "
            "that are available in the materials. "
            "Teach content in the order number, but do not explicitly acknowledge 'sections'. "
            "Similarly, do not explicitly acknowledge 'figures' while discussing them."
            ""
            "Regularly check understanding and encourage questions about the material covered."
        ),
    )

    await ctx.connect(auto_subscribe=AutoSubscribe.AUDIO_ONLY)

    agent = VoicePipelineAgent(
        chat_ctx=initial_ctx,
        vad=silero.VAD.load(),
        stt=deepgram.STT(),
        llm=openai.LLM(),
        tts=openai.TTS(),
        before_llm_cb=_teaching_enrichment,
    )

    agent.start(ctx.room)

    # Initialize with first piece of content
    _, first_paragraph = await tutor.get_next_relevant_content("introduction to philosophy")
    
    await agent.say(
        f"Welcome! I'm your philosophy tutor. Let's begin with this: {first_paragraph} "
        "What are your thoughts on this, or would you like me to explain further?",
        allow_interruptions=True
    )

if __name__ == "__main__":
    cli.run_app(WorkerOptions(entrypoint_fnc=entrypoint))
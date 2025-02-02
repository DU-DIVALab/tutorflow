import logging
import pickle
from enum import Enum
from typing import List, Optional, Tuple, Dict

from livekit.agents import AutoSubscribe, JobContext, WorkerOptions, cli, llm
from livekit.agents.pipeline import VoicePipelineAgent
from livekit.plugins import deepgram, openai, rag, silero, turn_detector

logger = logging.getLogger("philosophy-tutor")
annoy_index = rag.annoy.AnnoyIndex.load("vdb_data")

def prewarm(proc: JobProcess):
    proc.userdata["vad"] = silero.VAD.load()

embeddings_dimension = 1536
with open("vector.pkl", "rb") as f:
    paragraphs_by_uuid = pickle.load(f)

class TeachingMode(Enum):
    USER_LED = "user_led"
    AGENT_LED = "agent_led"
    HAND_RAISE = "hand_raise"

class PhilosophyTutor:
    def __init__(self, mode: TeachingMode):
        self.mode = mode
        self.covered_paragraphs = set()
        self.current_section: Optional[str] = None
        self.ordered_sections = self._organize_content()
        self.current_section_idx = 0
        self.current_paragraph_idx = 0
        self.hand_raised = False  # For HAND_RAISE mode
        self.section_understanding_confirmed = False
        self.last_progress_announcement = 0  # Track last announced progress percentage

    def get_progress_percentage(self) -> int:
        """Calculate current progress as a percentage"""
        total_paragraphs = sum(len(paragraphs) for paragraphs in self.ordered_sections.values())
        if total_paragraphs == 0:
            return 0
        return int((len(self.covered_paragraphs) / total_paragraphs) * 100)

    def should_announce_progress(self) -> tuple[bool, str]:
        """
        Determine if we should announce progress
        Returns: (should_announce, message)
        """
        current_progress = self.get_progress_percentage()
        
        # Announce every 10% increment if we haven't announced it yet
        if current_progress >= self.last_progress_announcement + 10:
            self.last_progress_announcement = (current_progress // 10) * 10
            message = f"You've completed {self.last_progress_announcement}% of the material. "
            
            # Add encouraging messages at specific milestones
            if self.last_progress_announcement == 50:
                message += "You're halfway through! Keep up the great work. "
            elif self.last_progress_announcement == 80:
                message += "Almost there! Just a bit more to go. "
            elif self.last_progress_announcement == 100:
                message += "Congratulations on completing the material! "
                
            return True, message
            
        return False, ""

    async def get_next_content(self, current_context: str) -> tuple[str, str, bool, bool]:
        """Get next content based on teaching mode and progress"""
        if self.current_section_idx >= len(self.ordered_sections):
            logger.info("DONE TEACHING")
            return None, "We have covered all the material. Thank you for your participation", True, False

        # Get current section and its paragraphs
        current_section = list(self.ordered_sections.keys())[self.current_section_idx]
        current_paragraphs = self.ordered_sections[current_section]

        # Check if we need understanding confirmation
        if self.mode == TeachingMode.AGENT_LED and not self.section_understanding_confirmed:
            return None, "Please demonstrate your understanding of what we've discussed before we continue.", True, True

        # Move to next section if current is complete
        if self.current_paragraph_idx >= len(current_paragraphs):
            if self.mode == TeachingMode.AGENT_LED:
                self.section_understanding_confirmed = False
            self.current_section_idx += 1
            self.current_paragraph_idx = 0
            return await self.get_next_content(current_context)

        # Get current paragraph
        para_id, content = current_paragraphs[self.current_paragraph_idx]
        self.current_paragraph_idx += 1
        self.covered_paragraphs.add(para_id)

        # Check if we should announce progress
        should_announce, progress_message = self.should_announce_progress()
        if should_announce:
            content = progress_message + self._clean_content(content)
        else:
            content = self._clean_content(content)

        # Determine interruption settings
        allow_interruptions = True
        if self.mode == TeachingMode.HAND_RAISE:
            allow_interruptions = self.hand_raised

        requires_understanding = self.mode == TeachingMode.AGENT_LED
        
        return para_id, content, allow_interruptions, requires_understanding
    
async def entrypoint(ctx: JobContext):
    # Get teaching mode from environment or config
    if "tutor" not in ctx.proc.userdata:
        mode = TeachingMode.USER_LED
        ctx.proc.userdata["tutor"] = PhilosophyTutor(mode)
    
    tutor = ctx.proc.userdata["tutor"]

    async def _teaching_enrichment(agent: VoicePipelineAgent, chat_ctx: llm.ChatContext):
        user_msg = chat_ctx.messages[-1]
        
        # Check for understanding in agent-led mode
        if tutor.mode == TeachingMode.AGENT_LED and not tutor.section_understanding_confirmed:
            # Use embeddings to evaluate understanding
            embedding = await openai.create_embeddings(
                input=[user_msg.content],
                model="text-embedding-3-small",
                dimensions=embeddings_dimension,
            )
            
            # Compare with current section content to evaluate understanding
            # This is a simplified check - you might want to make it more sophisticated
            current_section = list(tutor.ordered_sections.keys())[tutor.current_section_idx]
            current_content = tutor.ordered_sections[current_section]
            
            # If understanding is demonstrated, confirm and continue
            if True:  # Replace with actual understanding check
                tutor.confirm_understanding()
        
        # Get next content
        para_id, paragraph, allow_interruptions, requires_understanding = await tutor.get_next_content(user_msg.content)
        
        if paragraph:
            logger.info(f"Teaching content: {paragraph}")
            
            # Add appropriate instructions based on mode
            mode_instructions = ""
            if tutor.mode == TeachingMode.AGENT_LED:
                mode_instructions = "Ensure the user demonstrates understanding before proceeding. Ask specific questions about the content. When asking a question do not ask the user's opinion a topic, rather ask to guage understanding."
            elif tutor.mode == TeachingMode.HAND_RAISE:
                mode_instructions = "Wait for the user to raise their hand before allowing interruptions."
                
            context_msg = llm.ChatMessage.create(
                text=f"""Teaching Context:
Content: {paragraph}
Instructions: Use ONLY the information from the above content to respond to the user. 
{mode_instructions}
Do not add any external knowledge. If the user asks something not covered in the content,
suggest exploring related topics from our materials instead.""",
                role="system",
            )
            chat_ctx.messages[-1] = context_msg
            chat_ctx.messages.append(user_msg)
            
            # Update agent's interruption settings
            agent.allow_interruptions = allow_interruptions

    initial_ctx = llm.ChatContext().append(
        role="system",
        text=(
            "You are a philosophy tutor created by LiveKit engaging in voice-based teaching. "
            f"You are teaching in {mode.value} mode with these core principles:\n"
            "- Focus exclusively on the content provided in the Teaching Context - never introduce external concepts\n"
            "- Maintain a natural, conversational tone as if discussing with a colleague\n"
            "- Present ideas progressively, one concept at a time\n"
            "- Keep explanations concise and high-level while ensuring understanding\n\n"
            "Your teaching approach:\n"
            "- Introduce concepts individually with brief, focused explanations\n"
            "- Use short, relevant examples when clarifying points\n"
            "- Keep responses crisp and targeted to maintain engagement\n"
            "- Build understanding step by step before moving forward\n\n"
            "Interaction guidelines:\n"
            "- Maintain a brisk but comprehensible pace\n"
            "- Do not ask the user about their opinion ever"
            "- For off-topic questions, acknowledge briefly then guide back to the current topic\n"
            "- Never reference document structure, sections, or figures\n"
            "- Keep the conversation flowing naturally without sounding like you're reading from a text\n"
            "- If a topic isn't in your teaching materials, redirect to related concepts within your content\n"
            "- Check understanding regularly through natural conversation\n"
        ),
    )

    await ctx.connect(auto_subscribe=AutoSubscribe.AUDIO_ONLY)

    agent = VoicePipelineAgent(
        chat_ctx=initial_ctx,
        vad=ctx.proc.userdata["vad"],#vad=silero.VAD.load(),
        stt=deepgram.STT(),
        llm=openai.LLM(model="gpt-4o-mini"),#llm=openai.LLM(),
        tts=openai.TTS(),
        before_llm_cb=_teaching_enrichment,
        turn_detector=turn_detector.EOUModel(),
    )

    agent.start(ctx.room)

    # Initialize with first piece of content
    _, first_paragraph, allow_interruptions, requires_understanding = await tutor.get_next_content("introduction")
    
    await agent.say(
        f"Welcome! I'm your philosophy tutor. Let's begin. {first_paragraph} ",
        allow_interruptions=allow_interruptions
    )

if __name__ == "__main__":
    cli.run_app(
        WorkerOptions(
            entrypoint_fnc=entrypoint,
            prewarm_fnc=prewarm,
        ),
    )
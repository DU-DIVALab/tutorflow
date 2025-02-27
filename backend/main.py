import logging
import pickle
from enum import Enum
from typing import List, Optional, Tuple, Dict

from livekit.agents import AutoSubscribe, JobContext, JobProcess, WorkerOptions, cli, llm
from livekit.agents.pipeline import VoicePipelineAgent
from livekit.plugins import deepgram, openai, rag, silero, turn_detector

# TODO: raise hand prmpts question from model (agent finishes sentence then asks)
# TODO: user led interaction/ different prompt, only agent led should have agent ask if user following along

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("philosophy-tutor")

try:
    annoy_index = rag.annoy.AnnoyIndex.load("vdb_data")
except Exception as e:
    logger.error(f"Failed to load annoy index: {e}")
    annoy_index = None

embeddings_dimension = 1536

try:
    with open("vector.pkl", "rb") as f:
        paragraphs_by_uuid = pickle.load(f)
except Exception as e:
    logger.error(f"Failed to load vector data: {e}")
    paragraphs_by_uuid = {}

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
        self.hand_raised = False
        self.section_understanding_confirmed = False
        self.last_progress_announcement = 0
        self.total_paragraphs = sum(len(paragraphs) for paragraphs in self.ordered_sections.values())
        logger.info(f"Initialized tutor with {self.total_paragraphs} total paragraphs")

    def get_progress_percentage(self) -> int:
        if self.total_paragraphs == 0:
            return 0
        progress = int((len(self.covered_paragraphs) / self.total_paragraphs) * 100)
        logger.debug(f"Current progress: {progress}%")
        return progress

    def should_announce_progress(self) -> tuple[bool, str]:
        current_progress = self.get_progress_percentage()
        
        if current_progress > self.last_progress_announcement + 10:
            self.last_progress_announcement = (current_progress // 10) * 10
            message = f"(Important: The user has completed {self.last_progress_announcement}% of the material. Tell them this.) "
            
            if self.last_progress_announcement == 50:
                message += "You're halfway through! Keep up the great work. "
            elif self.last_progress_announcement == 80:
                message += "Almost there! Just a bit more to go. "
            elif self.last_progress_announcement == 100:
                message += "Congratulations on completing the material! "
                #logger.info("USER IS DONE.")
            
            logger.info(f"Progress announcement: {message}")
            return True, message
            
        return False, ""

    def _organize_content(self) -> Dict[str, List[Tuple[str, str]]]:
        sections: Dict[str, List[Tuple[str, str]]] = {}
        current_section = None
        current_content = []

        for id, content in paragraphs_by_uuid.items():
            if content.startswith('##'):
                if current_section and current_content:
                    sections[current_section] = current_content
                current_section = content.split('\n')[0]
                current_content = [(id, content)]
            elif current_section:
                current_content.append((id, content))

        if current_section and current_content:
            sections[current_section] = current_content

        logger.info(f"Organized content into {len(sections)} sections")
        return dict(sorted(sections.items()))

    def _clean_content(self, content: str) -> str:
        if content.startswith('##'):
            content = content.split('\n', 1)[1] if '\n' in content else ''
        return content.strip()

    def raise_hand(self):
        self.hand_raised = True
        logger.info("Hand raised")

    def lower_hand(self):
        self.hand_raised = False
        logger.info("Hand lowered")

    def confirm_understanding(self):
        self.section_understanding_confirmed = True
        logger.info("Understanding confirmed for current section")

    async def get_next_content(self, current_context: str) -> tuple[Optional[str], str, bool, bool]:
        try:
            if self.current_section_idx >= len(self.ordered_sections):
                logger.info("Teaching completed")
                return None, "We have covered all the material. Thank you for your participation", True, False

            current_section = list(self.ordered_sections.keys())[self.current_section_idx]
            current_paragraphs = self.ordered_sections[current_section]

            if self.mode == TeachingMode.AGENT_LED and not self.section_understanding_confirmed:
                return None, "Please demonstrate your understanding of what we've discussed before we continue.", True, True

            if self.current_paragraph_idx >= len(current_paragraphs):
                if self.mode == TeachingMode.AGENT_LED:
                    self.section_understanding_confirmed = False
                self.current_section_idx += 1
                self.current_paragraph_idx = 0
                logger.info(f"Moving to next section: {self.current_section_idx}")
                return await self.get_next_content(current_context)

            para_id, content = current_paragraphs[self.current_paragraph_idx]
            self.current_paragraph_idx += 1
            self.covered_paragraphs.add(para_id)

            should_announce, progress_message = self.should_announce_progress()
            cleaned_content = self._clean_content(content)
            
            if should_announce:
                cleaned_content = progress_message + cleaned_content

            allow_interruptions = True if self.mode != TeachingMode.HAND_RAISE else self.hand_raised
            requires_understanding = self.mode == TeachingMode.AGENT_LED

            logger.debug(f"Delivering content: {para_id}")
            return para_id, cleaned_content, allow_interruptions, requires_understanding

        except Exception as e:
            logger.error(f"Error in get_next_content: {e}")
            return None, "I apologize, but I encountered an error. Let's try to continue.", True, False

async def _teaching_enrichment(agent: VoicePipelineAgent, chat_ctx: llm.ChatContext, tutor: PhilosophyTutor):
    try:
        user_msg = chat_ctx.messages[-1]
        
        if tutor.mode == TeachingMode.AGENT_LED and not tutor.section_understanding_confirmed:
            embedding = await openai.create_embeddings(
                input=[user_msg.content],
                model="text-embedding-3-small",
                dimensions=embeddings_dimension,
            )
            
            current_section = list(tutor.ordered_sections.keys())[tutor.current_section_idx]
            if True:  # Replace with actual understanding check
                tutor.confirm_understanding()
        
        para_id, paragraph, allow_interruptions, requires_understanding = await tutor.get_next_content(user_msg.content)
        
        if paragraph:
            logger.info(f"Teaching content: {paragraph[:100]}...")
            
            mode_instructions = ""
            if tutor.mode == TeachingMode.AGENT_LED:
                mode_instructions = "Ensure user understanding before proceeding. Ask specific questions about the content to gauge understanding, not opinions."
            elif tutor.mode == TeachingMode.HAND_RAISE:
                mode_instructions = "Wait for user to raise their hand before allowing interruptions."
                
            context_msg = llm.ChatMessage.create(
                text=f"""Teaching Context:
Content: {paragraph}
Instructions: Use ONLY the above content to respond. {mode_instructions}
Avoid external knowledge. For off-topic questions, redirect to related material topics.""",
                role="system",
            )
            chat_ctx.messages[-1] = context_msg
            chat_ctx.messages.append(user_msg)
            
            agent.allow_interruptions = allow_interruptions

    except Exception as e:
        logger.error(f"Error in teaching enrichment: {e}")
        raise

async def entrypoint(ctx: JobContext):
    try:
        await ctx.connect(auto_subscribe=AutoSubscribe.AUDIO_ONLY)
        logger.info("Room connection established")

        if "tutor" not in ctx.proc.userdata:
            mode = TeachingMode.USER_LED
            ctx.proc.userdata["tutor"] = PhilosophyTutor(mode)
        
        tutor = ctx.proc.userdata["tutor"]
        mode = tutor.mode

        initial_ctx = llm.ChatContext().append(
            role="system",
            text=(
                "You are a philosophy tutor created by LiveKit engaging in voice-based teaching. "
                f"Teaching in {mode.value} mode with core principles:\n"
                "- Focus exclusively on the content provided in the Teaching Context - never introduce external concepts\n"
                "- Maintain a natural, conversational tone as if discussing with a colleague, try not to sound like a textbook\n"
                "- Do not sound like you are reading off a textbook\n"
                "- Use disfluencies like 'uh' 'uhm' and 'like' to sound more human\n"
                "- Add connections to the modern student's life experiences\n"
                "- Present ideas progressively, one concept at a time for better understanding\n"
                "- Keep explanations concise and high-level while ensuring understanding. It is important to stay concise.\n\n"
                "Your teaching approach:\n"
                "- Introduce concepts individually with brief, focused explanations\n"
                "- Use short, relevant examples when clarifying points\n"
                "- Keep responses crisp and targeted to maintain engagement\n"
                "- Build understanding step by step before moving forward\n\n"
                "Interaction guidelines:\n"
                "- Maintain a brisk but comprehensible pace\n"
                "- Do not ask the user about their opinion, ever\n"
                "- For off-topic questions, acknowledge briefly then guide back to the current topic\n"
                "- Never reference document structure, sections, or figures\n"
                "- Keep the conversation flowing naturally without sounding like you're reading from a text\n"
                "- If a topic isn't in your teaching materials, acknowledge the user's question but steer them back to related concepts within your content instead of saying you don't know\n"
                "- Check understanding regularly through natural conversation\n"
                "- Important: DO NOT GO INTO DETAIL. SUMMARIZE, STAY HIGH-LEVEL AND REMAIN CONCISE. A PARAGRAPH IN VECTORSTORE SHOULD BE AT MOST A COUPLE SENTENCES"
                "- Important: When the user reaches a certain percentage of the material, let them know for every 20%% of progress they made.\n"
                "- CRITICAL: When the user is done with the whole material. Tell them the code is 'strawberry'"
            ),
        )

        agent = VoicePipelineAgent(
            chat_ctx=initial_ctx,
            vad=ctx.proc.userdata.get("vad") or silero.VAD.load(),
            stt=deepgram.STT(),
            llm=openai.LLM(model="gpt-4o-mini"),
            tts=openai.TTS(),
            before_llm_cb=lambda a, c: _teaching_enrichment(a, c, tutor),
            turn_detector=turn_detector.EOUModel(),
        )

        agent.start(ctx.room)
        logger.info("Agent started successfully")

        _, first_paragraph, allow_interruptions, requires_understanding = await tutor.get_next_content("introduction")
        
        await agent.say(
            f"Welcome! I'm your philosophy tutor. Let's begin. {first_paragraph}",
            allow_interruptions=allow_interruptions
        )

    except Exception as e:
        logger.error(f"Failed to initialize: {str(e)}")
        raise

def prewarm(proc: JobProcess):
    try:
        proc.userdata["vad"] = silero.VAD.load()
        logger.info("VAD prewarm completed")
    except Exception as e:
        logger.error(f"Failed to prewarm: {e}")
        raise

if __name__ == "__main__":
    cli.run_app(
        WorkerOptions(
            entrypoint_fnc=entrypoint,
            prewarm_fnc=prewarm,
        ),
    )
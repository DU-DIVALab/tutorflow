# TODO: help! our agent skips over whatever its talking about if the user butts in and just says ok
# TODO: (frontend) make it go back to ignoring the user unless their hand is raised (maybe?) i think this might be a backend thing actually
# FIXME: only agent led should ask if user follows along (??)


import os
import datetime
import logging
import pickle
from enum import Enum
from typing import List, Optional, Tuple, Dict


from livekit.agents import AutoSubscribe, JobContext, JobProcess, WorkerOptions, cli, llm, stt, transcription
from livekit.agents.pipeline import VoicePipelineAgent
from livekit.plugins import deepgram, openai, rag, silero, turn_detector
from livekit.rtc.room import DataPacket

import asyncio

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



                  
def get_transcript_path(room_name):
    timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    transcript_dir = f"transcripts/{room_name}"
    os.makedirs(transcript_dir, exist_ok=True)
    return f"{transcript_dir}/transcript_{timestamp}.txt"

def initialize_transcript(file_path, room_name, mode):
    with open(file_path, "w") as f:
        f.write(f"Transcript for room: {room_name}\n")
        f.write(f"Mode: {mode}\n")
        f.write(f"Date: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
        f.write("-" * 50 + "\n\n")

def save_to_transcript(file_path, speaker, text):
    try:
        timestamp = datetime.datetime.now().strftime("%H:%M:%S")
        message = f"[{timestamp}] {speaker}: {text}\n"
        
        with open(file_path, "a") as f:
            f.write(message)
        
        return True
    except Exception as e:
        logger.error(f"Failed to save transcript: {e}")
        return False

class PhilosophyTutor:
    def __init__(self, mode: TeachingMode, ctx = None):
        self.ctx = ctx
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
        logger.info(f"Initialized tutor with {self.total_paragraphs} total paragraphs in {mode.value} mode")

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
            elif self.last_progress_announcement >= 98:
                message += "Congratulations on completing the material! The code is 'strawberry'. "
                logger.info("USER IS DONE. SENDING STRAWBERRY CODE.")
                
                # Use self.ctx instead of ctx
                if self.ctx:
                    asyncio.create_task(self.ctx.room.local_participant.publish_data(
                        payload="strawberry",
                        reliable=True,
                        topic="command"
                    ))
                else:
                    exit(1)
            
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
            if self.mode == TeachingMode.HAND_RAISE and self.hand_raised:
                self.hand_raised = False 
                return None, "Sure! What's your question?", True, False


            if self.current_section_idx >= len(self.ordered_sections):
                logger.info("Teaching completed")
                return None, "We have covered all the material. Thank you for your participation", True, False

            current_section = list(self.ordered_sections.keys())[self.current_section_idx]
            current_paragraphs = self.ordered_sections[current_section]

            if self.mode == TeachingMode.AGENT_LED and not self.section_understanding_confirmed:
                # TODO: dont do this on start, or do idk
                return None, "Please demonstrate your understanding of what you know before we continue.", True, True
                #return None, "Please demonstrate your understanding of what we've discussed before we continue.", True, True

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

async def _teaching_enrichment(agent: VoicePipelineAgent, chat_ctx: llm.ChatContext, tutor: PhilosophyTutor, ctx: JobContext):
    try:
        user_msg = chat_ctx.messages[-1]
        
        # We don't want to save a user's transcript
        # if user_msg.role == "user" and hasattr(agent, "transcript_file"):
        #   save_to_transcript(agent.transcript_file, "User", user_msg.content)

        # Check if hand is raised in HAND_RAISE mode
        if tutor.hand_raised:
            logger.info("Hand raised detected in teaching_enrichment")
            
            # Don't lower the hand yet - let get_next_content handle it
            # This ensures the special message gets delivered
            
            hand_raise_msg = llm.ChatMessage.create(
                text="The user has raised their hand. Finish your current sentence, then respond with 'I see you've raised your hand. What's your question?' and wait for their input.",
                role="system",
            )
            chat_ctx.messages[-1] = hand_raise_msg
            chat_ctx.messages.append(user_msg)
            agent.allow_interruptions = True
            return
  
        if tutor.mode == TeachingMode.AGENT_LED and not tutor.section_understanding_confirmed:
            embedding = await openai.create_embeddings(
                input=[user_msg.content],
                model="text-embedding-3-small",
                dimensions=embeddings_dimension,
            )
            
            current_section = list(tutor.ordered_sections.keys())[tutor.current_section_idx]
            if True:  # TODO: Replace with actual understanding check
                tutor.confirm_understanding()
        
        # check compleition
        current_progress = tutor.get_progress_percentage()
        if current_progress >= 98:
            # Ensure strawberry code is explicitly mentioned when content is completed
            completion_msg = llm.ChatMessage.create(
                text="CRITICAL: The user has completed the material! Make sure to tell them: 'Congratulations on completing all the material! Your code is strawberry.' This is extremely important.",
                role="system",
            )
            chat_ctx.messages.append(completion_msg)
            asyncio.create_task(ctx.room.local_participant.publish_data(
                        payload="strawberry",
                        reliable=True,
                        topic="command"
            ))
            logger.info("Added strawberry code message for 100% completion")

        
        para_id, paragraph, allow_interruptions, requires_understanding = await tutor.get_next_content(user_msg.content)
        
        if paragraph:
            logger.info(f"Teaching content: {paragraph[:100]}...")
            
            mode_instructions = ""
            if tutor.mode == TeachingMode.AGENT_LED:
                mode_instructions = "Ensure user understanding before proceeding. Ask specific questions about the content to gauge understanding, not opinions. Build understanding step by step before moving forward" 
            elif tutor.mode == TeachingMode.HAND_RAISE:
                mode_instructions = "Encourage the user to ask questions at natural pauses by raising their hand. Wait for user to raise their hand before allowing interruptions."
                if tutor.hand_raised:
                    tutor.lower_hand() 
                    agent.allow_interruptions = True
                    chat_ctx.messages.append(llm.ChatMessage.create(text="You raised your hand! What's your question?", role="assistant")) 
            
            # Check if the paragraph contains the strawberry code completion message
            if "strawberry" in paragraph.lower():
                logger.info("DETECTED STRAWBERRY CODE IN CONTENT - ENSURING IT'S PRONOUNCED")
                # Add additional instruction to ensure strawberry code is spoken clearly
                mode_instructions += " CRITICAL: Make sure to clearly say the word 'strawberry' as the code."
                
            context_msg = llm.ChatMessage.create(
                text=f"""Teaching Context:
Content: {paragraph}

STRICT RULES:
1. ONLY teach what's explicitly contained in the above content
2. You must teach every concept, theory and factoid mentioned
3. Do NOT introduce ANY external concepts, theories, or thinkers

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


        room_name = ctx.room.name
        if "SQUARE" in room_name:
            mode = TeachingMode.USER_LED
        elif "CIRCLE" in room_name:
            mode = TeachingMode.AGENT_LED
        elif "TRIANGLE" in room_name:
            mode = TeachingMode.HAND_RAISE
        else:
            return
        
        logger.info(f"Using mode from room name: {mode.value}")
        if "tutor" not in ctx.proc.userdata:
            ctx.proc.userdata["tutor"] = PhilosophyTutor(mode, ctx)
        
        tutor = ctx.proc.userdata["tutor"]
        mode = tutor.mode

        # transcript setup
        transcript_file = get_transcript_path(room_name)
        initialize_transcript(transcript_file, room_name, mode.value)
        logger.info(f"Transcript will be saved to: {transcript_file}")


        specific_a = """- Add connections to the modern student's life experiences\n
- Present ideas progressively, one concept at a time for better understanding
"""
        specific_b = """

"""
        if mode == TeachingMode.USER_LED:
            specific_a = "- Move on to the next topic only when the USER decides they want to"
            specific_b = ""
        initial_ctx = llm.ChatContext().append(
            role="system",
            text=(
                "You are a philosophy tutor engaging in voice-based teaching. "
                f"Teaching in {mode.value} mode with core principles:\n"
                "- Focus exclusively on the content provided in the Teaching Context - never introduce external concepts\n"
                "- Maintain a natural, conversational tone as if discussing with a colleague, try not to sound like a textbook\n"
                # "- Do not sound like you are reading off a textbook\n"
                "- Use disfluencies like 'uh' 'uhm' and 'like' to sound more human\n"
                f"{specific_a}"
                "- Keep explanations concise and high-level while ensuring understanding. It is important to stay concise.\n\n"
                "Your teaching approach:\n"
                # "- Introduce concepts individually with brief, focused explanations\n"
                # "- Summarize bigger ideas to maintain user engagement\n"
                "- Use short, relevant examples when clarifying points\n"
                "- Keep responses short, crisp, and targeted to maintain engagement\n\n"
                "Interaction guidelines:\n"
                "- Maintain a brisk but comprehensible pace\n"
                "- Do not ask the user about their opinion, ever\n"
                "- For off-topic questions, acknowledge briefly then guide back to the current topic\n"
                "- Keep the conversation flowing naturally without sounding like you're reading from a text\n"
                "- If a topic isn't in your teaching materials, acknowledge the user's question but steer them back to related concepts within your content instead of saying you don't know\n"
                "- Check understanding regularly through natural conversation\n"
                "- Maintain a brisk but comprehensible pace while adapting based on the following teaching modes: \n"

                "- CRITICAL: When the user reaches a certain percentage of the material covered, let them know for every 33%% of progress they make.\n"
                "- CRITICAL: When the user is done with the whole material. Tell them the code is 'strawberry'\n"
                "- CRITICAL: MENTION EACH EXAMPLE/KEYWORD GIVEN TO YOU THE USER MUST HEAR ALL OF THEM"
                "- CRITICAL: IF IT MENTIONS A PERSON, YOU MUST MENTION THAT PERSON."
            ),
        )

        agent = VoicePipelineAgent(
            chat_ctx=initial_ctx,
            vad=ctx.proc.userdata.get("vad") or silero.VAD.load(),
            stt=deepgram.STT(),
            llm=openai.LLM(model="gpt-4o-mini"),
            tts=openai.TTS(),
            before_llm_cb=lambda a, c: _teaching_enrichment(a, c, tutor, ctx),
            turn_detector=turn_detector.EOUModel(),
        )

        setattr(agent, "transcript_file", transcript_file)


        def on_data_received(packet: DataPacket):
            if packet.topic == "command":
                command = packet.data.decode('utf-8').strip().upper()
                if command == "HAND_RAISED":
                    tutor = ctx.proc.userdata.get("tutor")
                    save_to_transcript(transcript_file, "System", "User raised hand")
                    if tutor:
                        tutor.raise_hand()
        
        def on_transcription_received(msg):
            save_to_transcript(transcript_file, "Agent", msg.content)

        ctx.room.on("data_received", on_data_received)
        agent.on("agent_speech_committed", on_transcription_received)

        agent.start(ctx.room)


        
        logger.info("Agent started successfully")

        _, first_paragraph, allow_interruptions, requires_understanding = await tutor.get_next_content("introduction")
    

        special = {
            "user_led": "I'll teach you philosophy and its incumbent on you to interrupt me to ask questions and move on to new parts of the material.",
            "agent_led": "I'll be teaching you philosophy.",
            "hand_raise": "I'll be teaching you Philosophy. Feel free to raise your hand when you have a question so that I may call on you."
        }


        await agent.say(
            f"Welcome! I'm your philosophy tutor. {special[tutor.mode.value]} Let's begin. {first_paragraph}",
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
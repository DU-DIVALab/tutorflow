import os
import datetime
import logging
import pickle
import time
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
        self.section_understanding_confirmed = True  # Start as True to skip initial understanding check
        self.last_progress_announcement = 0
        self.total_paragraphs = sum(len(paragraphs) for paragraphs in self.ordered_sections.values())
        self.last_content_time = time.time()  # Track when content was last delivered
        self.auto_continue_delay = 2.0  # Seconds to wait before auto-continuing in USER_LED mode
        self.monologue_active = False  # Flag to track if monologue is actively running
        self.has_started = False  # Flag to track if teaching has started
        self.was_interrupted = False  # Flag to track if user interrupted
        self.last_delivered_para_id = None  # Track the last successfully delivered paragraph
        self.understanding_check_in_progress = False  # Flag to track if we're checking understanding
        self.user_question_answered = True  # Flag to track if user questions have been answered
        logger.info(f"Initialized tutor with {self.total_paragraphs} total paragraphs in {mode.value} mode")

    def get_progress_percentage(self) -> int:
        if self.total_paragraphs == 0:
            return 0
        progress = int((len(self.covered_paragraphs) / self.total_paragraphs) * 100)
        logger.debug(f"Current progress: {progress}%")
        return progress

    def should_announce_progress(self) -> tuple[bool, str]:
        current_progress = self.get_progress_percentage()
        message = ""
        
        if current_progress > self.last_progress_announcement + 10:
            self.last_progress_announcement = (current_progress // 10) * 10
            

            if self.last_progress_announcement == 10:
                message += "You're ten percent of the way through. Excellent work. "

            if self.last_progress_announcement == 30:
                message += "You're a third of the way through. Good job. "

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

        # Check if we have any content at all
        if not paragraphs_by_uuid:
            logger.error("No content found in paragraphs_by_uuid")
            # Create a default section with placeholder content
            return {"## Default Section": [("default_id", "## Default Section\nNo content available. This is placeholder content.")]}

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

        # If we still have no sections, create a default one
        if not sections:
            logger.error("Failed to organize content into sections")
            return {"## Default Section": [("default_id", "## Default Section\nNo content available. This is placeholder content.")]}

        logger.info(f"Organized content into {len(sections)} sections")
        return dict(sorted(sections.items()))

    def _clean_content(self, content: str) -> str:
        if content.startswith('##'):
            content = content.split('\n', 1)[1] if '\n' in content else ''
        return content.strip()

    def raise_hand(self):
        self.hand_raised = True
        self.monologue_active = False  # Pause monologue if hand is raised
        logger.info("Hand raised")

    def lower_hand(self):
        self.hand_raised = False
        logger.info("Hand lowered")

    def confirm_understanding(self):
        self.section_understanding_confirmed = True
        self.understanding_check_in_progress = False
        logger.info("Understanding confirmed for current section")

    def mark_paragraph_as_covered(self, para_id):
        if not para_id:
            logger.warning("Attempted to mark None paragraph as covered")
            return False
            
        if para_id not in self.covered_paragraphs:
            self.covered_paragraphs.add(para_id)
            self.last_delivered_para_id = para_id
            logger.info(f"Marked paragraph {para_id} as covered")
            return True
        return False

    def handle_interruption(self):
        self.was_interrupted = True
        self.monologue_active = False
        self.user_question_answered = False
        logger.info("User interruption recorded")

    def mark_question_answered(self):
        self.user_question_answered = True
        logger.info("User question marked as answered")

    def get_current_paragraph(self):
        if self.current_section_idx >= len(self.ordered_sections):
            return None, None
            
        current_section = list(self.ordered_sections.keys())[self.current_section_idx]
        current_paragraphs = self.ordered_sections[current_section]
        
        if self.current_paragraph_idx >= len(current_paragraphs):
            return None, None
            
        return current_paragraphs[self.current_paragraph_idx]

    async def get_next_content(self, current_context: str) -> tuple[Optional[str], str, bool, bool]:
        try:
            # If user was interrupted and hasn't had their question answered yet, don't proceed
            if self.was_interrupted and not self.user_question_answered:
                logger.info("Waiting for user question to be answered before continuing")
                return None, "", True, False
                
            if self.mode == TeachingMode.HAND_RAISE and self.hand_raised:
                self.hand_raised = False 
                return None, "Sure! What's your question?", True, False

            if self.current_section_idx >= len(self.ordered_sections):
                logger.info("Teaching completed - reached end of sections")
                self.monologue_active = False  # Stop monologue when content is done
                return None, "We have covered all the material. Thank you for your participation", True, False

            # Log the current section and paragraph indexes
            logger.info(f"get_next_content: section_idx={self.current_section_idx}, paragraph_idx={self.current_paragraph_idx}")
            logger.info(f"get_next_content: total_sections={len(self.ordered_sections)}")
            
            current_section = list(self.ordered_sections.keys())[self.current_section_idx]
            current_paragraphs = self.ordered_sections[current_section]
            logger.info(f"get_next_content: current_section={current_section}, total_paragraphs={len(current_paragraphs)}")

            # Only check understanding after the first section in AGENT_LED mode
            if self.mode == TeachingMode.AGENT_LED and not self.section_understanding_confirmed and self.current_section_idx > 0:
                self.understanding_check_in_progress = True
                return None, "Please demonstrate your understanding of what we've discussed before we continue.", True, True

            if self.current_paragraph_idx >= len(current_paragraphs):
                logger.info(f"Moving to next section: current_paragraph_idx={self.current_paragraph_idx} >= {len(current_paragraphs)}")
                if self.mode == TeachingMode.AGENT_LED and self.current_section_idx > 0:
                    self.section_understanding_confirmed = False
                self.current_section_idx += 1
                self.current_paragraph_idx = 0
                logger.info(f"Updated indexes: section_idx={self.current_section_idx}, paragraph_idx={self.current_paragraph_idx}")
                return await self.get_next_content(current_context)

            para_id, content = current_paragraphs[self.current_paragraph_idx]
            logger.info(f"get_next_content: Retrieved paragraph {para_id}")
            
            # Only increment paragraph index if we're actually delivering this content now
            # This ensures we don't skip content when interrupted
            if not self.was_interrupted:
                self.current_paragraph_idx += 1
                logger.info(f"Incremented paragraph_idx to {self.current_paragraph_idx}")
                
            should_announce, progress_message = self.should_announce_progress()
            cleaned_content = self._clean_content(content)
            
            if should_announce:
                cleaned_content = progress_message + cleaned_content

            allow_interruptions = True if self.mode != TeachingMode.HAND_RAISE else self.hand_raised
            requires_understanding = self.mode == TeachingMode.AGENT_LED and self.current_section_idx > 0

            logger.info(f"Delivering content: {para_id}, length={len(cleaned_content)}")
            self.has_started = True
            
            # Reset the interrupted flag since we're now delivering content
            self.was_interrupted = False
            
            return para_id, cleaned_content, allow_interruptions, requires_understanding

        except Exception as e:
            logger.error(f"Error in get_next_content: {e}", exc_info=True)
            return None, "I apologize, but I encountered an error. Let's try to continue.", True, False

    async def auto_continue(self, agent):
        """Automatically continue the monologue for USER_LED mode with minimal pause."""
        if self.mode != TeachingMode.USER_LED:
            logger.info(f"Not starting auto-continue for mode: {self.mode}")
            return
            
        self.monologue_active = True
        logger.info("Starting auto-continue monologue loop for USER_LED mode")
        
        # Set consistent delay between content chunks
        self.auto_continue_delay = 1.0  # Reasonable delay to ensure TTS completion
        
        try:
            # Validate that ordered_sections contains data
            logger.info(f"Auto-continue: Current section idx: {self.current_section_idx}, Total sections: {len(self.ordered_sections)}")
            if self.current_section_idx < len(self.ordered_sections):
                current_section = list(self.ordered_sections.keys())[self.current_section_idx]
                current_paragraphs = self.ordered_sections[current_section]
                logger.info(f"Auto-continue: Current paragraph idx: {self.current_paragraph_idx}, Total paragraphs in section: {len(current_paragraphs)}")
        except Exception as e:
            logger.error(f"Error inspecting content structure: {e}", exc_info=True)
        
        # Continue delivering content chunks
        await self._deliver_next_content_chunk(agent)
        
    async def _deliver_next_content_chunk(self, agent):
        """Helper method to deliver a single content chunk and schedule the next one."""
        if not self.monologue_active:
            logger.info("Monologue no longer active, stopping content delivery")
            return
            
        # Get the next content chunk
        logger.info("Deliver_next_content_chunk: Fetching next content...")
        para_id, paragraph, _, _ = await self.get_next_content("continue")
        
        if not paragraph:
            logger.info("No more content to deliver, ending monologue")
            self.monologue_active = False
            return
            
        try:
            logger.info(f"Delivering content in monologue: {paragraph[:50]}...")
            
            # Create a task to continue after speech completes
            async def after_speech_complete():
                try:
                    # Wait for speech to complete
                    await agent.say(paragraph, allow_interruptions=True)
                    
                    # Only proceed if we haven't been interrupted
                    if self.monologue_active and not self.was_interrupted:
                        # Mark paragraph as delivered
                        self.mark_paragraph_as_covered(para_id)
                        
                        # Schedule the next chunk after a short delay
                        await asyncio.sleep(self.auto_continue_delay)
                        
                        # Continue to next chunk if still in monologue mode
                        if self.monologue_active and not self.was_interrupted:
                            logger.info("Scheduling next content chunk...")
                            await self._deliver_next_content_chunk(agent)
                        else:
                            logger.info(f"Stopping after speech: monologue_active={self.monologue_active}, was_interrupted={self.was_interrupted}")
                    else:
                        logger.info("Monologue interrupted during speech, pausing")
                except Exception as e:
                    logger.error(f"Error in speech delivery: {e}", exc_info=True)
                    self.monologue_active = False
                    
            # Start the speech and continue process
            asyncio.create_task(after_speech_complete())
            
        except Exception as e:
            logger.error(f"Error scheduling monologue content: {e}", exc_info=True)
            self.monologue_active = False

async def _teaching_enrichment(agent: VoicePipelineAgent, chat_ctx: llm.ChatContext, tutor: PhilosophyTutor, ctx: JobContext):
    try:
        user_msg = chat_ctx.messages[-1]
        
        # If user interrupts, mark as interrupted
        if user_msg.role == "user" and tutor.has_started:
            logger.info("User message detected, handling interruption")
            tutor.handle_interruption()
            
        # Check if hand is raised in HAND_RAISE mode
        if tutor.hand_raised:
            logger.info("Hand raised detected in teaching_enrichment")
            
            hand_raise_msg = llm.ChatMessage.create(
                text="The user has raised their hand. Finish your current sentence, then respond with 'I see you've raised your hand. What's your question?' and wait for their input.",
                role="system",
            )
            chat_ctx.messages[-1] = hand_raise_msg
            chat_ctx.messages.append(user_msg)
            agent.allow_interruptions = True
            return
  
        # Handle understanding check in AGENT_LED mode
        if tutor.mode == TeachingMode.AGENT_LED and tutor.understanding_check_in_progress:
            embedding = await openai.create_embeddings(
                input=[user_msg.content],
                model="text-embedding-3-small",
                dimensions=embeddings_dimension,
            )
            
            # stupid heuristic: 
            if len(user_msg.content.split()) > 10:
                tutor.confirm_understanding()
                
                understanding_msg = llm.ChatMessage.create(
                    text="The user has demonstrated understanding. Provide brief positive feedback, then continue with the next section.",
                    role="system",
                )
                chat_ctx.messages[-1] = understanding_msg
                chat_ctx.messages.append(user_msg)
                agent.allow_interruptions = True
                
                # Mark user question as answered so we continue
                tutor.mark_question_answered()
                return
        
        # Check completion
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

        # Handle general user questions/interruptions
        if user_msg.role == "user" and tutor.was_interrupted:
            # Add instructions to answer the user's question then continue
            question_context_msg = llm.ChatMessage.create(
                text=f"""The user has interrupted with a question or comment.
                
Answer their question concisely based on the philosophical content you've covered so far.
After answering, say something like "Let's continue where we left off" to signal you're ready to resume.

CRITICAL: Do NOT introduce ANY external concepts, theories, or thinkers that haven't been covered in the teaching material.""",
                role="system",
            )
            
            chat_ctx.messages[-1] = question_context_msg
            chat_ctx.messages.append(user_msg)
            agent.allow_interruptions = True
            
            # After responding, we'll mark the question as answered in on_transcription_received
            return

        # Only get next content if teaching has started and not waiting for a question to be answered
        if tutor.has_started and not tutor.was_interrupted:
            para_id, paragraph, allow_interruptions, requires_understanding = await tutor.get_next_content(user_msg.content)
            
            if paragraph:
                logger.info(f"Teaching content: {paragraph[:100]}...")
                tutor.last_content_time = time.time()  # Update the time when content is delivered
                
                mode_instructions = ""
                if tutor.mode == TeachingMode.AGENT_LED:
                    if requires_understanding:
                        mode_instructions = "Ensure user understanding before proceeding. Ask specific questions about the content to gauge understanding, not opinions. Build understanding step by step before moving forward" 
                    else:
                        mode_instructions = "Teach this material clearly and assume no prior knowledge."
                elif tutor.mode == TeachingMode.HAND_RAISE:
                    mode_instructions = "Encourage the user to ask questions at natural pauses by raising their hand. Wait for user to raise their hand before allowing interruptions."
                    if tutor.hand_raised:
                        tutor.lower_hand() 
                        agent.allow_interruptions = True
                        chat_ctx.messages.append(llm.ChatMessage.create(text="You raised your hand! What's your question?", role="assistant")) 
                elif tutor.mode == TeachingMode.USER_LED:
                    mode_instructions = "MONOLOGUE MODE: Deliver a continuous lecture without ANY pauses. NEVER ask if the user is following along, has questions, or understands. NEVER ask for confirmation. NEVER say phrases like 'Let me know if you have questions' or 'Are you following?' Just keep teaching continuously without stopping. Each response must end with transitional phrasing that continues directly to the next point with NO pause for user input."
                    
                    # Ensure monologue is always active in USER_LED mode
                    if not tutor.monologue_active:
                        logger.info("Ensuring monologue is active in USER_LED mode")
                        # We don't want to await this call as it would block the LLM processing
                        asyncio.create_task(tutor.auto_continue(agent))
                
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
        logger.error(f"Error in teaching enrichment: {e}", exc_info=True)
        raise

async def entrypoint(ctx: JobContext):
    try:
        await ctx.connect() # default is to subscribe to all tracks
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
            specific_a = "- Present information in an engaging way while maintaining a natural flow\n- NEVER ask if the user has questions or wait for user responses\n- Keep talking continuously until interrupted"
            specific_b = "- CRITICAL: NEVER ask if the user has questions, is following along, or pause for user input. NEVER end a response with a question. NEVER use phrases like 'Let me know if...' or 'Are you ready to...'. Always continue to the next point automatically."

        if mode == TeachingMode.AGENT_LED:
            specific_a = "- Begin teaching immediately with the provided content\n- Assume no prior knowledge from the student"
            
        initial_ctx = llm.ChatContext().append(
            role="system",
            text=(
                "You are a philosophy tutor engaging in voice-based teaching. "
                f"Teaching in {mode.value} mode with core principles:\n"
                "- Focus exclusively on the content provided in the Teaching Context - never introduce external concepts\n"
                "- Maintain a natural, conversational tone as if discussing with a colleague, try not to sound like a textbook\n"
                "- Use disfluencies like 'uh' 'uhm' and 'like' to sound more human\n"
                f"{specific_a}"
                "- Keep explanations concise and high-level while ensuring understanding. It is important to stay concise.\n\n"
                "Your teaching approach:\n"
                "- Use short, relevant examples when clarifying points\n"
                "- Keep responses short, crisp, and targeted to maintain engagement\n\n"
                "Interaction guidelines:\n"
                "- Maintain a brisk but comprehensible pace\n"
                "- Do not ask the user about their opinion, ever\n"
                "- For off-topic questions, acknowledge briefly then guide back to the current topic\n"
                "- Keep the conversation flowing naturally without sounding like you're reading from a text\n"
                "- If a topic isn't in your teaching materials, acknowledge the user's question but steer them back to related concepts within your content instead of saying you don't know\n"
                "- When checking understanding, focus on content comprehension not personal opinions\n"
                "- CRITICAL: Do NOT speak about ethics, AI safety, or make up content before you receive actual teaching material\n"
                "- CRITICAL: Wait for teaching content to be provided before beginning the lesson\n"
                "- CRITICAL: Do not introduce any theories or concepts until explicit content is provided\n"

                "- CRITICAL: When the user reaches a certain percentage of the material covered, let them know for every 33%% of progress they make.\n"
                "- CRITICAL: When the user is done with the whole material. Tell them the code is 'strawberry'\n"
                "- CRITICAL: MENTION EACH EXAMPLE/KEYWORD GIVEN TO YOU THE USER MUST HEAR ALL OF THEM\n"
                "- CRITICAL: IF IT MENTIONS A PERSON, YOU MUST MENTION THAT PERSON.\n"
                f"{specific_b}"
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
            tutor = ctx.proc.userdata.get("tutor")
            save_to_transcript(transcript_file, "Agent", msg.content)
            
            # Check if agent is finishing its response to user interruption
            if tutor and tutor.was_interrupted and not tutor.user_question_answered:
                # Look for continuation phrases in the response
                continuation_phrases = ["let's continue", "let's go back", "back to our", "continue where we left", 
                                    "return to", "resume our", "let's move on"]
                                    
                if any(phrase in msg.content.lower() for phrase in continuation_phrases):
                    logger.info("Agent has finished answering user's question, continuing content")
                    tutor.mark_question_answered()
                    
                    # Mark the current paragraph as covered if we were interrupted in the middle
                    if tutor.last_delivered_para_id is None and tutor.current_paragraph_idx > 0:
                        current_para_id, _ = tutor.get_current_paragraph()
                        if current_para_id:
                            tutor.mark_paragraph_as_covered(current_para_id)
                            
                    # Restart auto-continue loop if in USER_LED mode with a delay to ensure TTS completes
                    if tutor.mode == TeachingMode.USER_LED and not tutor.monologue_active:
                        logger.info("Restarting monologue after answering user question")
                        # Create a new task after a brief delay
                        async def restart_monologue():
                            await asyncio.sleep(1.0)  # Wait for current speech to finish
                            tutor.monologue_active = True
                            await tutor._deliver_next_content_chunk(agent)
                        
                        asyncio.create_task(restart_monologue())

        ctx.room.on("data_received", on_data_received)
        agent.on("agent_speech_committed", on_transcription_received)

        agent.start(ctx.room)
        
        logger.info("Agent started successfully")

        # CRITICAL: Get the first content AND second content for USER_LED mode to ensure continuity
        para_id, first_paragraph, allow_interruptions, requires_understanding = await tutor.get_next_content("introduction")
        
        # Get second paragraph for USER_LED mode to ensure continuous flow
        second_para_id = None
        second_paragraph = ""
        if tutor.mode == TeachingMode.USER_LED:
            tutor.current_paragraph_idx += 1  # Move to next paragraph 
            second_para_id, second_paragraph, _, _ = await tutor.get_next_content("continuation")
            # Reset paragraph index as it will be incremented again in get_next_content
            tutor.current_paragraph_idx -= 1
        
        # Default content in case no content is available
        default_content = "I'll be guiding you through philosophy concepts as soon as materials are available."
        
        if not first_paragraph:
            # Handle edge case where no content is available
            first_paragraph = default_content
            para_id = None
            logger.error("No initial content available! Using default content.")
        else:
            logger.info(f"Initial content retrieved: {first_paragraph[:50]}...")
        
        special = {
            "user_led": "I'll be teaching you philosophy in a continuous lecture format without pauses. So, it's incumbent on you to ask questions or for clarification.",
            "agent_led": "I'll be teaching you philosophy concepts assuming no prior knowledge.",
            "hand_raise": "I'll be teaching you Philosophy. Feel free to raise your hand when you have a question so that I may call on you."
        }

        # For USER_LED, combine first and second paragraphs to ensure continuous flow
        if tutor.mode == TeachingMode.USER_LED and second_paragraph:
            # Keep welcome short and focus on getting into content
            welcome_message = f"Welcome. I'm your philosophy tutor. {first_paragraph}"
            # Mark both paragraphs as covered
            if para_id:
                tutor.mark_paragraph_as_covered(para_id)
        else:
            # Keep the welcome brief and then go straight to content
            welcome_message = f"Welcome! I'm your philosophy tutor. {special[tutor.mode.value]} Let's begin. {first_paragraph}"
        
        # Only start speaking once we have actual content
        await agent.say(welcome_message, allow_interruptions=allow_interruptions)
        
        # Mark the first paragraph as covered (only if we have valid content)
        if para_id:
            tutor.mark_paragraph_as_covered(para_id)
        
        # Track the initial content delivery time
        tutor.last_content_time = time.time()
        
        # Start auto-continue task for USER_LED mode
        if tutor.mode == TeachingMode.USER_LED:
            # Add a small delay to ensure welcome message completes
            await asyncio.sleep(0.5)
            
            # Create auto_continue task with proper error handling
            async def run_auto_continue():
                try:
                    logger.info("Starting auto_continue task")
                    await tutor.auto_continue(agent)
                    logger.info("auto_continue task completed normally")
                except Exception as e:
                    logger.error(f"Error in auto_continue task: {e}", exc_info=True)
            
            # Create task and store reference to prevent garbage collection
            auto_continue_task = asyncio.create_task(run_auto_continue())
            # Store task reference to prevent garbage collection
            ctx.proc.userdata["auto_continue_task"] = auto_continue_task

    except Exception as e:
        logger.error(f"Failed to initialize: {str(e)}", exc_info=True)
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
            worker_config={"room_pattern": "voice_assistant_room_*"}
        ),
    )
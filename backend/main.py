import os
import datetime
import logging
import pickle
import time
from enum import Enum
from typing import List, Optional, Tuple, Dict
import re


from livekit.agents import AutoSubscribe, JobContext, JobProcess, WorkerOptions, cli, llm, stt, transcription
from livekit.agents.pipeline import VoicePipelineAgent
from livekit.plugins import deepgram, openai, rag, silero, turn_detector
from livekit.rtc.room import DataPacket
from openai import OpenAI

import asyncio

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("philosophy-tutor")

async def entrypoint(ctx: JobContext):
    try:
        await ctx.connect() # default is to subscribe to all tracks
        logger.info("Connected to room")

        room_name = ctx.room.name

        #if "tutor" not in ctx.proc.userdata: # to avoid cacheing?
        ctx.proc.userdata["tutor"] = PhilosophyTutor(get_mode_from_roomname(room_name), ctx)
        tutor = ctx.proc.userdata["tutor"]
        logger.info(f"Using mode from room name: {tutor.mode.value}")

        transcript_file = get_transcript_path(room_name)
        initialize_transcript(transcript_file, room_name, tutor.mode.value)
        logger.info(f"Transcript will be saved to: {transcript_file}")


        mode_specific_default = ""
        mode_specific_critical = ""
        if tutor.mode == TeachingMode.USER_LED:
            mode_specific_default = "- Present information in an engaging way while maintaining a natural flow\n- NEVER ask if the user has questions or wait for user responses\n- Keep talking continuously until interrupted"
            mode_specific_critical = "- CRITICAL: NEVER ask if the user has questions, is following along, or pause for user input. NEVER end a response with a question. NEVER use phrases like 'Let me know if...' or 'Are you ready to...'. Always continue to the next point automatically."
        elif tutor.mode == TeachingMode.AGENT_LED:
            # avoid mention of prior knowledge, [prompting] is a bad way to solve this
            mode_specific_default = "- Begin teaching immediately with the provided content\n- Assume no prior knowledge from the student"

        initial_ctx = llm.ChatContext().append(
            role="system",
            text=(
                "You are a philosophy tutor engaging in voice-based teaching. "
                f"Teaching in {tutor.mode.value} mode with core principles:\n"
                "- Focus exclusively on the content provided in the Teaching Context - never introduce external concepts\n"
                "- Maintain a natural, conversational tone as if discussing with a colleague, try not to sound like a textbook\n"
                "- Use disfluencies like 'uh' 'uhm' and 'like' to sound more human\n"
                f"{mode_specific_default}"
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
                f"{mode_specific_critical}"
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
        # agent.on talked, prompt a continue el o el for user led mode??

        agent.start(ctx.room)
        logger.info("Agent started successfully")

        cases = {
            "user_led": "I'll be teaching you philosophy in a continuous lecture format without pauses. So, it's incumbent on you to ask questions or for clarification.",
            "agent_led": "I'll be teaching you philosophy concepts assuming no prior knowledge.",
            "hand_raise": "I'll be teaching you philosophy. Feel free to raise your hand when you have a question so that I may call on you."
        }


        welcome_message = f"Welcome! I'm your philosophy tutor. {cases[tutor.mode.value]} Let's begin."
        await agent.say(welcome_message, allow_interruptions=True)

        # Start first section immediately
        if tutor.current_section < len(tutor.sections):
            first_section = f"Let's start with our first topic. {tutor.sections[tutor.current_section]}"
            await agent.say(first_section, allow_interruptions=True)
            
            # For USER_LED mode, set up auto-continuation
            if tutor.mode == TeachingMode.USER_LED:
                asyncio.create_task(tutor.continue_teaching(agent))

    
        
    except Exception as e:
        logger.error(f"Failed to initialize: {str(e)}", exc_info=True)
        raise



class TeachingMode(Enum):
    USER_LED = "user_led"
    AGENT_LED = "agent_led"
    HAND_RAISE = "hand_raise"

class PhilosophyTutor:
    def __init__(self, mode: TeachingMode, ctx):
        self.ctx = ctx
        self.mode = mode

        self.current_section = 0
        self.sections = list(extract_markdown_sections(open("summary.md", "r", encoding="utf-8").read()).values()) # holy moly
        self.hand_raised = False

        self.pending_check = False
        
        logger.info(f"Initialized tutor with {len(self.sections)} total sections in {mode.value} mode")
    
    def raise_hand(self):
        self.hand_raised = True
        logger.info("Hand raised")

    def lower_hand(self):
        self.hand_raised = False
        logger.info("Hand lowered")

    async def continue_teaching(self, agent):
        if self.mode == TeachingMode.USER_LED and self.current_section < len(self.sections):
            try:
                logger.info(f"continuing teaching {self.current_section}")
                # small delay to make it feel more natural
                await asyncio.sleep(0.5)
                await agent.say(self.sections[self.current_section], allow_interruptions=True)
                self.current_section += 1
                
                # schedule if exists
                if self.current_section < len(self.sections):
                    current_progress = (self.current_section / len(self.sections)) * 100
                    if current_progress > 0 and current_progress % 33 < 33 / len(self.sections):
                        progress_message = f"We've covered about {int(current_progress)}% of the material. "
                        await agent.say(progress_message, allow_interruptions=True)
                    
                    # do schedule
                    asyncio.create_task(self.continue_teaching(agent))
                elif self.current_section >= len(self.sections):
                    await agent.say("Congratulations on completing all the material! Your code is strawberry.", allow_interruptions=True)
                    asyncio.create_task(self.ctx.room.local_participant.publish_data(
                        payload="strawberry",
                        reliable=True,
                        topic="command"
                    ))
            except Exception as e:
                logger.error(f"Error in continue_teaching: {e}", exc_info=True)

async def _teaching_enrichment(agent: VoicePipelineAgent, chat_ctx: llm.ChatContext, tutor: PhilosophyTutor, ctx: JobContext):
    try:
        user_msg = chat_ctx.messages[-1]
        if user_msg.role == "user":# and tutor.has_started:
            logger.info("User message detected, handling interruption")
            if tutor.pending_check:

                response_relevance = evaluate_response_relevance(user_msg.text, tutor.sections[tutor.current_section])
                if response_relevance:
                    logger.info("User response is relevant, advancing to next section")
                    tutor.pending_check = False
                    tutor.current_section += 1
                    if tutor.current_section < len(tutor.sections):
                        if tutor.mode != TeachingMode.USER_LED:
                            # For AGENT_LED and HAND_RAISE, schedule next section after response
                            asyncio.create_task(schedule_next_section(agent, tutor))
                        else:
                            # For USER_LED, continue automatically
                            asyncio.create_task(tutor.continue_teaching(agent))
                    elif tutor.current_section >= len(tutor.sections):
                        await agent.say("Congratulations on completing all the material! Your code is strawberry.", allow_interruptions=True)
                        asyncio.create_task(ctx.room.local_participant.publish_data(
                            payload="strawberry",
                            reliable=True,
                            topic="command"
                        ))
                else:
                    # Response wasn't relevant, prompt again or provide feedback
                    if tutor.mode != TeachingMode.USER_LED:
                        # Keep pending_check true and ask again for AGENT_LED and HAND_RAISE
                        follow_up_msg = llm.ChatMessage.create(
                            text="The user's response wasn't directly relevant to the material. Gently guide them back to the key concepts and ask again what they found most important.",
                            role="system",
                        )
                        chat_ctx.messages.append(follow_up_msg)
                

        # Check for hand raise
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


        if tutor.current_section == len(tutor.sections):
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
        if tutor.current_section == len(tutor.sections) // 2:
            await agent.say("You're halfway through the material! Keep up the great work.")
            logger.info("(User) is halfway done the material.")

        if tutor.current_section == len(tutor.sections) // 3:
            await agent.say("You're a third of the way through the material! Keep up the great work.")
            logger.info("(User) is 1/3rd done the material.")


        # pending_check should be marked true each section, if the user can answer whats the most important thing they've
        # learned AND THE RESPONSE IS RELEVANT (in AGENT_LED or HAND_RAISE), we can continue to the next section.
        # in USER_LED, just continue if the user hasn't said anything by the end of the spiel or if the user said something, make sure the 
        # agent has resposnded and the user doesnt have any more to say

        if tutor.sections < len(tutor.sections):
            # Set appropriate instructions based on mode
            if tutor.mode == TeachingMode.AGENT_LED or tutor.mode == TeachingMode.HAND_RAISE:
                tutor.pending_check = True
                instructions = "After explaining this section, ask: 'What is the most important thing you've learned so far?'"
            elif tutor.mode == TeachingMode.USER_LED:
                tutor.pending_check = False
            
            context_msg = llm.ChatMessage.create(
                    text=f"""Teaching Context:
Content: {tutor.sections[tutor.current_section]}

STRICT RULES:
1. ONLY teach what's explicitly contained in the above content
2. You must teach every concept, theory and factoid mentioned
3. Do NOT introduce ANY external concepts, theories, or thinkers

Instructions: Use ONLY the above content to respond. {instructions}
Avoid external knowledge. For off-topic questions, redirect to related material topics.""",
                    role="system",
                )
            chat_ctx.messages[-1] = context_msg
            chat_ctx.messages.append(user_msg)
            #agent.allow_interruptions = allow_interruptions
            
        

    except Exception as e:
        logger.error(f"Error in teaching enrichment: {e}", exc_info=True)
        raise


def evaluate_response_relevance(user_response, current_section_content):
    # FIXME: LOL
    # "demonstrates awareness of their own knowledge" is the language used in that paepr [sic]
    if len(user_response.split()) > 10:
        return True
        
    return False

async def schedule_next_section(agent, tutor):
    try:
        # Wait a brief moment before continuing to next section
        await asyncio.sleep(0.2)
        if tutor.current_section < len(tutor.sections):
            next_section = tutor.sections[tutor.current_section]
            # Continue teaching automatically without waiting for user input
            await agent.say(f"Moving on to our next topic. {next_section}", allow_interruptions=True)
    except Exception as e:
        logger.error(f"Error scheduling next section: {e}", exc_info=True)

def get_mode_from_roomname(name: str):
    if "SQUARE" in name:
        return TeachingMode.USER_LED
    elif "CIRCLE" in name:
        return TeachingMode.AGENT_LED
    elif "TRIANGLE" in name:
        return TeachingMode.HAND_RAISE
    else:
        return Exception(f"Invalid room name: {name}")


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
    
def extract_markdown_sections(markdown_text):
    heading_lines = re.findall(r'^(#{1,6}\s+.+?)$', markdown_text, re.MULTILINE)
    if not heading_lines:
        return "No headings found"
    heading_levels = [re.match(r'^(#+)', line).group(1) for line in heading_lines] # get levels
    level_counts = {}
    for level in heading_levels:
        level_counts[level] = level_counts.get(level, 0) + 1
    sorted_levels = sorted(level_counts.items(), key=lambda x: x[1], reverse=True)
    target_level = sorted_levels[0][0]
    pattern = rf'^({target_level}\s+(.+?))\n([\s\S]*?)(?=^{target_level}|\Z)'
    sections = {}
    matches = re.finditer(pattern, markdown_text, re.MULTILINE)
    for match in matches:
        heading_text = match.group(2).strip()
        content = match.group(3).strip()
        sections[heading_text] = content
    return sections

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
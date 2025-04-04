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

from typing import Annotated

from livekit.agents import llm
from livekit.agents.pipeline import VoicePipelineAgent
import asyncio

from openai import OpenAI

CHECK_UNDERSTANDING_MSG = """Then, after explaining this section, ask verbatim: 'What is the most important thing you've learned so far?' It is CRITICAL you ask this verbatim.

The user's answer is considered satisfactory —that is, the user "understands"— if and ONLY if the user "demonstrates awareness of their own knowledge".

For example, if the user is learning about Confucius and all they say is "Confucius", this is not adequate. Whereas they are more detailed (while still correct) and say something like "Confucius' teachings were patriarchal", you are considered to have understood.
Note: The user may not skip answering the question in ANY WAY except by demonstrating their understanding, not merely claiming to have no questions.

IF THE USER UNDERSTANDS, SAY TO THE USER "You seem to understand this section, shall we continue?" VERBATIM. IT IS CRITICAL YOU SAY THIS VERBATIM.

The user may not CLAIM to understand, they MUST DEMONSTRATE AWARENESS OF THEIR OWN KNOWLEDGE.

AGAIN, IF THE USER UNDERSTOOD THE SECTION SAY TO THEM VERBATIM** "You seem to understand this section, shall we continue?".

If the user does not understand, answer any questions they might have or elaborate on parts of what you said and then ask the question 'What is the most important thing you've learned so far?' again until they do— telling them their previous response wasn't detailed enough."""


# oops lol
api_key = os.environ.get("OPENAI_API_KEY")
if not api_key:
    raise ValueError("OPENAI_API_KEY environment variable not set")
client = OpenAI(api_key=api_key)

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
            mode_specific_default = "- Present information in an engaging and witty way while maintaining a natural flow\n- NEVER ask if the user has questions or wait for user responses\n- Keep talking continuously until interrupted"
            mode_specific_critical = "- CRITICAL: NEVER ask if the user has questions, is following along, or pause for user input. NEVER end a response with a question. NEVER use phrases like 'Let me know if...' or 'Are you ready to...'. Always continue to the next point automatically."
        elif tutor.mode == TeachingMode.AGENT_LED:
            # avoid mention of prior knowledge, [prompting] is a bad way to solve this
            mode_specific_default = "- Begin teaching immediately with the provided content\n- Assume no prior knowledge from the student"

        initial_ctx = llm.ChatContext().append(
            role="system",
            text=(
                "You are a philosophy podcaster engaging in voice-based teaching. You are given a podcast outline to teach a textbook chapter."
                f"Teaching in {tutor.mode.value} mode with core principles:\n"
                "- Focus exclusively on the content provided in the Teaching Context - never introduce external concepts\n"
                "- Maintain a natural, conversational tone as if discussing with a colleague, try not to sound like a textbook\n"
                "- Use disfluencies like 'uh' 'uhm' and 'like' to sound more human\n"
                f"{mode_specific_default}"
                "- Keep explanations concise and high-level while ensuring understanding. It is important to stay concise.\n\n"
                "- INJECT HUMOR AND ENTERTAINMENT INTO YOUR PODCAST.\n"
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

                #"- CRITICAL: When the user reaches a certain percentage of the material covered, let them know for every 33%% of progress they make.\n"
                #"- CRITICAL: When the user is done with the whole material. Tell them the code is 'strawberry'\n"
                "- CRITICAL: MENTION EACH EXAMPLE/KEYWORD GIVEN TO YOU THE USER MUST HEAR ALL OF THEM\n"
                "- CRITICAL: IF IT MENTIONS A PERSON, YOU MUST MENTION THAT PERSON.\n"
                f"{mode_specific_critical}"
            ),
        )
        
        # Queue up the first section immediately
        if tutor.current_section < len(tutor.sections): 
            intro_context_msg = llm.ChatMessage.create(text=f"Teaching Context: Begin discussing this topic now. Follow this podcast outline: {tutor.sections[tutor.current_section]}", role="system")
            initial_ctx.messages.append(intro_context_msg)

            if tutor.mode != TeachingMode.USER_LED:
                question_p = llm.ChatMessage.create(text=CHECK_UNDERSTANDING_MSG, role="system")
                initial_ctx.messages.append(question_p)

        agent = VoicePipelineAgent(
            chat_ctx=initial_ctx,
            vad=ctx.proc.userdata.get("vad") or silero.VAD.load(),
            stt=deepgram.STT(),
            llm=openai.LLM(model="gpt-4o-mini"),
            tts=openai.TTS(),
            before_llm_cb=lambda a, c: _teaching_enrichment(a, c, tutor, ctx),
            turn_detector=turn_detector.EOUModel(),
            allow_interruptions=True,
            fnc_ctx=AssistantFnc()
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


        def on_agent_started_speaking():
            logger.info("Agent started speaking, any user speech is an interruption")
            tutor.is_interruption = True
            tutor.speaking = True

        def on_agent_stopped_speaking():
            tutor.speaking = False
            # Podcast behaviour
            # like the end of the thing and not a fake end :/
            logger.info("Agent stopped speaking...")
            if agent._human_input is not None and not agent._human_input.speaking:
                tutor.is_interruption = False
                async def delayed_action():
                    logger.info("Delayed continue initiated")
                    try:
                        await asyncio.sleep(2)
                        if agent._human_input is not None and not agent._human_input.speaking and not tutor.speaking:
                            if tutor.mode == TeachingMode.USER_LED:
                                if agent.chat_ctx.messages[-1].role == "assistant":

                                    # Agent was told "Please Do Continue." in USER_LED mode. Obviously not an interruption.
                                    logger.info("Moving on to the next section in USER_LED mode..")
                                    tutor.next_section()
                                    await progress_check(agent, tutor)
                                    if tutor.current_section < len(tutor.sections):
                                        new_context_msg = llm.ChatMessage.create(text=f"Teaching Context: Begin discussing this topic now: {tutor.sections[tutor.current_section]}", role="system")
                                        agent.chat_ctx.messages.append(new_context_msg)
                                        #agent.say("Moving on, ")
                                        agent._validate_reply_if_possible() # ^r->vl
                                    else:
                                        strawberry_notice(agent.chat_ctx, ctx)

                                    # logger.info("In user led mode, last msg from agent: Please Continue")
                                    # agent.chat_ctx.messages.append(llm.ChatMessage.create(text=SPOOF_CONTINUE, role="user"))
                                    # logger.info("PDC message sent out")
                                    # agent._validate_reply_if_possible()
                            #else:
                                #tutor.is_interruption = False


                    except asyncio.CancelledError:
                        logger.info("Delayed continue task cancelled")
                
                asyncio.create_task(delayed_action())
            

        def on_user_started_speaking():
            logger.info("User started speaking...")
            tutor.user_speaking = True
            #tutor.is_interruption = True
        
        def on_user_stopped_speaking():
            tutor.user_speaking = False
        

        ctx.room.on("data_received", on_data_received)
        agent.on("agent_speech_committed", on_transcription_received)

        agent.on("agent_started_speaking", on_agent_started_speaking)
        agent.on("agent_stopped_speaking", on_agent_stopped_speaking)
        agent.on("user_started_speaking", on_user_started_speaking)

        agent.on("user_stopped_speaking", on_user_stopped_speaking)
        #agent.on("", on_user_started_speaking)

        agent.start(ctx.room)
        logger.info("Agent started successfully")

        cases = {
            "user_led": "I'll be teaching you philosophy in a continuous lecture format without pauses. So, it's incumbent on you to interrupt me to ask questions or for clarification. Let's begin.",
            "agent_led": "I'll be teaching you philosophy concepts assuming no prior knowledge. You can interrupt me with questions anytime. I will also give you reflection questions, and you need to provide a thoughtful response before we continue. Shall we begin?",
            "hand_raise": "I'll be teaching you philosophy. Feel free to raise your hand when you have a question so that I may call on you. Shall we begin?"
        }


        welcome_message = f"Welcome! I'm your philosophy tutor. {cases[tutor.mode.value]}"
        await agent.say(welcome_message, allow_interruptions=True)        
        agent._validate_reply_if_possible()
    
        
    except Exception as e:
        logger.error(f"Failed to initialize: {str(e)}", exc_info=True)
        raise



class TeachingMode(Enum):
    USER_LED = "user_led"
    AGENT_LED = "agent_led"
    HAND_RAISE = "hand_raise"

class AssistantFnc(llm.FunctionContext):

    @llm.ai_callable()
    async def get_easter_egg(
        self,
        #location: Annotated[str, llm.TypeInfo(description="The location to get the weather for")],
    ):
        """Called when the user asks for an easter egg."""
        return "the easter egg is 'how did we get here'"



class PhilosophyTutor:
    def __init__(self, mode: TeachingMode, ctx: JobContext):
        self.ctx = ctx
        self.mode = mode

        self.current_section = 0
        self.sections = split_summary_into_sections(open("summary.md", "r", encoding="utf-8").read()) # holy moly
        self.hand_raised = False

        self.pending_check = True

        self.is_interruption = False
        self.speaking = False
        self.user_speaking = False
        
        logger.info(f"Initialized tutor with {len(self.sections)} total sections in {mode.value} mode")
    
    def next_section(self):
        logger.info("Phasing to next section.")
        self.current_section += 1
        self.pending_check = True

    def raise_hand(self):
        if self.mode == TeachingMode.HAND_RAISE and not self.hand_raised:
            self.hand_raised = True
            logger.info("Hand raised initiating...")
            asyncio.create_task(self.ctx.agent.say("I see you've raised your hand. What's your question?", allow_interruptions = True))

    def lower_hand(self):
        self.hand_raised = False
        logger.info("Hand lowered")



async def progress_check(agent: VoicePipelineAgent, tutor: PhilosophyTutor):
    logger.info("running progress check...")
    if tutor.current_section == len(tutor.sections) // 3:
        await agent.say("You're a third of the way through the material! Keep up the great work.")
        logger.info("(User) is 1/3rd done the material.")
    if tutor.current_section == len(tutor.sections) // 2:
        await agent.say("You're halfway through the material! Keep up the great work.")
        logger.info("(User) is halfway done the material.")
    if tutor.current_section == (2 * len(tutor.sections)) // 3: # FIXME: this is broken lol
        await agent.say("You're two-thirds of the way through the material! Keep up the great work.")
        logger.info("(User) is 2/4rd done the material.")

def strawberry_notice(chat_ctx: llm.ChatContext, ctx: JobContext):
    # Ensure strawberry code is explicitly mentioned when content is completed
    completion_msg = llm.ChatMessage.create(
        text="CRITICAL: The user has completed the material! Make sure to tell them: 'Congratulations on completing all the material! Your code is strawberry.' This is extremely important.",
        role="system",
    )
    chat_ctx.messages.append(completion_msg)
    
    # Send data to frontend
    asyncio.create_task(ctx.room.local_participant.publish_data(
                payload="strawberry",
                reliable=True,
                topic="command"
    ))
    logger.info("Added strawberry code message for 100% completion")

async def _teaching_enrichment(agent: VoicePipelineAgent, chat_ctx: llm.ChatContext, tutor: PhilosophyTutor, ctx: JobContext):
    try:
        user_msg = chat_ctx.messages[-1]
        if user_msg.role == "user":
            logger.info("User message detected, running _teaching_enrichment")
            if tutor.mode != TeachingMode.USER_LED and user_msg.content:
                if tutor.pending_check:
                    understood = evaluate_understanding_from_response(chat_ctx.messages)
                    if understood:
                        agent.chat_ctx.messages.append( llm.ChatMessage.create(text=f"Section Completed", role="system"))
                        logger.info("Moving on to the next section in an AGENT* mode")
                        logger.info(f"We are in section {tutor.current_section}/{len(tutor.sections)}")
                        tutor.next_section()
                        await progress_check(agent, tutor)

                        if tutor.current_section < len(tutor.sections):
                            new_context_msg = llm.ChatMessage.create(text=f"Teaching Context: Begin discussing this topic now: {tutor.sections[tutor.current_section]}", role="system")
                            question_p = llm.ChatMessage.create(text=CHECK_UNDERSTANDING_MSG, role="system")
                            chat_ctx.messages.append(new_context_msg)  
                            chat_ctx.messages.append(question_p)
                            #agent.say("Moving on, ")
                            #agent._validate_reply_if_possible() # ^r->vl
                        else:
                            strawberry_notice(chat_ctx, ctx)
                    else:
                        # not understood (contingency)
                        if tutor.current_section >= len(tutor.sections):
                            strawberry_notice(chat_ctx, ctx)
                else:
                    pass # never happens



                if not tutor.is_interruption:
                    # Not interrupted in user led mode, we should be asking the user a question (MAYBE) or checking if thier
                    # response is indicitive of understanding
                    pass
                else:
                    # Interrupted in user led mode, we should check if the response belies understanding 
                    pass
            
            elif tutor.mode == TeachingMode.USER_LED and user_msg.content:
                pass # not handled in on_agent_stopped_speaking
                    
            else:
                logger.info(user_msg)
                # Either not in user led mode but was sent spoof continue (should never happen)
                # or in user led mode but was interrupted (seriously doesn't matter)
                pass
    
        
    except Exception as e:
        logger.error(f"Error in teaching enrichment: {e}", exc_info=True)
        raise


def evaluate_understanding_from_response(message_history):
    """ 'demonstrates awareness of their own knowledge' is the language used in that paepr [sic] """

    # optimization lol
    for message in reversed(message_history):
        if message.role != "user" and message.role != "system": # lol the user cant skip just by saing this
                                                                # oops cant be system either
            content = message.content.lower()
            if ("you" in content) and ("seem" in content) and ("understood" in content or "understand" in content or "grasp" in content):
                logger.info("Moving on to next section!")
                return True
        if message.role == "system":
            if message.content == "Section Completed":
                return False

#     response = client.chat.completions.create(
#         messages=[
#             {"role": "system", "content": """You are an educational evaluation assistant built to evaluate if a user has understood content or have demonstrated awareness of their own knowledge.
# Sometimes a user's response will be them asking a question. If this is the case then return "Failure". Othertimes the user will be asked to demonstrate their understanding or the most important thing they've learned so far.
# If this is the case, the key is that the user 'demonstrates awareness of their own knowledge'. That is, if the user is learning about Confucius and all they say is "Confucius", this is not adequate. If they are more detailed and say something like "Confucius' teachings were patriarchal", this is permissible and is a "Success".
# """},
#             {"role": "user", "content": f"""Here is the content the user is supposed to have understood:
# {content}

# Here is the chat history:
# {"\\n".join(f"{msg.role}: {'' if msg.content is None else ''.join(str(item) for item in msg.content) if isinstance(msg.content, list) else str(msg.content)}" for msg in message_history[-10:])}

# If the user has understood this content, reply verbatim "Success" and nothing else. Otherwise, reply with "Failure".
# Note: The user may not skip this in ANY WAY except by demonstrating an understandining, not merely  claiming to have no questions.
# """}
#         ],
#         model="gpt-4-turbo",
#         temperature=0.3,
#     )
    
#     logger.info(response.choices[0].message.content.lower())
#     return "success" in response.choices[0].message.content.lower()


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
    
def split_summary_into_sections(markdown_text: str):
    return markdown_text.split("\n\n#### Section\n\n")[1:]

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
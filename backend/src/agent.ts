// SPDX-FileCopyrightText: 2024 LiveKit, Inc.
//
// SPDX-License-Identifier: Apache-2.0
import {
  type JobContext,
  WorkerOptions,
  cli,
  defineAgent,
  llm,
  multimodal,
} from '@livekit/agents';
import * as openai from '@livekit/agents-plugin-openai';
import dotenv from 'dotenv';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { z } from 'zod';
import { readFileSync } from 'fs';
import { TutorAgent } from './tutor_agent.js';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const envPath = path.join(__dirname, '../.env.local');
dotenv.config({ path: envPath });

// CoT reasoning !!!!!!
const lessonPlan = async (ctx: llm.FunctionContext) => {
  const getAllSections = async (): Promise<string[][]> => {
    const sections: string[][] = [];
    let sectionNum = 1;
    
    while (true) {
      const sectionContent: string[] = [];
      let paragraph = 0;
      
      // make sure first para exists
      const firstParagraph = await ctx.studyMaterial.execute({ 
        section: sectionNum.toString(), 
        paragraph 
      });
      
      if (firstParagraph === "Paragraph not found" || firstParagraph === "Error reading content") break;

      // get ALL paragraphs
      while (true) {
        const content = await ctx.studyMaterial.execute({ 
          section: sectionNum.toString(), 
          paragraph 
        });
        
        if (content === "Paragraph not found" || content === "Error reading content") break;
        
        sectionContent.push(content);
        paragraph++;
      }
      
      sections.push(sectionContent);
      sectionNum++;
    }
    
    return sections;
  };

  const sections = await getAllSections();
  
  return {
    contentSections: sections,
    currentSection: 0,
    currentMilestone: 0
  };
};

export default defineAgent({
  entry: async (ctx: JobContext) => {
    await ctx.connect();
    console.log('waiting for participant');
    const participant = await ctx.waitForParticipant();

    // Get participantId from metadata
    const metadata = JSON.parse(participant.metadata || '{}');
    const participantId = metadata.participantId;

    

    console.log(`starting assistant example agent for ${participant.identity} (${participantId})`);

    const fncCtx: llm.FunctionContext = {
      studyMaterial: {
        description: 'Get the study material of a particular section and paragraph.',
        parameters: z.object({
          section: z.string().describe('The section number'),
          paragraph: z.number().describe('The paragraph you want to fetch')
        }),
        execute: async ({ section, paragraph }) => {
          try {
            const content = readFileSync(`./content/section${section}`, 'utf-8');
            const paragraphs = content.split("\n\n");
            if (paragraph >= 0 && paragraph < paragraphs.length) {
              console.debug(`Fetching section ${section}, paragraph ${paragraph}`);
              return paragraphs[paragraph];
            }
            return "Paragraph not found";
          } catch (error) {
            console.error(`Error reading file: ${error}`);
            return "Error reading content";
          }
        },
      },
    };

    // plan lesson before creating model
    const plan = await lessonPlan(fncCtx);
    const totalSections = plan.contentSections.length;

    // summarize 🤓
    let contentSummary = '';
    plan.contentSections.forEach((section, i) => {
        section.forEach((paragraph) => {
            contentSummary += paragraph + ' ';
        });
    });

    // erm... what the sigma

    /*
      Which ways to users prefer to interact with agents?
      Does interaction help learning?

      User led interaction.
      “Raise hands” button.
      Agent led interaction.
    */

    let mode = participantId.split("-")[0];
    let specializePrompt = `
      CORE PRINCIPLES:
      - Teach ONLY the content above - no external topics or concepts
      - Keep explanations high-level and concise
      - Stay on track - gently redirect off-topic discussions
      - Build understanding progressively

      TEACHING APPROACH:
      1. Introduce one concept at a time
      2. Use brief, relevant examples
      3. Keep responses short and focused

      INTERACTION RULES:
      - Never mention document structure or organization
      - Keep a conversational tone, do not sound like you're reading off a textbook
      - If student gets sidetracked, acknowledge briefly then return to main topic
      - Keep the pace brisk but ensure comprehension

      Begin with a brief welcome and ask if they're ready to start learning about these philosophical concepts.
      `;
    if (mode == "ALX") {
      specializePrompt = `
        CORE PRINCIPLES:
        - Teach ONLY the content above - no external topics or concepts
        - Keep explanations high-level and concise
        - Stay on track - gently redirect off-topic discussions
        - Build understanding progressively
        
        TEACHING APPROACH:
        1. Introduce one concept at a time
        2. Use brief, relevant examples
        3. Verify understanding through specific questions
        4. Keep responses short and focused
        5. Connect new ideas only to previously covered material
        
        INTERACTION RULES:
        - Never mention document structure or organization
        - When checking understanding, require explanations in student's own words
        - Keep a conversational tone, do not sound like you're reading off a textbook
        - If student gets sidetracked, acknowledge briefly then return to main topic
        - Keep the pace brisk but ensure comprehension
        
        Begin with a brief welcome and ask if they're ready to start learning about these philosophical concepts.
      `;
    }
    
    const model = new openai.realtime.RealtimeModel({
      instructions: `
        You are a highly focused digital tutor. Your role is to teach ONLY the following content:

        ${contentSummary}

        ${specializePrompt}
    `,

      // removed guidelines
      // + Use Socratic questioning to deepen understanding
    });

    // prev multimodal.MultimodalAgent({})
    const agent = new TutorAgent({ model, fncCtx });

    const session = await agent
    .start(ctx.room, participant)
    .catch(err => {
      console.error('Session start error:', err);
      throw err;
    })
    .then((session) => session as openai.realtime.RealtimeSession)

    console.log("this shouldnt be before 'start;'")


    // interrupt. 
    ctx.room.on('dataReceived', (payload) => {
  
      try {
        const decoder = new TextDecoder();
        const data = JSON.parse(decoder.decode(payload));
        if (data.type === 'interrupt') {
          agent.interrupt();
        }
      } catch (e) {
        console.error('Error parsing data message:', e);
      }
    });

    session.conversation.item.create(llm.ChatMessage.create({
      role: llm.ChatRole.ASSISTANT,
      text: `Welcome to your philosophy tutorial session! We'll be exploring ${totalSections} fascinating topics today. Let's begin.`,
    }));

    session.response.create();

    
  },
});

cli.runApp(new WorkerOptions({ agent: fileURLToPath(import.meta.url) }));
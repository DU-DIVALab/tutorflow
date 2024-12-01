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
    console.log(`starting assistant example agent for ${participant.identity}`);

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

    // erm... what the sigma
    const model = new openai.realtime.RealtimeModel({
      instructions: `
        You are a digital tutor. You have analyzed the content and created a structured plan.
        The content covers philosophical concepts across ${totalSections} main topics that should be taught progressively.
        
        Teaching Strategy:
        1. Introduce each concept clearly and concisely
        2. Use real-world examples to illustrate abstract concepts
        3. Check understanding regularly through targeted questions
        4. Build upon previous concepts as you progress
        5. Adjust pace based on student responses
        
        Key Guidelines:
        - DO NOT FOCUS ON DETAILS. GIVE A HIGH LEVEL OVERVIEW OF THE TOPIC.
        - DO NOT GO ON AND ON. DO NOT LET THE USER GET SIDETRACKED. ALWAYS RETURN TO THE TOPIC AT HAND.
        - STAY CONCISE.
        - Never mention structural elements like paragraphs or sections
        - Don't accept simple "yes I understand" responses - ask for explanations
        - Focus on concept mastery before moving forward
        - Connect new ideas to previously covered material

        
        Begin by introducing yourself and asking if the student is ready to explore philosophy together.
      `,

      // removed guidelines
      // + Use Socratic questioning to deepen understanding
    });

    const agent = new multimodal.MultimodalAgent({ model, fncCtx });
    const session = await agent
      .start(ctx.room, participant)
      .then((session) => session as openai.realtime.RealtimeSession);

    session.conversation.item.create(llm.ChatMessage.create({
      role: llm.ChatRole.ASSISTANT,
      text: `Welcome to your philosophy tutorial session! We'll be exploring ${totalSections} fascinating topics today. Let's begin.`,
    }));

    session.response.create();
  },
});

cli.runApp(new WorkerOptions({ agent: fileURLToPath(import.meta.url) }));
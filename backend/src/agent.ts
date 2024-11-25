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


export default defineAgent({
  entry: async (ctx: JobContext) => {
    await ctx.connect();
    console.log('waiting for participant');
    const participant = await ctx.waitForParticipant();
    console.log(`starting assistant example agent for ${participant.identity}`);

    // dont mention paragraphs
    // dont believe user when they say they understand
    // dont ask user if theyre ready to continue
    // plan out lesson first, chain of thought
    const model = new openai.realtime.RealtimeModel({
      instructions: 
        `You are a digital tutor. Your job is to teach philosophy, specifically focusing on Section 1.1 and 1.2 of the Study Material. Please teach this content progressively, checking for understanding before moving forward. Start with Chapter 1 Section 1 and only proceed to Section 2 when the student shows good comprehension of Section 1 concepts.`,
    });

    const fncCtx: llm.FunctionContext = {
      studyMaterial: {
        description: 'Get the study material of a particular section and paragraph.',
        parameters: z.object({
          section: z.enum(['1', '2']).describe('The section number'),
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
      // checkUnderstanding: {
      //   description: 'Check student understanding of a concept',
      //   parameters: z.object({
      //     concept: z.string().describe('The concept to check understanding of'),
      //     section: z.enum(['1', '2']).describe('The section number'),
      //   }),
      //   execute: async ({ concept, section }) => {
      //     console.debug(`Checking understanding of ${concept} from Section ${section}`);
      //     return true;
      //   },
      // },
      // weather: {
      //   description: 'Get the weather in a location',
      //   parameters: z.object({
      //     location: z.string().describe('The location to get the weather for'),
      //   }),
      //   execute: async ({ location }) => {
      //     console.debug(`executing weather function for ${location}`);
      //     const response = await fetch(`https://wttr.in/${location}?format=%C+%t`);
      //     if (!response.ok) {
      //       throw new Error(`Weather API returned status: ${response.status}`);
      //     }
      //     const weather = await response.text();
      //     return `The weather in ${location} right now is ${weather}.`;
      //   },
      // },
    };
    const agent = new multimodal.MultimodalAgent({ model, fncCtx });
    const session = await agent
      .start(ctx.room, participant)
      .then((session) => session as openai.realtime.RealtimeSession);

    session.conversation.item.create(llm.ChatMessage.create({
      role: llm.ChatRole.ASSISTANT,
      text: `Welcome to your philosophy tutorial session! We'll be covering two sections today. Let's begin.`,
    }));

    session.response.create();
  },
});

cli.runApp(new WorkerOptions({ agent: fileURLToPath(import.meta.url) }));

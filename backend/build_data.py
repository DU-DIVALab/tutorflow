import asyncio
import pickle
import uuid
import os 

import aiohttp
from livekit.agents import tokenize
from openai import OpenAI
from livekit.plugins import openai, rag
from tqdm import tqdm


# from this blog https://openai.com/index/new-embedding-models-and-api-updates/
# 512 seems to provide good MTEB score with text-embedding-3-small
EMBEDDINGS_DIMENSION = 1536
MODEL = "text-embedding-3-small"
RAW_DATA_PATH = "material.md"
VDB_PATH = "vdb_data"
PKL_PATH = "raw_vector.pkl"
SUMMARY_PKL_PATH = "vector.pkl"
LEARNING_OBJECTIVES = [
    "Identify sages (early philosophers) across historical traditions.",
    "Explain the connection between ancient philosophy and the origin of the sciences.",
    "Describe philosophy as a discipline that makes coherent sense of a whole.",
    "Summarize the broad and diverse origins of philosophy."
]

REVIEW_QUESTIONS = [
    "What are some common characteristics of ancient sages in the Greek, Indian, and Chinese traditions?",
    "What characteristics are essential for being identified as a “sage”?",
    "What is the connection between sages and philosophers?",
    "Provide one example of an ancient philosopher or sage who was doing something like natural science. What made this philosopher's activity scientific?",
    "What does it mean for philosophy to “have an eye on the whole”? How is this different from other disciplines?",
    "Why is it necessary for philosophers to discard suppositions or assumptions that may be acceptable in other disciplines?"
]

api_key = os.environ.get("OPENAI_API_KEY")
if not api_key:
    raise ValueError("OPENAI_API_KEY environment variable not set")
client = OpenAI(api_key=api_key)

async def _create_embeddings(
    input: str, http_session: aiohttp.ClientSession
) -> openai.EmbeddingData:
    results = await openai.create_embeddings(
        input=[input],
        model=MODEL,
        dimensions=EMBEDDINGS_DIMENSION,
        http_session=http_session,
    )
    return results[0]

async def _generate_summary(paragraphs, http_session: aiohttp.ClientSession) -> str:
    """Generate a concise summary focused on the learning objectives."""
    
    # makedoc
    content = "\n\n".join(paragraphs)
    
    # Define the system prompt to guide the summary generation
    system_prompt = (
        "You are a philosophy professor creating concise teaching material. "
        "Generate a summary focused specifically on these learning objectives:\n" + 
        "\n".join(f"- {obj}" for obj in LEARNING_OBJECTIVES) + 
        "Content summarized must be structured in a way to answer questions like the example review questions:\n" + 
        "\n".join(f"- {obj}" for obj in REVIEW_QUESTIONS) + 
        "\n\nYour summary should be comprehensive enough to teach from, but concise and "
        "focused only on these objectives. Include key examples of sages from different "
        "traditions, clear explanations of philosophy's connection to sciences, and the "
        "diverse origins of philosophical thinking."
    )
    
    response = client.chat.completions.create(
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": f"Here is the source material:\n\n{content}\n\nCreate a summary that addresses the learning objectives while not omiting any key concepts or ideas:"}
        ],
        model="gpt-4-turbo",
        temperature=0.3,
    )
    
    return response.choices[0].message.content

async def main() -> None:
    async with aiohttp.ClientSession() as http_session:

        # clean old ver
        if os.path.exists(PKL_PATH):
            os.remove(PKL_PATH)

        if os.path.exists(SUMMARY_PKL_PATH):
            os.remove(SUMMARY_PKL_PATH)
            
        # generate vector db
        print("Creating from raw data...")
        paragraphs_by_uuid = {}
        raw_data = open(RAW_DATA_PATH, "r", encoding="utf-8").read()
        
        index_builder = rag.annoy.IndexBuilder(f=EMBEDDINGS_DIMENSION, metric="angular")
        
        for p in tokenize.basic.tokenize_paragraphs(raw_data):
            p_uuid = uuid.uuid4()
            paragraphs_by_uuid[p_uuid] = p
        
        for p_uuid, paragraph in tqdm(paragraphs_by_uuid.items()):
            resp = await _create_embeddings(paragraph, http_session)
            index_builder.add_item(resp.embedding, p_uuid)
        
        index_builder.build()
        index_builder.save(VDB_PATH)
        
        # save data (w pickle)
        with open(PKL_PATH, "wb") as f:
            pickle.dump(paragraphs_by_uuid, f)
        
        print(f"Created and saved vector database with {len(paragraphs_by_uuid)} paragraphs.")
        
        # generate summary
        paragraphs = list(paragraphs_by_uuid.values())
        print("Generating summary based on learning objectives...")
        summary = await _generate_summary(paragraphs, http_session)
        
        # gen new vector db
        summary_paragraphs = tokenize.basic.tokenize_paragraphs(summary)
        summary_by_uuid = {}
        summary_idx_builder = rag.annoy.IndexBuilder(f=EMBEDDINGS_DIMENSION, metric="angular")
        
        for p in summary_paragraphs:
            p_uuid = uuid.uuid4()
            summary_by_uuid[p_uuid] = p
        
        for p_uuid, paragraph in tqdm(summary_by_uuid.items()):
            resp = await _create_embeddings(paragraph, http_session)
            summary_idx_builder.add_item(resp.embedding, p_uuid)
        
        summary_idx_builder.build()
        summary_idx_builder.save("summary_vdb_data")
        
        # save summary 
        with open(SUMMARY_PKL_PATH, "wb") as f:
            pickle.dump(summary_by_uuid, f)
        
        print(f"Created and saved summary vector database with {len(summary_by_uuid)} paragraphs.")
        print(f"Summary saved to {SUMMARY_PKL_PATH}")
        
        # ok we can do this
        with open("summary.txt", "w", encoding="utf-8") as f:
            f.write(summary)
        
        print("Full summary text saved to summary.txt")

if __name__ == "__main__":
    asyncio.run(main())




if __name__ == "__main__":
    asyncio.run(main())
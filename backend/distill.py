import os
from openai import OpenAI

# oops lol
api_key = os.environ.get("OPENAI_API_KEY")
if not api_key:
    raise ValueError("OPENAI_API_KEY environment variable not set")
client = OpenAI(api_key=api_key)


def get_raw_data(material_path):
    if os.path.exists(material_path):
        # generate vector db
        print("Fetching from raw data...")
        raw_data = open(material_path, "r", encoding="utf-8").read()
        return raw_data.split("\n\n")[1:] # skip ""
    else:
        return Exception(f"Could not find path {material_path}")

def generate_summary(material_path):
    content = "\n\n".join(get_raw_data(material_path)) # lol
    print("Distilling from raw data...")
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
    system_prompt = (
        "You are a philosophy professor creating concise teaching material. "
        "Generate a summary focused specifically on these learning objectives:\n" + 
        "\n".join(f"- {obj}" for obj in LEARNING_OBJECTIVES) + 
        "Content summarized must be structured in a way that answer questions like the example review questions:\n" + 
        "\n".join(f"- {obj}" for obj in REVIEW_QUESTIONS) + 
        "\n\nThese should serve to guide your summary making but do not mention them explitly. "
        "Your summary should be comprehensive enough to teach from, but concise and "
        "focused only on these objectives. Include key examples of sages from different "
        "traditions, clear explanations of philosophy's connection to sciences, and the "
        "diverse origins of philosophical thinking."
    )
    response = client.chat.completions.create(
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": f"Here is the source material:\n\n{content}\n\nCreate a summary that addresses the learning objectives while not omiting any key concepts or ideas. Do not reference this task or call this a summary:"}
        ],
        model="gpt-4-turbo",
        temperature=0.3,
    )
    
    return response.choices[0].message.content

def main():
    with open("summary.md", "w", encoding="utf-8") as f:
        f.write(generate_summary("material.md"))

if __name__ == "__main__":
    main()
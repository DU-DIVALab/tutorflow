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
    content = get_raw_data(material_path) # lol
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
        "You are a philosophy professor creating part of a podcast (should be a short paragraph in length) from a paragraph of the material. "
        # "specifically focused specifically on these learning objectives:\n" + 
        # "\n".join(f"- {obj}" for obj in LEARNING_OBJECTIVES) + 
        # "\n\nThese should serve to guide your summary making but do not mention them explitly."
    )


    with open("summary.md", "w", encoding="utf-8") as f:
        f.write("### Philosophy\n\n")
        for section in content:
            f.write("#### Section\n\n")
            

            response = client.chat.completions.create(
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": f"Here is the paragraph:\n\n{section}\n\nDo not include an intro or outro, this content will be part of the middle of the podcast."}
                ],
                model="gpt-4-turbo",
                temperature=0.3,
            )
            f.write(f"{response.choices[0].message.content}\n\n")
    


def main():
    generate_summary("material.md")

if __name__ == "__main__":
    main()
from dotenv import load_dotenv
load_dotenv()

from google import genai
import os

client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])

for m in client.models.list():
    print(m.name, vars(m))
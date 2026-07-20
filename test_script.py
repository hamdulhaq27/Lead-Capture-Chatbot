from groq import Groq

client = Groq(api_key="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")

for model in client.models.list().data:
    print(model.id)
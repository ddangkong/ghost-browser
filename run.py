from browser_use import Agent, ChatOllama


llm = ChatOllama(
	model='gemma4:latest',
	ollama_options={'temperature': 0, 'num_ctx': 32768},
)

agent = Agent(
	task='Open https://example.com and tell me the page title.',
	llm=llm,
)

agent.run_sync()

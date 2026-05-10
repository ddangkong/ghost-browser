from collections.abc import Mapping
from dataclasses import dataclass
import json
from typing import Any, TypeVar, overload

import httpx
from ollama import AsyncClient as OllamaAsyncClient
from ollama import Options
from pydantic import BaseModel

from browser_use.llm.base import BaseChatModel
from browser_use.llm.exceptions import ModelProviderError
from browser_use.llm.messages import BaseMessage
from browser_use.llm.ollama.serializer import OllamaMessageSerializer
from browser_use.llm.views import ChatInvokeCompletion

T = TypeVar('T', bound=BaseModel)


def _extract_json_object(content: str) -> str:
	"""Return a JSON object from local models that wrap structured output in prose or fences."""
	text = content.strip()
	if text.startswith('```'):
		lines = text.splitlines()
		if lines and lines[0].lstrip().startswith('```'):
			lines = lines[1:]
		if lines and lines[-1].strip() == '```':
			lines = lines[:-1]
		text = '\n'.join(lines).strip()

	try:
		json.loads(text)
		return text
	except json.JSONDecodeError:
		pass

	start = text.find('{')
	if start == -1:
		return text

	depth = 0
	in_string = False
	escape = False
	for index, char in enumerate(text[start:], start=start):
		if in_string:
			if escape:
				escape = False
			elif char == '\\':
				escape = True
			elif char == '"':
				in_string = False
			continue

		if char == '"':
			in_string = True
		elif char == '{':
			depth += 1
		elif char == '}':
			depth -= 1
			if depth == 0:
				return text[start : index + 1]

	return text


@dataclass
class ChatOllama(BaseChatModel):
	"""
	A wrapper around Ollama's chat model.
	"""

	model: str

	# # Model params
	# TODO (matic): Why is this commented out?
	# temperature: float | None = None

	# Client initialization parameters
	host: str | None = None
	timeout: float | httpx.Timeout | None = None
	client_params: dict[str, Any] | None = None
	ollama_options: Mapping[str, Any] | Options | None = None

	# Static
	@property
	def provider(self) -> str:
		return 'ollama'

	def _get_client_params(self) -> dict[str, Any]:
		"""Prepare client parameters dictionary."""
		return {
			'host': self.host,
			'timeout': self.timeout,
			'client_params': self.client_params,
		}

	def get_client(self) -> OllamaAsyncClient:
		"""
		Returns an OllamaAsyncClient client.
		"""
		return OllamaAsyncClient(host=self.host, timeout=self.timeout, **self.client_params or {})

	@property
	def name(self) -> str:
		return self.model

	@overload
	async def ainvoke(
		self, messages: list[BaseMessage], output_format: None = None, **kwargs: Any
	) -> ChatInvokeCompletion[str]: ...

	@overload
	async def ainvoke(self, messages: list[BaseMessage], output_format: type[T], **kwargs: Any) -> ChatInvokeCompletion[T]: ...

	async def ainvoke(
		self, messages: list[BaseMessage], output_format: type[T] | None = None, **kwargs: Any
	) -> ChatInvokeCompletion[T] | ChatInvokeCompletion[str]:
		ollama_messages = OllamaMessageSerializer.serialize_messages(messages)

		try:
			if output_format is None:
				response = await self.get_client().chat(
					model=self.model,
					messages=ollama_messages,
					options=self.ollama_options,
				)

				return ChatInvokeCompletion(completion=response.message.content or '', usage=None)
			else:
				schema = output_format.model_json_schema()

				response = await self.get_client().chat(
					model=self.model,
					messages=ollama_messages,
					format=schema,
					options=self.ollama_options,
				)

				completion = response.message.content or ''
				if output_format is not None:
					completion = _extract_json_object(completion)
					completion = output_format.model_validate_json(completion)

				return ChatInvokeCompletion(completion=completion, usage=None)

		except Exception as e:
			raise ModelProviderError(message=str(e), model=self.name) from e

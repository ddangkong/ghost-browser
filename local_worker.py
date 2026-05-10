from __future__ import annotations

import json
import sys
import traceback
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx
from browser_use import Agent, BrowserProfile, ChatOllama
from browser_use.agent.views import AgentHistoryList, MessageCompactionSettings
from browser_use.llm.messages import UserMessage


FINAL_START = '__BROWSER_USE_FINAL_RESULT_START__'
FINAL_END = '__BROWSER_USE_FINAL_RESULT_END__'
OLLAMA_URL = 'http://127.0.0.1:11434'
LOGS_DIR = Path(__file__).parent / 'logs'
AUTO_MAX_STEPS = 80
MAX_IDENTICAL_ACTIONS = 3
HARNESS_RECOVERY_ATTEMPTS = 2
MAX_TOTAL_RECOVERIES = 20  # force done() when harness has recovered this many times in one attempt

# JavaScript to enumerate all non-blank iframes — used by iframe approach 1
_IFRAME_LIST_JS = (
	'(function(){'
	'var f=Array.from(document.querySelectorAll("iframe"))'
	'.filter(function(el){return el.src&&el.src.indexOf("about:blank")<0&&el.src.indexOf("javascript:")<0;})'
	'.map(function(el){return{id:el.id||"",name:el.name||"",src:el.src,w:el.offsetWidth,h:el.offsetHeight};});'
	'return JSON.stringify(f);'
	'})()'
)


def sanitize_agent_text(text: str) -> str:
	"""Return agent context unchanged; the browser agent must verify and correct it."""
	return text or ''


_FIND_ELEMENTS_NOISE_KEYS = frozenset({'max_results', 'highlight', 'scroll_into_view'})
_INTERNAL_URL_PREFIXES = ('edge://', 'chrome://', 'about:', 'data:', 'blob:')


def _normalize_action_data(action_name: str, params: Any) -> Any:
	"""Strip noise parameters from well-known action types so signatures stay stable across variations."""
	if action_name == 'find_elements' and isinstance(params, dict):
		return {k: v for k, v in params.items() if k not in _FIND_ELEMENTS_NOISE_KEYS}
	return params


def action_signature(actions: list[Any]) -> str:
	"""Create a stable signature for the model's planned browser actions."""
	serialized: list[Any] = []
	for action in actions:
		if hasattr(action, 'model_dump'):
			raw = action.model_dump(exclude_none=True, exclude_unset=True)
			normalized = {k: _normalize_action_data(k, v) for k, v in raw.items()}
			serialized.append(normalized)
		else:
			serialized.append(str(action))
	return json.dumps(serialized, ensure_ascii=False, sort_keys=True, default=str)


def action_policy_violation(actions: list[Any]) -> str:
	"""Return a generic harness policy violation for actions known to be invalid before execution."""
	for action in actions:
		if hasattr(action, 'model_dump'):
			action_data = action.model_dump(exclude_none=True, exclude_unset=True)
		else:
			continue
		params = action_data.get('find_elements')
		if isinstance(params, dict):
			selector = str(params.get('selector', ''))
			if ':contains(' in selector:
				return (
					'Invalid CSS selector for find_elements: ":contains(...)" is jQuery syntax and is not supported by '
					'querySelectorAll. Use search_page, scan_visible_text, extract, or evaluate to locate text, or use a valid CSS selector.'
				)
	return ''


def action_hint(actions: list[Any], next_goal: str | None = None) -> str:
	"""Extract useful words from a blocked action to guide recovery without repeating it."""
	hints: list[str] = []
	if next_goal:
		hints.append(next_goal)
	for action in actions:
		if not hasattr(action, 'model_dump'):
			continue
		action_data = action.model_dump(exclude_none=True, exclude_unset=True)
		for action_name, params in action_data.items():
			hints.append(str(action_name))
			if isinstance(params, dict):
				for key in ('text', 'pattern', 'query', 'selector', 'url'):
					value = params.get(key)
					if value:
						hints.append(str(value))
	return ' '.join(hints)[:500]


def make_action(agent: Agent, action_name: str, params: dict[str, Any]) -> Any:
	"""Build an ActionModel instance for the current page action schema."""
	return agent.ActionModel.model_validate({action_name: params})


def _get_find_elements_selector(actions: list[Any]) -> str | None:
	"""Return the selector from a find_elements action, or None if this is not find_elements."""
	for action in actions:
		if not hasattr(action, 'model_dump'):
			continue
		data = action.model_dump(exclude_none=True, exclude_unset=True)
		params = data.get('find_elements')
		if isinstance(params, dict):
			return str(params.get('selector', ''))
	return None


def recovery_action(agent: Agent, actions: list[Any], reason: str, next_goal: str | None = None) -> Any:
	"""Choose a generic in-session recovery action that preserves browser state."""
	hint = action_hint(actions, next_goal)
	signature = action_signature(actions)

	if '"scan_visible_text"' in signature:
		query = (
			'Extract the relevant content visible or near the current viewport for the user task. '
			'If a table-like section is visible, include row labels, column labels, and numeric values. '
			f'Goal/hints: {hint}'
		)
		return make_action(agent, 'extract', {'query': query})

	# find_elements kept returning but agent never followed up with a click/extract.
	# Inject extract so the agent can act on whatever is visible instead of re-discovering.
	if '"find_elements"' in signature:
		selector = _get_find_elements_selector(actions) or ''
		if ':contains(' in selector:
			query = f'{hint} table row labels values nearby visible text'
			return make_action(agent, 'scan_visible_text', {'query': query, 'max_results': 40})
		query = (
			f'Extract all text content visible on the current page relevant to: {hint}. '
			'Include any table data, section headings, key figures, or narrative text that is currently visible in the viewport. '
			f'Selector that was repeatedly failing: {selector}'
		)
		return make_action(agent, 'extract', {'query': query})

	query = hint or reason
	return make_action(agent, 'scan_visible_text', {'query': query, 'max_results': 40})


def choose_recovery(
	current_url: str,
	page_state: dict[str, Any],
	iframe_state: dict[str, Any],
	agent: Agent,
	actions: list[Any],
	reason: str,
	next_goal: str | None = None,
	nav_threshold: int = 3,
) -> tuple[Any, str]:
	"""Return (replacement_action, message) using normal recovery or the 3-step iframe ladder.

	Iframe ladder (triggered after nav_threshold recoveries on same URL):
	  Approach 1 — evaluate iframe srcs, agent navigates to content directly
	  Approach 2 — enable coordinate clicking + screenshot so agent clicks visually
	  Approach 3 — scan for PDF/download fallback
	  Exhausted   — go_back
	"""
	if current_url and current_url == page_state['url']:
		page_state['count'] += 1
	else:
		page_state['url'] = current_url
		page_state['count'] = 1
		if iframe_state.get('url') != current_url:
			iframe_state['url'] = ''
			iframe_state['approach'] = 0

	if page_state['count'] < nav_threshold:
		return recovery_action(agent, actions, reason, next_goal), ''

	# --- Stuck on same URL: run iframe recovery ladder ---
	if iframe_state.get('url') != current_url:
		iframe_state['url'] = current_url
		iframe_state['approach'] = 0

	approach = iframe_state['approach']
	page_state['count'] = 0  # reset so each approach gets fresh attempts

	if approach == 0:
		iframe_state['approach'] = 1
		print(f'[harness-iframe-1] Approach 1 (evaluate iframes) on {current_url!r}', flush=True)
		action = make_action(agent, 'evaluate', {'code': _IFRAME_LIST_JS})
		msg = (
			'HARNESS IFRAME RECOVERY — Approach 1: The content you need may be inside an iframe. '
			'The harness ran evaluate() to list all iframes on this page. '
			'Look at the result: find the iframe with the largest width/height — that is the main content frame. '
			'Your NEXT action MUST be: navigate(url="<that iframe src>"). '
			'Do NOT call find_elements, extract, or scan_visible_text — navigate directly to the iframe src URL.'
		)
		return action, msg

	elif approach == 1:
		iframe_state['approach'] = 2
		print(f'[harness-iframe-2] Approach 2 (vision + coordinate click) on {current_url!r}', flush=True)
		agent.tools.set_coordinate_clicking(True)
		action = make_action(agent, 'screenshot', {})
		msg = (
			'HARNESS IFRAME RECOVERY — Approach 2: DOM navigation failed. '
			'Coordinate-based clicking is now ENABLED. '
			'Study the screenshot. Identify the exact visual position of the element you need to click. '
			'Use click(coordinate_x=X, coordinate_y=Y) instead of click(index=N). '
			'If the content is a table or text visible in the screenshot, use extract() to read what you can see.'
		)
		return action, msg

	elif approach == 2:
		iframe_state['approach'] = 3
		print(f'[harness-iframe-3] Approach 3 (PDF/download fallback) on {current_url!r}', flush=True)
		action = make_action(agent, 'scan_visible_text', {
			'query': 'PDF download 다운로드 원문보기 첨부파일 파일 저장 download file',
			'max_results': 20,
		})
		msg = (
			'HARNESS IFRAME RECOVERY — Approach 3: Visual interaction also failed. '
			'Look for a PDF or file download link on this page. '
			'If you find a download link, click it to download the file. '
			'Then use extract() on the downloaded file to read its contents. '
			'If no download link exists, call done() with whatever partial information you have gathered so far.'
		)
		return action, msg

	else:
		iframe_state['url'] = ''
		iframe_state['approach'] = 0
		print(f'[harness-navigate] All iframe approaches exhausted on {current_url!r} — go_back', flush=True)
		return make_action(agent, 'go_back', {}), (
			'HARNESS RECOVERY: All iframe recovery approaches exhausted. Navigated back. '
			'Choose a completely different page or source. Do not return to the same URL.'
		)


def call_ollama(prompt: str, model: str, timeout: int = 180) -> str:
	try:
		resp = httpx.post(
			f'{OLLAMA_URL}/api/generate',
			json={
				'model': model,
				'prompt': prompt,
				'stream': False,
				'options': {'temperature': 0.1, 'num_ctx': 8192},
			},
			timeout=timeout,
		)
		resp.raise_for_status()
		return resp.json().get('response', '')
	except Exception as e:
		print(f'[planner] Ollama call failed: {e}', flush=True)
		return ''


def generate_plan(task: str, model: str) -> str:
	"""Ask the LLM to produce a detailed browser execution plan before the agent starts."""
	prompt = f"""You are a browser automation expert. A user wants to complete this task using a web browser:

{task}

Create a detailed, numbered step-by-step execution plan for a browser agent. Be specific about:
- Exact URLs or sites to navigate to
- Search terms and keywords to use
- Specific page sections, tabs, or elements to locate and interact with
- What data to extract and how to structure the final output
- How to verify the task is complete

Important:
- Do not invent official URLs. If unsure, tell the browser agent to search the web for the official site.
- Avoid Markdown code fences or inline code marks around URLs.
- If you include a URL, it is still only a draft suggestion. The browser agent must verify it before relying on it.

Output ONLY the numbered plan. No intro, no outro."""

	print('[planner] Generating execution plan...', flush=True)
	plan = sanitize_agent_text(call_ollama(prompt, model))
	if plan:
		print(f'[planner] Plan ready ({len(plan)} chars).', flush=True)
	else:
		print('[planner] Planning failed, proceeding without plan.', flush=True)
	return plan


def format_history_as_log(history: AgentHistoryList, attempt: int) -> str:
	"""Convert a completed agent run into a full, detailed step-by-step log."""
	lines: list[str] = []
	lines.append(f'=== ATTEMPT {attempt} HISTORY ===')
	lines.append(f'Steps completed: {history.number_of_steps()}')
	lines.append(f'Duration: {history.total_duration_seconds():.1f}s')

	visited_urls = [u for u in history.urls() if u]
	if visited_urls:
		lines.append(f'URLs visited ({len(visited_urls)}):')
		for url in visited_urls:
			lines.append(f'  - {sanitize_agent_text(url)}')

	lines.append('')
	lines.append('--- STEP-BY-STEP ACTIONS AND RESULTS ---')
	for step_text in history.agent_steps():
		lines.append(sanitize_agent_text(step_text))

	thoughts = history.model_thoughts()
	if thoughts:
		lines.append('--- AGENT REASONING PER STEP ---')
		for i, thought in enumerate(thoughts, 1):
			lines.append(f'Step {i}:')
			if thought.evaluation_previous_goal:
				lines.append(f'  Eval: {thought.evaluation_previous_goal}')
			if thought.memory:
				lines.append(f'  Memory: {thought.memory}')
			if thought.next_goal:
				lines.append(f'  Next goal: {thought.next_goal}')

	extracted = [e for e in history.extracted_content() if e]
	if extracted:
		lines.append(f'--- EXTRACTED CONTENT ({len(extracted)} items) ---')
		for i, content in enumerate(extracted, 1):
			lines.append(f'[{i}]\n{content}')

	errors = [e for e in history.errors() if e]
	if errors:
		lines.append(f'--- ERRORS ({len(errors)}) ---')
		for error in errors:
			lines.append(f'- {sanitize_agent_text(error)}')

	return '\n'.join(lines)


def save_partial_results(history: AgentHistoryList, attempt: int) -> None:
	"""Save extracted content and agent memory to results.md when no formal done() was produced."""
	extracted = [e for e in history.extracted_content() if e and len(e.strip()) > 50]
	thoughts = history.model_thoughts()
	memories = [t.memory for t in thoughts if t.memory and len(t.memory.strip()) > 20]

	if not extracted and not memories:
		return

	lines: list[str] = [
		f'# Partial Results — Attempt {attempt}',
		'*(Auto-saved by harness — agent did not complete the task)*\n',
	]

	if memories:
		lines.append('## Agent Memory (last 5 steps)')
		for m in memories[-5:]:
			lines.append(f'- {m}')
		lines.append('')

	if extracted:
		lines.append(f'## Extracted Content ({len(extracted)} items)')
		for i, content in enumerate(extracted, 1):
			lines.append(f'\n### Extract {i}')
			lines.append(content[:5000])

	results_path = Path(__file__).parent / 'results.md'
	results_path.write_text('\n'.join(lines), encoding='utf-8')
	print(f'[harness] Partial results saved -> {results_path}', flush=True)


def save_history_to_disk(history: AgentHistoryList, attempt: int, task: str, result: str | None) -> Path:
	"""Persist the full attempt history to a markdown file in logs/."""
	LOGS_DIR.mkdir(exist_ok=True)
	timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
	path = LOGS_DIR / f'attempt_{attempt}_{timestamp}.md'

	sections: list[str] = [
		f'# Attempt {attempt} - {timestamp}',
		f'## Task\n{task}',
		f'## Result\n{result or "(none)"}',
		format_history_as_log(history, attempt),
	]
	path.write_text('\n\n'.join(sections), encoding='utf-8')
	print(f'[history] Saved -> {path}', flush=True)
	return path


def evaluate_result(task: str, result: str, model: str) -> tuple[bool, str]:
	"""Ask the LLM to judge whether the result fully satisfies the task."""
	if not result or len(result.strip()) < 50:
		return False, 'Result is empty or too short.'

	prompt = f"""Task: {task}

Browser agent result:
{result}

Evaluate strictly whether this result fully completes the task.

Reply in EXACTLY this format (no extra text):
ADEQUATE: yes/no
MISSING: <what specific information or action is missing, or "nothing" if adequate>
APPROACH: <concrete revised approach the agent should use next time, or "none" if adequate>"""

	print('[evaluator] Evaluating result quality...', flush=True)
	evaluation = call_ollama(prompt, model, timeout=120)

	lower = evaluation.lower()
	is_adequate = 'adequate: yes' in lower

	approach = ''
	if 'APPROACH:' in evaluation:
		approach = evaluation.split('APPROACH:', 1)[1].strip()
	if not approach or approach.lower() == 'none':
		approach = evaluation if not is_adequate else ''

	print(f'[evaluator] Adequate: {is_adequate}', flush=True)
	if not is_adequate and approach:
		print(f'[evaluator] Feedback: {approach}', flush=True)
	return is_adequate, approach


def generate_recovery_plan(
	task: str,
	history_log: str,
	feedback: str,
	forbidden_actions: list[str],
	model: str,
) -> str:
	"""Ask the harness LLM to diagnose the failed attempt and create a concrete bypass plan."""
	if not history_log and not feedback:
		return ''

	forbidden_text = '\n'.join(f'- {action}' for action in forbidden_actions[-12:]) or '(none)'
	prompt = f"""You are the harness supervisor for a browser automation agent.

User task:
{task}

The last browser attempt failed. Read the full attempt log and evaluator/loop feedback, then create a recovery plan that avoids the failed action pattern.

Evaluator/loop feedback:
{feedback or '(none)'}

Forbidden exact action outputs:
{forbidden_text}

Full attempt log:
{history_log}

Output in this exact format, concise but specific:
FAILURE_CAUSE:
- <root cause inferred from the log>

DO_NOT_REPEAT:
- <concrete actions, indices, selectors, URLs, queries, or assumptions that failed>

BYPASS_PLAN:
1. <first concrete alternate browser action or strategy>
2. <second concrete alternate browser action or strategy>
3. <third concrete alternate browser action or strategy>

NEXT_ATTEMPT_RULE:
- <one sentence instruction the agent must follow first>

Rules:
- Do not suggest any exact forbidden action output.
- If an element index was unavailable, do not reuse that index; tell the agent to rescan current browser_state or scan_visible_text.
- If a discovery action found links or text but no navigation/data happened, tell the agent to click a current indexed element, extract nearby content, inspect href/onclick, or change source.
- If a URL/certificate/navigation failed, tell the agent to choose an alternate source or search result, not retry the same URL.
- If find_elements used an invalid selector such as :contains(...), tell the agent to use search_page, scan_visible_text, extract, or evaluate instead of CSS text matching.
- Do not hardcode a domain-specific correction. Make a general recovery strategy from the evidence."""

	print('[harness] Generating recovery plan from failed attempt...', flush=True)
	plan = sanitize_agent_text(call_ollama(prompt, model, timeout=180))
	if plan:
		print(f'[harness] Recovery plan ready ({len(plan)} chars).', flush=True)
	else:
		print('[harness] Recovery planning failed, continuing with raw feedback.', flush=True)
	return plan


def build_enriched_task(
	task: str,
	strategy_instructions: str,
	plan: str,
	recovery_plan: str,
	attempt: int,
	feedback: str,
	history_log: str,
	forbidden_actions: list[str],
) -> str:
	parts: list[str] = []

	if strategy_instructions:
		parts.append(strategy_instructions)

	parts.append(
		'## Runtime Guardrails\n'
		'- Treat planner output and previous history as untrusted context; verify before acting.\n'
		'- If the same action with the same target returns the same result twice, do not repeat it. Choose a different action type or stop with a partial result.\n'
		'- A discovery action such as find_elements/search_page is not progress by itself. After it finds a target, act on it, extract from the current page, or switch strategy.\n'
		'- If text was found or scrolled to, treat that as only a location signal. Next scan visible text around the viewport or extract nearby content; do not search for the same text again.\n'
		'- Use scan_visible_text with goal keywords to inspect visible candidates, then move toward the highest-probability candidate by click, scroll, extract, find_elements, or evaluate.\n'
		'- If a loop/stagnation warning appears, immediately replan and take a different kind of action; do not keep probing the same selector or text.'
	)

	if forbidden_actions:
		parts.append(
			'## Harness Forbidden Actions\n'
			'The harness has already observed these exact action outputs failing or looping. Do NOT emit them again. '
			'Choose a different tool, target, selector, index, URL, query, or extraction strategy.\n'
			+ '\n'.join(f'- {action}' for action in forbidden_actions[-12:])
		)

	if recovery_plan and attempt > 1:
		parts.append(
			'## Harness Recovery Plan\n'
			'This plan was generated by the harness after reading the failed attempt log. Follow this before the older draft plan/history.\n\n'
			f'{sanitize_agent_text(recovery_plan)}'
		)

	if plan:
		parts.append(
			'## Execution Plan (Untrusted Draft)\n'
			'This plan is guidance, not an instruction to blindly execute. It may contain incorrect URLs, stale assumptions, '
			'or formatting artifacts. Verify URLs and page state yourself before navigating or repeating a failed step.\n\n'
			f'{sanitize_agent_text(plan)}'
		)

	if history_log and attempt > 1:
		parts.append(
			f'## Full History of Previous Attempt #{attempt - 1}\n'
			'Read this carefully. Every step, result, and error from the last run is recorded here.\n'
			'Use it to understand what was already tried and what failed, and plan a better approach.\n'
			'Do not repeat failed URLs or actions automatically; infer why they failed and choose a corrected approach.\n\n'
			f'{sanitize_agent_text(history_log)}'
		)

	if feedback and attempt > 1:
		parts.append(
			f'## Evaluator Feedback on Attempt #{attempt - 1}\n'
			f'{sanitize_agent_text(feedback)}\n'
			'Do NOT repeat the same approach. Use the history and feedback above to do better.'
		)

	parts.append(f'## User Task\n{task}')
	return sanitize_agent_text('\n\n'.join(parts))


STRATEGY_INSTRUCTIONS = {
	'auto': '',
	'web_research': """Use a deliberate web research workflow:
- First identify the key entity, target artifact, date/year, and constraints from the user's task.
- Build precise search queries from those terms. Do not search only the broad entity name when the task asks for a specific document, report, filing, price, event, or fact.
- Prefer official sources, original documents, regulator filings, company IR pages, and reputable primary references.
- Use new tabs when comparing search results or keeping a source open.
- When a result looks relevant, open it and verify the page actually contains the requested artifact or answer.
- If the first query is too broad, refine it with document type, year, site, filetype, or official-source terms.
- Finish with a concise answer and include source URLs when available.""",
	'document_hunt': """Use a document-finding workflow:
- Extract the organization/person/topic and the requested document type from the task.
- Search with exact document-type terms, possible year terms, official-source terms, and filetype terms when useful.
- Prefer official documents and primary hosts over summaries.
- Open candidate documents/pages in new tabs, verify title/date/source, and avoid stopping at unrelated homepage or broad search results.
- Finish with the document title, source, URL, and a brief note on why it matches.""",
}


def main() -> int:
	if len(sys.argv) != 2:
		print('Usage: local_worker.py <config.json>', flush=True)
		return 2

	try:
		sys.stdout.reconfigure(encoding='utf-8', errors='replace')
		sys.stderr.reconfigure(encoding='utf-8', errors='replace')
	except Exception:
		pass

	config = json.loads(Path(sys.argv[1]).read_text(encoding='utf-8'))
	task = config['task']
	model = config['model']
	raw_max_steps = config.get('max_steps', 0)
	max_attempts = config.get('max_attempts', 3)
	effective_max_attempts = max_attempts + HARNESS_RECOVERY_ATTEMPTS
	strategy = config.get('strategy', 'auto')
	strategy_instructions = STRATEGY_INSTRUCTIONS.get(strategy, '')

	effective_max_steps = raw_max_steps if raw_max_steps > 0 else AUTO_MAX_STEPS

	llm = ChatOllama(
		model=model,
		ollama_options={
			'temperature': config['temperature'],
			'num_ctx': config['num_ctx'],
		},
	)
	no_compaction = MessageCompactionSettings(enabled=False)
	plan = generate_plan(task, model)

	best_result: str | None = None
	best_adequate = False
	last_feedback = ''
	last_history_log = ''
	forbidden_actions: list[str] = []
	last_recovery_plan = ''

	for attempt in range(1, effective_max_attempts + 1):
		attempt_label = f'{attempt}/{max_attempts}'
		if attempt > max_attempts:
			attempt_label = f'{attempt}/{max_attempts}+{HARNESS_RECOVERY_ATTEMPTS} recovery'
		print(f'[worker] -- Attempt {attempt_label} --', flush=True)
		repeat_state = {'last_signature': '', 'count': 0, 'reason': ''}
		page_recovery_state: dict[str, Any] = {'url': '', 'count': 0}
		iframe_recovery_state: dict[str, Any] = {'url': '', 'approach': 0}
		total_recovery_count = [0]  # mutable container for closure
		last_real_url: list[str] = ['']  # last non-internal URL seen

		def apply_recovery(current_url: str, actions: list[Any], reason: str, next_goal: str | None, model_output: Any, policy_violation: str = '') -> None:
			"""Shared recovery injection: choose replacement, apply it, notify agent, check global recovery limit."""
			total_recovery_count[0] += 1
			if total_recovery_count[0] > MAX_TOTAL_RECOVERIES:
				print(
					f'[harness-done] {total_recovery_count[0]} total recoveries — forcing done(success=False)',
					flush=True,
				)
				replacement = make_action(
					agent,
					'done',
					{
						'text': (
							'HARNESS FORCED STOP: The harness has intercepted too many repeated/invalid actions. '
							'The agent is stuck in a loop and cannot make further progress on the current page or search. '
							'Summarize everything you have found so far and stop. '
							'Partial result is acceptable.'
						),
						'success': False,
					},
				)
				model_output.action = [replacement]
				agent.state.last_model_output = model_output
				agent._message_manager._add_context_message(
					UserMessage(
						content=(
							'HARNESS FORCED STOP: You have been stuck in a recovery loop for too many steps. '
							'Call done() immediately with whatever partial information you have gathered. '
							'Do NOT continue browsing.'
						)
					)
				)
				return

			replacement, iframe_msg = choose_recovery(current_url, page_recovery_state, iframe_recovery_state, agent, actions, reason, next_goal)
			model_output.action = [replacement]
			agent.state.last_model_output = model_output
			if iframe_msg:
				msg = iframe_msg
			elif policy_violation:
				msg = (
					'HARNESS RECOVERY: The previous action was invalid or repeated. '
					f'Reason: {policy_violation} '
					'The harness replaced it. Use the replacement result to continue; do not repeat the blocked action.'
				)
			else:
				msg = (
					'HARNESS RECOVERY: The previous action was invalid or repeated. '
					'The harness replaced it. Use the replacement result to continue; do not repeat the blocked action.'
				)
			agent._message_manager._add_context_message(UserMessage(content=msg))

		async def guard_model_actions(_browser_state: Any, model_output: Any, _step_num: int) -> None:
			actions = getattr(model_output, 'action', None) if model_output else None
			if not actions:
				return

			current_url = getattr(_browser_state, 'url', '') or ''

			# Track last real URL and recover if browser landed on an internal page
			if current_url and not any(current_url.startswith(p) for p in _INTERNAL_URL_PREFIXES):
				last_real_url[0] = current_url
			elif current_url and any(current_url.startswith(p) for p in _INTERNAL_URL_PREFIXES) and last_real_url[0]:
				print(f'[harness-newtab] Internal URL detected ({current_url!r}), navigating back to {last_real_url[0]!r}', flush=True)
				model_output.action = [make_action(agent, 'navigate', {'url': last_real_url[0]})]
				agent.state.last_model_output = model_output
				agent._message_manager._add_context_message(UserMessage(content=(
					f'HARNESS RECOVERY: The browser landed on an internal page ({current_url}). '
					f'The harness is navigating you back to your last page: {last_real_url[0]}. '
					'Continue your task from there.'
				)))
				return

			signature = action_signature(actions)
			policy_violation = action_policy_violation(actions)
			if policy_violation:
				reason = f'Harness replaced invalid action before execution: {policy_violation} Action: {signature[:500]}'
				repeat_state['reason'] = reason
				if signature not in forbidden_actions:
					forbidden_actions.append(signature)
				apply_recovery(current_url, actions, reason, getattr(model_output, 'next_goal', None), model_output, policy_violation)
				print(f'[harness-recover] {reason}', flush=True)
				return

			if signature in forbidden_actions:
				reason = f'Harness replaced forbidden repeated action before execution: {signature[:500]}'
				repeat_state['reason'] = reason
				apply_recovery(current_url, actions, reason, getattr(model_output, 'next_goal', None), model_output)
				print(f'[harness-recover] {reason}', flush=True)
				return

			if signature == repeat_state['last_signature']:
				repeat_state['count'] += 1
			else:
				repeat_state['last_signature'] = signature
				repeat_state['count'] = 1

			if repeat_state['count'] >= MAX_IDENTICAL_ACTIONS:
				reason = (
					f'Harness replaced {repeat_state["count"]} identical action outputs before execution: '
					f'{signature[:500]}'
				)
				repeat_state['reason'] = reason
				if signature not in forbidden_actions:
					forbidden_actions.append(signature)
				apply_recovery(current_url, actions, reason, getattr(model_output, 'next_goal', None), model_output)
				print(f'[harness-recover] {reason}', flush=True)
				return

		enriched = build_enriched_task(
			task=task,
			strategy_instructions=strategy_instructions,
			plan=plan,
			recovery_plan=last_recovery_plan,
			attempt=attempt,
			feedback=last_feedback,
			history_log=last_history_log,
			forbidden_actions=forbidden_actions,
		)

		browser_profile = BrowserProfile(
			user_data_dir=Path(__file__).parent / '.browser-profile',
		)
		agent = Agent(
			task=enriched,
			llm=llm,
			browser_profile=browser_profile,
			use_vision='auto' if config.get('use_vision', True) else False,
			message_compaction=no_compaction,
			directly_open_url=False,
			max_failures=3,
			register_new_step_callback=guard_model_actions,
			planning_replan_on_stall=2,
			planning_exploration_limit=3,
			loop_detection_window=8,
		)
		history: AgentHistoryList = agent.run_sync(max_steps=effective_max_steps)

		attempt_result: str | None = None
		if hasattr(history, 'final_result'):
			attempt_result = history.final_result()

		if attempt_result and attempt_result.strip():
			best_result = attempt_result

		save_history_to_disk(history, attempt, task, attempt_result)
		last_history_log = format_history_as_log(history, attempt)

		# Save partial results if agent never produced a proper done() output
		if not attempt_result or 'HARNESS FORCED STOP' in attempt_result:
			save_partial_results(history, attempt)

		agent_success = history.is_successful() if hasattr(history, 'is_successful') else None
		judge_verdict = history.is_validated() if hasattr(history, 'is_validated') else None
		judgement = history.judgement() if hasattr(history, 'judgement') else None

		if agent_success is False:
			is_adequate = False
			feedback = 'The agent called done with success=false.'
		elif judge_verdict is False:
			is_adequate = False
			if isinstance(judgement, dict) and judgement.get('failure_reason'):
				feedback = str(judgement['failure_reason'])
			else:
				feedback = 'The judge failed this run.'
		else:
			is_adequate, feedback = evaluate_result(task, attempt_result or '', model)
			if not is_adequate and repeat_state['reason']:
				feedback = f'{feedback}\nHarness recovery note: {repeat_state["reason"]}'

		if is_adequate:
			best_adequate = True
			print(f'[worker] Result accepted on attempt {attempt}.', flush=True)
			break

		if attempt >= effective_max_attempts:
			last_feedback = feedback
			print('[worker] Max attempts reached.', flush=True)
			break

		last_feedback = feedback
		last_recovery_plan = generate_recovery_plan(
			task=task,
			history_log=last_history_log,
			feedback=last_feedback,
			forbidden_actions=forbidden_actions,
			model=model,
		)
		print(f'[worker] Attempt {attempt} insufficient - retrying with full history context.', flush=True)

	print(FINAL_START, flush=True)
	if best_result:
		print(best_result, flush=True)
	elif last_feedback:
		print(f'Task failed: {last_feedback}', flush=True)
	else:
		print('Task failed. See logs for details.', flush=True)
	print(FINAL_END, flush=True)

	if not best_adequate:
		print(f'[worker] Final result inadequate: {last_feedback or "no adequate result"}', flush=True)
		return 11
	return 0


if __name__ == '__main__':
	try:
		raise SystemExit(main())
	except KeyboardInterrupt:
		print('Worker interrupted.', flush=True)
		raise SystemExit(130)
	except Exception:
		traceback.print_exc()
		raise SystemExit(1)

"""Stub of core.provider — only what queue_merge / media_recognize import."""


class LLMRequest:
    def __init__(self, messages=None, tool_set=None, **kw):
        self.messages = messages or []
        self.tool_set = tool_set
        self.user_prompt = []
        self.system_prompt = []
        self.tools = []
        self.tool_choice = "auto"


class LLMResponse:
    def __init__(self, text_response="", tool_calls=None, **kw):
        self.text_response = text_response
        self.reasoning_content = ""
        self.tool_calls = tool_calls or []
        self.tool_results = []
        self.agent_step_index = None
        self.input_tokens = 0
        self.output_tokens = 0
        self.cached_tokens = None
        self.time_consumed = 0.0

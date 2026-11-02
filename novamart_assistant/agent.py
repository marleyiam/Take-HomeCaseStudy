from google.adk.agents import Agent
from google.genai import types

from . import config
from .prompts import build_instruction, build_ticket_instruction
from .tools import (
    check_return_eligibility,
    create_escalation_ticket,
    get_order_status,
    search_policies,
    submit_draft,
)

TOOLS = [search_policies, get_order_status, check_return_eligibility, create_escalation_ticket]

root_agent = Agent(
    name="novamart_assist",
    model=config.MODEL,
    description="Internal assistant for NovaMart store employees: policies, orders and return decisions.",
    # A callable instruction is re-evaluated each turn (keeps "today" correct) and skips ADK's {state} templating.
    instruction=build_instruction,
    tools=TOOLS,
    generate_content_config=types.GenerateContentConfig(temperature=config.TEMPERATURE),
)

# Same knowledge and tools, but drafts ticket replies for an ops-team reviewer instead of chatting live.
ticket_agent = Agent(
    name="novamart_ticket_desk",
    model=config.MODEL,
    description="Drafts replies to store tickets in the operations queue for a human reviewer to approve.",
    instruction=build_ticket_instruction,
    tools=[*TOOLS, submit_draft],
    generate_content_config=types.GenerateContentConfig(temperature=config.TEMPERATURE),
)

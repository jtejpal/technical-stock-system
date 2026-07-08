import asyncio
import os
from pathlib import Path
from typing import Annotated, Sequence

from dotenv import load_dotenv
from typing_extensions import TypedDict

from langchain_core.messages import BaseMessage
from langchain_openai import ChatOpenAI
from langchain_mcp_adapters.client import MultiServerMCPClient
from langgraph.graph import StateGraph, START, END
from langgraph.graph.message import add_messages
from langgraph.prebuilt import ToolNode, tools_condition

from email_sender import send_html_email
from validators import validate_technical_output, validate_html_output

load_dotenv()

MAX_RETRIES = 2  # per node, before we pass through with a flagged warning


class AgentState(TypedDict):
    messages: Annotated[Sequence[BaseMessage], add_messages]
    technical_output: str
    html_output: str
    email_sent: bool
    technical_valid: bool
    technical_validation_errors: list
    technical_retries: int
    design_valid: bool
    design_validation_errors: list
    design_retries: int


async def main():
    client = MultiServerMCPClient({
        "technical": {
            "transport": "stdio",
            "command": "python3",
            "args": ["mcp/technical-mcp.py"]
        }
    })

    tools = await client.get_tools()

    design_agent_system_prompt = Path("agents/design-agent.md").read_text()
    technical_agent_system_prompt = Path("agents/technical-agent.md").read_text()

    llm = ChatOpenAI(
        model="gpt-5.4-nano-2026-03-17",
        api_key=os.getenv("OPENAI_API_KEY")
    )

    llm_with_tools = llm.bind_tools(tools)

    def technical_agent(state: AgentState):
        messages = state["messages"]
        response = llm_with_tools.invoke([("system", technical_agent_system_prompt)] + list(messages))
        return {"messages": [response], "technical_output": response.content}

    def validate_technical(state: AgentState):
        valid, errors, warnings = validate_technical_output(state["technical_output"])
        for w in warnings:
            print(f"[WARN][technical] {w}")
        if errors:
            print(f"[VALIDATION FAILED][technical] {len(errors)} error(s):")
            for e in errors:
                print(f"  - {e}")
        return {"technical_valid": valid, "technical_validation_errors": errors}

    def route_after_technical_validation(state: AgentState):
        if state.get("technical_valid"):
            return "design_agent"
        if state.get("technical_retries", 0) >= MAX_RETRIES:
            print("[WARN] technical output still invalid after max retries -- proceeding to design anyway")
            return "design_agent"
        return "technical_retry_feedback"

    def technical_retry_feedback(state: AgentState):
        errors = state.get("technical_validation_errors", [])
        feedback = (
            "Your previous JSON output failed validation:\n"
            + "\n".join(f"- {e}" for e in errors)
            + "\n\nFix these issues and re-output the full corrected JSON object, "
              "following the schema and rules exactly."
        )
        return {
            "messages": [("user", feedback)],
            "technical_retries": state.get("technical_retries", 0) + 1,
        }

    def design_agent(state: AgentState):
        technical_text = state["technical_output"]
        design_errors = state.get("design_validation_errors", [])

        if design_errors:
            prior_html = state.get("html_output", "")
            user_msg = (
                f"{technical_text}\n\n---\n"
                "Your previous HTML output failed the email-safety self-check:\n"
                + "\n".join(f"- {e}" for e in design_errors)
                + f"\n\nHere is your previous output to revise:\n{prior_html}\n\n"
                "Fix these specific issues and re-output the full corrected HTML snippet."
            )
        else:
            user_msg = technical_text

        response = llm.invoke([
            ("system", design_agent_system_prompt),
            ("user", user_msg),
        ])
        return {"messages": [response], "html_output": response.content}

    def validate_html(state: AgentState):
        valid, errors, warnings = validate_html_output(state["html_output"])
        for w in warnings:
            print(f"[WARN][design] {w}")
        if errors:
            print(f"[VALIDATION FAILED][design] {len(errors)} error(s):")
            for e in errors:
                print(f"  - {e}")
        return {"design_valid": valid, "design_validation_errors": errors}

    def route_after_design_validation(state: AgentState):
        if state.get("design_valid"):
            return "send_email"
        if state.get("design_retries", 0) >= MAX_RETRIES:
            print("[WARN] HTML output still invalid after max retries -- sending with a flagged warning")
            return "send_email"
        return "design_retry_increment"

    def design_retry_increment(state: AgentState):
        return {"design_retries": state.get("design_retries", 0) + 1}

    def send_email(state: AgentState):
        subject = "Stock Analysis"
        html = state["html_output"]
        if not state.get("design_valid", True):
            subject = "\u26a0\ufe0f [Unvalidated] " + subject
            html = (
                '<div style="background-color:#FCEBEB;color:#791F1F;padding:12px;'
                'font-family:Arial,Helvetica,sans-serif;font-size:13px;">'
                "Note: this report did not pass automated formatting validation "
                "and may render incorrectly in some email clients."
                "</div>" + html
            )
        send_html_email(html, "jaitejpal02@gmail.com", subject)
        return {"email_sent": True}

    # --------------------------------- graph -------------------------------------

    builder = StateGraph(AgentState)

    builder.add_node("technical_agent", technical_agent)
    builder.add_node("technical_agent_tools", ToolNode(tools))
    builder.add_node("validate_technical", validate_technical)
    builder.add_node("technical_retry_feedback", technical_retry_feedback)

    builder.add_node("design_agent", design_agent)
    builder.add_node("validate_html", validate_html)
    builder.add_node("design_retry_increment", design_retry_increment)
    builder.add_node("send_email", send_email)

    builder.add_edge(START, "technical_agent")

    builder.add_conditional_edges("technical_agent", tools_condition, {
        "tools": "technical_agent_tools",
        "__end__": "validate_technical",
    })
    builder.add_edge("technical_agent_tools", "technical_agent")

    builder.add_conditional_edges("validate_technical", route_after_technical_validation, {
        "design_agent": "design_agent",
        "technical_retry_feedback": "technical_retry_feedback",
    })
    builder.add_edge("technical_retry_feedback", "technical_agent")

    builder.add_edge("design_agent", "validate_html")
    builder.add_conditional_edges("validate_html", route_after_design_validation, {
        "send_email": "send_email",
        "design_retry_increment": "design_retry_increment",
    })
    builder.add_edge("design_retry_increment", "design_agent")

    builder.add_edge("send_email", END)

    graph = builder.compile()

    print(graph.get_graph().draw_mermaid())

    inputs = {"messages": [("user", "EVR")]}
    # async for chunk in graph.astream(inputs, stream_mode="values"):
    #     last_msg = chunk["messages"][-1]
    #     print(f"[{last_msg.type.upper()}]: {last_msg.content}\n")


if __name__ == "__main__":
    asyncio.run(main())
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

load_dotenv()

class AgentState(TypedDict):
    messages: Annotated[Sequence[BaseMessage], add_messages]
    technical_output: str
    html_output: str
    reviewed_html: bool
    email_body: str
    email_sent: bool


async def main():
    client = MultiServerMCPClient({
        "technical": {
            "transport": "stdio",
            "command": "python",
            "args": ["mcp/technical-mcp.py"]
        }
    })

    tools = await client.get_tools()

    design_agent_system_prompt = Path("agents/design-agent-v1.md").read_text()
    technical_agent_system_prompt = Path("agents/technical-agent-v1.md").read_text()

    llm = ChatOpenAI(
        model="gpt-5.4-nano-2026-03-17",
        api_key=os.getenv("OPENAI_API_KEY")
    )

    llm_with_tools = llm.bind_tools(tools)

    def technical_agent(state: AgentState):
        messages = state["messages"]
        response = llm_with_tools.invoke([("system", technical_agent_system_prompt)] + list(messages))
        return {"messages": [response],"technical_output": response.content}

    def validator_agent(state: AgentState):
        messages = state["messages"]
        technical_output = state["technical_output"]
        response = llm_with_tools.invoke([("system", technical_agent_system_prompt)] + list(messages))
        return {"messages": [response],"technical_output": response.content}

    def design_agent(state: AgentState):
        technical_text = state["messages"][-1].content
        response = llm.invoke([
            ("system", design_agent_system_prompt),
            ("user", technical_text),
        ])
        send_html_email(response.content, "jaitejpal02@gmail.com", "Stock Analysis")
        return {
            "messages": [response],
            "html_output": response.content
        }

    builder = StateGraph(AgentState)

    builder.add_node("technical_agent", technical_agent)
    builder.add_node("design_agent", design_agent)
    builder.add_node("technical_agent_tools", ToolNode(tools))

    builder.add_edge(START, "technical_agent")

    builder.add_conditional_edges("technical_agent", tools_condition, {
        "tools": "technical_agent_tools",
        "__end__": "design_agent",
    })

    builder.add_edge("technical_agent_tools", "technical_agent")
    builder.add_edge("design_agent", END)

    graph = builder.compile()

    inputs = {"messages": [("user", "EVR")]}
    async for chunk in graph.astream(inputs, stream_mode="values"):
        last_msg = chunk["messages"][-1]
        print(f"[{last_msg.type.upper()}]: {last_msg.content}\n")


if __name__ == "__main__":
    asyncio.run(main())
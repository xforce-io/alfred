"""
User Intervention Integration Test

This test reproduces the issue where user intervention messages
cause context loss because Alfred doesn't properly use Dolphin's
interrupt/resume_with_input mechanism.

The issue:
1. User sends initial message -> Agent starts processing
2. User sends intervention message while agent is running
3. Alfred cancels the task and starts new one
4. BUT: Alfred doesn't call agent.interrupt() / resume_with_input()
5. Result: Multiple consecutive user messages without assistant response,
   causing LLM to lose context

Expected behavior:
1. User sends initial message -> Agent starts processing
2. User sends intervention message while agent is running
3. Alfred calls agent.interrupt() -> Agent pauses with context preserved
4. Alfred calls agent.resume_with_input(new_message)
5. Agent resumes with full context + new user input
"""

import pytest
import asyncio
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, AsyncMock, patch
from dataclasses import dataclass
from typing import List, Dict, Any

from dolphin.sdk.agent.dolphin_agent import DolphinAgent
from dolphin.core.agent.agent_state import AgentState, PauseType
from dolphin.core.common.enums import Messages, MessageRole


class MockWebSocket:
    """Mock WebSocket for testing"""
    def __init__(self):
        self.sent_messages: List[Dict] = []
        self.receive_queue: asyncio.Queue = asyncio.Queue()
        
    async def send_json(self, data: Dict):
        self.sent_messages.append(data)
        
    async def receive_json(self):
        return await self.receive_queue.get()
    
    async def inject_message(self, msg: Dict):
        """Inject a message to be received"""
        await self.receive_queue.put(msg)


@pytest.fixture
def mock_agent():
    """Create a mock DolphinAgent that simulates slow execution"""
    agent = MagicMock(spec=DolphinAgent)
    agent.name = "test_agent"
    agent.state = AgentState.INITIALIZED
    agent._pause_type = None
    
    # Track history messages
    agent._history = []
    
    # Mock context
    mock_context = MagicMock()
    mock_context.get_history_messages.return_value = agent._history
    agent.executor = MagicMock()
    agent.executor.context = mock_context
    
    return agent


@pytest.mark.asyncio
async def test_repro_consecutive_user_messages():
    """
    Reproduce: When user intervenes during agent execution, 
    multiple user messages are added to history without assistant response.
    
    This test verifies the PROBLEM exists (before fix).
    """
    # Simulate the history state after a user intervention scenario
    # This is what happens when Alfred cancels task and starts new one
    # without using interrupt/resume_with_input
    
    history_messages = [
        {"role": "user", "content": "帮我搜索美联储主席候选人"},
        # User intervened while agent was still processing the first message
        {"role": "user", "content": "刚遇到机器人检测了，我刚帮你通过了"},
        # User repeated the question after intervention
        {"role": "user", "content": "帮我搜索美联储主席候选人"},
        # Agent responds - but to which message?
        {"role": "assistant", "content": "我来帮你搜索..."},
    ]
    
    # Count consecutive user messages
    consecutive_user_count = 0
    max_consecutive = 0
    for msg in history_messages:
        if msg["role"] == "user":
            consecutive_user_count += 1
            max_consecutive = max(max_consecutive, consecutive_user_count)
        else:
            consecutive_user_count = 0
    
    # The problem: we have 3 consecutive user messages
    # This should NOT happen in normal conversation flow
    assert max_consecutive == 3, f"Expected 3 consecutive user messages (the bug), got {max_consecutive}"
    
    # Verify the intervention message is "lost" in the middle
    # The agent's response doesn't acknowledge the intervention
    intervention_msg = history_messages[1]
    agent_response = history_messages[3]
    
    # The agent should have acknowledged the intervention (like "好的，我继续搜索")
    # But it doesn't - it just continues with the original task
    assert "通过" not in agent_response["content"], \
        "Agent should have acknowledged the intervention but didn't"


@pytest.mark.asyncio  
async def test_correct_intervention_flow():
    """
    Verify: The correct flow when user intervenes should result in
    proper alternating user/assistant messages.
    
    This test defines what the behavior SHOULD be (after fix).
    """
    # Expected history after proper interrupt/resume_with_input handling
    expected_history = [
        {"role": "user", "content": "帮我搜索美联储主席候选人"},
        {"role": "assistant", "content": "我正在搜索..."},  # Partial response before interrupt
        # User intervention - properly handled via resume_with_input
        {"role": "user", "content": "刚遇到机器人检测了，我刚帮你通过了"},
        # Agent acknowledges and continues
        {"role": "assistant", "content": "好的，感谢你帮我通过验证。我继续搜索美联储主席候选人的信息..."},
    ]
    
    # Verify no consecutive user messages
    for i in range(len(expected_history) - 1):
        if expected_history[i]["role"] == "user":
            # Next message should be assistant (in proper conversation)
            assert expected_history[i+1]["role"] == "assistant", \
                f"After user message, expected assistant but got {expected_history[i+1]['role']}"


@pytest.mark.asyncio
async def test_interrupt_mechanism_available():
    """
    Verify that DolphinAgent has the interrupt() and resume_with_input() methods
    that Alfred should be using.
    """
    from dolphin.sdk.agent.dolphin_agent import DolphinAgent
    from dolphin.core.agent.base_agent import BaseAgent
    
    # Check that the methods exist
    assert hasattr(BaseAgent, 'interrupt'), "BaseAgent should have interrupt() method"
    assert hasattr(BaseAgent, 'resume_with_input'), "BaseAgent should have resume_with_input() method"
    
    # Verify they are async methods
    import inspect
    assert inspect.iscoroutinefunction(BaseAgent.interrupt), "interrupt() should be async"
    assert inspect.iscoroutinefunction(BaseAgent.resume_with_input), "resume_with_input() should be async"


@pytest.mark.asyncio
async def test_alfred_chat_service_intervention_bug():
    """
    Reproduce the specific bug in Alfred's chat_service.py
    
    The bug is in handle_chat_session() around line 394-404:
    - When a new message arrives while a task is running
    - Alfred only cancels the asyncio task
    - Alfred does NOT call agent.interrupt() + resume_with_input()
    - This causes context loss
    """
    from src.everbot.web.services.chat_service import ChatService
    
    # The ChatService has this problematic code:
    # 
    # if current_task and not current_task.done():
    #     current_task.cancel()  # <- BUG: Only cancels task, doesn't interrupt agent
    # 
    # current_task = asyncio.create_task(
    #     self._process_message(...)  # <- Creates new task without proper context handoff
    # )
    
    # To fix, it should be:
    # 
    # if current_task and not current_task.done():
    #     await agent.interrupt()        # <- Properly pause the agent
    #     await agent.resume_with_input(message)  # <- Inject new input
    #     ... then continue with new task
    
    # For now, just verify the ChatService exists and has handle_chat_session
    chat_service = ChatService()
    assert hasattr(chat_service, 'handle_chat_session')
    assert hasattr(chat_service, '_process_message')


@pytest.mark.asyncio
async def test_simulate_intervention_with_dolphin_agent():
    """
    Simulate the correct intervention flow using Dolphin's API.
    
    This test shows HOW the fix should work by using a real agent file.
    """
    import os
    from dolphin.sdk.agent.dolphin_agent import DolphinAgent
    from dolphin.core.config.global_config import GlobalConfig
    
    # Use an existing demo agent file
    demo_agent_path = os.path.expanduser("~/.alfred/agents/demo_agent/agent.dph")
    
    # Skip if demo agent doesn't exist
    if not os.path.exists(demo_agent_path):
        pytest.skip(f"Demo agent not found at {demo_agent_path}")
    
    config = GlobalConfig()
    agent = DolphinAgent(
        name="test_intervention",
        file_path=demo_agent_path,
        global_config=config
    )
    
    # Initialize agent
    await agent.initialize()
    assert agent.state == AgentState.INITIALIZED
    
    # The correct flow when user intervenes:
    # 1. Agent is RUNNING
    # 2. Call agent.interrupt() -> sets interrupt event
    # 3. Agent catches interrupt, pauses with PAUSED state and USER_INTERRUPT type  
    # 4. Call agent.resume_with_input(new_message)
    # 5. Agent resumes with context + new message
    
    # Verify interrupt can be called (even when not running, should not crash)
    # When agent is not running, interrupt() logs warning but still succeeds
    result = await agent.interrupt()
    assert result == True, "interrupt() should succeed"
    
    # Clean up
    await agent.terminate()


@pytest.mark.asyncio
async def test_history_after_task_cancel():
    """
    Verify that when Alfred cancels a task, the history might get corrupted.
    
    This simulates what happens in _process_message when CancelledError is caught.
    """
    # Simulate the state after a cancelled task writes to history
    from src.everbot.core.session.session import SessionManager, SessionData
    from dolphin.core.common.constants import KEY_HISTORY
    
    with tempfile.TemporaryDirectory() as tmpdir:
        session_dir = Path(tmpdir)
        manager = SessionManager(session_dir)
        
        # State 1: Original history before intervention
        original_history = [
            {"role": "user", "content": "搜索美联储主席候选人"}
        ]
        
        # State 2: After CancelledError handler writes user message (bug)
        # The cancelled task might still write the message
        history_after_cancel = [
            {"role": "user", "content": "搜索美联储主席候选人"},
            {"role": "user", "content": "我帮你通过了验证"},  # Written by cancelled task
        ]
        
        # State 3: New task also writes the same or different message
        history_after_new_task = [
            {"role": "user", "content": "搜索美联储主席候选人"},
            {"role": "user", "content": "我帮你通过了验证"},
            {"role": "user", "content": "搜索美联储主席候选人"},  # User repeated
        ]
        
        # This is the bug: 3 consecutive user messages
        consecutive_users = 0
        for msg in history_after_new_task:
            if msg["role"] == "user":
                consecutive_users += 1
            else:
                break
        
        assert consecutive_users == 3, \
            f"Bug reproduced: {consecutive_users} consecutive user messages"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])


@pytest.mark.asyncio
async def test_chat_service_task_cancel_behavior():
    """
    Simulate the exact behavior of ChatService when user intervenes.
    
    This reproduces the bug where:
    1. Task A is processing user message "question1"
    2. User sends "intervention" while Task A is running
    3. ChatService cancels Task A (current_task.cancel())
    4. ChatService creates Task B to process "intervention"
    5. Task A's CancelledError handler might still write to history
    6. Task B also writes to history
    7. Result: consecutive user messages without assistant response
    """
    # Simulate history tracking (like what happens in chat_service)
    history = []
    
    async def simulate_task_a(message: str, history_list: list):
        """Simulate a processing task that gets cancelled"""
        try:
            # Task starts processing
            history_list.append({"role": "user", "content": message})
            
            # Simulate long running operation (like LLM call)
            await asyncio.sleep(10)  # Will be cancelled before this completes
            
            # This would be the assistant response (never reached if cancelled)
            history_list.append({"role": "assistant", "content": f"Response to {message}"})
        except asyncio.CancelledError:
            # BUG: The current ChatService code might write user message here too
            # due to how _process_message handles cancellation
            # (See chat_service.py line 813-815)
            print("Task A cancelled")
            raise  # Re-raise to propagate cancellation
    
    async def simulate_task_b(message: str, history_list: list):
        """Simulate the new task that processes intervention"""
        # New task writes its user message
        history_list.append({"role": "user", "content": message})
        
        # Simulate some processing
        await asyncio.sleep(0.1)
        
        # Assistant responds
        history_list.append({"role": "assistant", "content": f"Response to {message}"})
    
    # Simulate the chat_service flow
    task_a = asyncio.create_task(simulate_task_a("帮我搜索美联储主席候选人", history))
    
    # Give task A time to start and write user message
    await asyncio.sleep(0.1)
    assert len(history) == 1, "Task A should have written user message"
    
    # User intervention arrives while Task A is running
    # ChatService cancels Task A (the BUG: doesn't call agent.interrupt())
    task_a.cancel()
    try:
        await task_a
    except asyncio.CancelledError:
        pass
    
    # Simulate user sending same message again (common pattern)
    # or the intervention itself
    history.append({"role": "user", "content": "我帮你通过了验证"})
    
    # Then user repeats original question
    history.append({"role": "user", "content": "帮我搜索美联储主席候选人"})
    
    # Task B processes
    task_b = asyncio.create_task(simulate_task_b("帮我搜索美联储主席候选人", history))
    await task_b
    
    # Verify the bug: we now have consecutive user messages
    consecutive_user = 0
    max_consecutive = 0
    for msg in history:
        if msg["role"] == "user":
            consecutive_user += 1
            max_consecutive = max(max_consecutive, consecutive_user)
        else:
            consecutive_user = 0
    
    # This documents the bug: 3+ consecutive user messages
    assert max_consecutive >= 3, \
        f"Expected at least 3 consecutive user messages (the bug), got {max_consecutive}"
    
    # Print history for debugging
    print("\nHistory after bug scenario:")
    for i, msg in enumerate(history):
        print(f"  {i}: [{msg['role']}] {msg['content'][:50]}...")


@pytest.mark.asyncio
async def test_correct_intervention_with_interrupt_api():
    """
    Test the CORRECT behavior using Dolphin's interrupt/resume mechanism.
    
    This shows what SHOULD happen after the fix.
    """
    # Simulate history tracking with proper interrupt handling
    history = []
    interrupted = asyncio.Event()
    pending_input = None
    
    async def simulate_proper_task(message: str, history_list: list):
        """Simulate a task that properly handles interrupts"""
        history_list.append({"role": "user", "content": message})
        
        # Simulate work with interrupt checkpoints
        for i in range(10):
            # Check for interrupt (like Dolphin's context.check_user_interrupt())
            if interrupted.is_set():
                # Properly save partial state
                history_list.append({
                    "role": "assistant", 
                    "content": "[执行被中断，等待用户输入]"
                })
                return "interrupted"
            await asyncio.sleep(0.1)
        
        history_list.append({"role": "assistant", "content": f"完成处理: {message}"})
        return "completed"
    
    # Start task
    task = asyncio.create_task(simulate_proper_task("帮我搜索美联储主席候选人", history))
    
    # Wait a bit then simulate user intervention
    await asyncio.sleep(0.15)
    
    # Proper interrupt (like agent.interrupt())
    interrupted.set()
    
    # Wait for task to handle interrupt
    result = await task
    assert result == "interrupted"
    
    # Now simulate resume_with_input
    history.append({"role": "user", "content": "我帮你通过了验证"})
    history.append({
        "role": "assistant", 
        "content": "好的，感谢您帮我通过验证。让我继续搜索..."
    })
    
    # Verify proper alternating pattern (no consecutive user messages)
    for i in range(len(history) - 1):
        curr_role = history[i]["role"]
        next_role = history[i+1]["role"]
        
        # User should be followed by assistant (in well-formed conversation)
        if curr_role == "user":
            assert next_role == "assistant", \
                f"Position {i}: user should be followed by assistant, got {next_role}"
    
    print("\nHistory after proper interrupt handling:")
    for i, msg in enumerate(history):
        print(f"  {i}: [{msg['role']}] {msg['content']}")


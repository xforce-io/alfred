#!/usr/bin/env python3
"""Debug script: reproduce the off-by-one bug by simulating context assembly.

Skips agent creation entirely - directly manipulates Dolphin context objects
to see what messages the LLM would receive.
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, "/Users/xupeng/dev/github/dolphin/src")

from dolphin.core.common.constants import KEY_HISTORY
from dolphin.core.common.enums import Messages, MessageRole
from dolphin.core.context.context import Context
from dolphin.core.context_engineer.config.settings import BuildInBucket
from dolphin.core.context_engineer.core.context_manager import ContextManager


def main():
    # 1. Load session data
    session_path = Path.home() / ".alfred/sessions/tg_session_demo_agent__8576399597.json"
    with open(session_path) as f:
        session_raw = json.load(f)

    history_messages = session_raw.get("history_messages", [])
    print(f"Total history messages from disk: {len(history_messages)}")

    # Show user messages
    user_indices = [i for i, m in enumerate(history_messages) if m.get("role") == "user"]
    print(f"User message indices: {user_indices}")
    for idx in user_indices:
        content = str(history_messages[idx].get("content", ""))[:80]
        print(f"  [{idx}] {content}")

    # 2. Create a minimal Context with ContextManager
    ctx = Context()

    # 3. Import history (simulating DolphinStateAdapter.import_session_state)
    msgs = Messages()
    msgs.extend_plain_messages(history_messages)
    ctx.set_history_bucket(msgs)
    ctx.set_variable(KEY_HISTORY, msgs.get_messages_as_dict())

    print(f"\nAfter import_session_state:")
    key_hist = ctx.get_var_value(KEY_HISTORY)
    if isinstance(key_hist, list):
        print(f"  KEY_HISTORY: {len(key_hist)} messages")
        user_in_hist = [m for m in key_hist if m.get("role") == "user"]
        if user_in_hist:
            print(f"  Last user in KEY_HISTORY: {str(user_in_hist[-1].get('content', ''))[:80]}")
    print(f"  Buckets: {list(ctx.context_manager.state.buckets.keys())}")

    # 4. Simulate continue_exploration setup
    test_message = "这是测试消息ABC"
    print(f"\n--- Simulating continue_exploration(content='{test_message}') ---")

    # Step 1: reset_for_block
    ctx.reset_for_block()
    print(f"\nAfter reset_for_block:")
    print(f"  Buckets: {list(ctx.context_manager.state.buckets.keys())}")

    # Step 2: setup system bucket
    ctx.add_bucket(
        BuildInBucket.SYSTEM.value,
        "You are a helpful assistant.",
        message_role=MessageRole.SYSTEM,
    )

    # Step 3: add user message to QUERY
    ctx.add_user_message(test_message, bucket=BuildInBucket.QUERY.value)
    print(f"After add_user_message to QUERY:")
    print(f"  Buckets: {list(ctx.context_manager.state.buckets.keys())}")

    # Check QUERY bucket content
    query_bucket = ctx.context_manager.state.buckets.get(BuildInBucket.QUERY.value)
    if query_bucket:
        content = query_bucket.content
        if isinstance(content, Messages):
            qmsgs = content.get_messages()
            print(f"  QUERY bucket: {len(qmsgs)} messages")
            for qm in qmsgs:
                role = qm.role.value if hasattr(qm.role, 'value') else str(qm.role)
                print(f"    {role}: {str(qm.content)[:80]}")
        else:
            print(f"  QUERY bucket content (str): {str(content)[:80]}")

    # Step 4: make history messages (read from KEY_HISTORY with projection)
    history_result = ctx.get_history_messages(projected=True)
    if history_result:
        h_msgs = history_result.get_messages()
        print(f"\nget_history_messages(projected=True): {len(h_msgs)} messages")
        user_in_projected = [m for m in h_msgs if m.role == MessageRole.USER]
        print(f"  User messages: {len(user_in_projected)}")
        if user_in_projected:
            last = user_in_projected[-1]
            print(f"  Last user: {str(last.content)[:100]}")

    # Step 5: set history bucket
    if history_result and not history_result.empty():
        ctx.set_history_bucket(history_result)

    # 5. Assemble LLM messages
    print(f"\n--- Assembling LLM messages ---")
    print(f"Final buckets: {list(ctx.context_manager.state.buckets.keys())}")
    print(f"Bucket order: {ctx.context_manager.state.bucket_order}")

    llm_messages = ctx.context_manager.to_dph_messages()
    all_msgs = llm_messages.get_messages()
    print(f"Total LLM messages: {len(all_msgs)}")

    # Show role distribution
    roles = {}
    for m in all_msgs:
        r = m.role.value if hasattr(m.role, 'value') else str(m.role)
        roles[r] = roles.get(r, 0) + 1
    print(f"Role distribution: {roles}")

    # Show first 3 and last 5 messages
    print(f"\n--- First 3 messages ---")
    for i in range(min(3, len(all_msgs))):
        msg = all_msgs[i]
        role = msg.role.value if hasattr(msg.role, 'value') else str(msg.role)
        content = str(msg.content)[:120].replace('\n', ' ')
        print(f"  [{i}] {role}: {content}")

    print(f"\n--- Last 5 messages ---")
    for i in range(max(0, len(all_msgs) - 5), len(all_msgs)):
        msg = all_msgs[i]
        role = msg.role.value if hasattr(msg.role, 'value') else str(msg.role)
        content = str(msg.content)[:200].replace('\n', ' ')
        tc = hasattr(msg, 'tool_calls') and msg.tool_calls
        print(f"  [{i}] {role}{'(tc)' if tc else ''}: {content}")

    # Check: is the last message our test message?
    last_msg = all_msgs[-1] if all_msgs else None
    if last_msg:
        last_content = str(last_msg.content)
        last_role = last_msg.role.value if hasattr(last_msg.role, 'value') else str(last_msg.role)
        if last_role == "user" and test_message in last_content:
            print(f"\n✅ CORRECT: Last message IS our test message")
        else:
            print(f"\n❌ BUG: Last message is NOT our test message!")
            print(f"   Last msg role={last_role}, content={last_content[:100]}")
            # Find where our test message ended up
            for i, m in enumerate(all_msgs):
                if test_message in str(m.content):
                    print(f"   Test message found at index {i} (role={m.role.value if hasattr(m.role, 'value') else m.role})")


if __name__ == "__main__":
    main()

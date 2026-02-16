
import asyncio
from pathlib import Path
import sys

# Add src to path
sys.path.append("/Users/xupeng/lab/alfred/src")

async def test_api():
    try:
        from everbot.agent_factory import create_agent
        from everbot.user_data import UserDataManager
        
        user_data = UserDataManager()
        agent_name = "demo_agent"
        agent_dir = user_data.get_agent_dir(agent_name)
        
        print(f"Creating agent: {agent_name} at {agent_dir}")
        agent = await create_agent(agent_name, agent_dir)
        context = agent.executor.context
        
        # Test 1: Check existing history
        history = context.get_history_messages(normalize=True)
        print(f"Initial history size: {len(history)}")
        
        # Test 2: Try to call set_history_messages (should fail)
        try:
            print("Checking context.clear_history...")
            print(f"clear_history exists: {hasattr(context, 'clear_history')}")
            print("Trying context.set_history_messages...")
            context.set_history_messages([])
            print("SUCCESS (Unexpectedly!)")
        except AttributeError:
            print("FAILED as expected: Context has no attribute 'set_history_messages'")
            
        # Test 3: Try set_variable("history", ...)
        print("Trying context.set_variable('history', ...)")
        test_history = [{"role": "user", "content": "hello test"}]
        context.set_variable("history", test_history)
        
        new_history = context.get_history_messages(normalize=True)
        print(f"New history size: {len(new_history)}")
        if len(new_history) > 0:
            print(f"New history first message: {new_history[0].content}")
            
        # Test 4: Check if messages are correctly set via bucket
        print("Checking history bucket...")
        # In Dolphin, history is often managed via a bucket that is synchronized with the variable pool
        
    except Exception as e:
        print(f"Error during test: {e}")
        import traceback
        traceback.print_exc()

if __name__ == "__main__":
    asyncio.run(test_api())

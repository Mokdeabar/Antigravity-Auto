"""
Tests for A2A Protocol (V74, Audit §4.8)
"""

import sys
import os
import asyncio
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


def run_tests():
    passed = 0
    failed = 0
    total = 0

    def test(name, condition):
        nonlocal passed, failed, total
        total += 1
        if condition:
            passed += 1
            print(f"  ✅ {name} PASSED")
        else:
            failed += 1
            print(f"  ❌ {name} FAILED")

    print("\n🧪 Running A2A Protocol Unit Tests...\n")

    from supervisor.a2a_protocol import (
        MessageType, AgentRole, Priority,
        AgentCard, A2AMessage, TaskAssignment,
        AgentRegistry, A2ARouter, A2ADispatcher,
        DEFAULT_AGENTS, create_default_registry,
    )

    # ── Enums ──
    test("test_message_types", MessageType.REQUEST == "request" and MessageType.RESPONSE == "response")
    test("test_agent_roles", AgentRole.DEBUGGER == "debugger" and AgentRole.ARCHITECT == "architect")
    test("test_priorities", Priority.CRITICAL == "critical" and Priority.LOW == "low")

    # ── AgentCard ──
    card = AgentCard()
    test("test_card_defaults", card.agent_id == "" and card.context_limit == 200000)

    card2 = AgentCard(
        agent_id="test-1", role="debugger",
        capabilities=["debugging", "error_analysis"],
    )
    test("test_card_supports", card2.supports("debugging") and card2.supports("DEBUGGING"))
    test("test_card_not_supports", not card2.supports("refactoring"))

    d = card2.to_dict()
    test("test_card_to_dict", d["agent_id"] == "test-1" and d["role"] == "debugger")

    # ── A2AMessage ──
    msg = A2AMessage(sender="a", recipient="b", payload={"task": "fix"})
    test("test_msg_auto_id", len(msg.message_id) == 16)
    test("test_msg_auto_timestamp", msg.timestamp > 0)
    test("test_msg_auto_conversation", msg.conversation_id == msg.message_id)
    test("test_msg_not_expired", not msg.expired)

    old_msg = A2AMessage(sender="a", recipient="b", timestamp=1.0, ttl_s=1)
    test("test_msg_expired", old_msg.expired)

    d2 = msg.to_dict()
    test("test_msg_to_dict", "message_id" in d2 and d2["sender"] == "a")

    reply = msg.reply({"result": "ok"})
    test("test_msg_reply", reply.sender == "b" and reply.recipient == "a")
    test("test_msg_reply_correlation", reply.conversation_id == msg.conversation_id)
    test("test_msg_reply_type", reply.msg_type == "response")

    # ── TaskAssignment ──
    task = TaskAssignment()
    test("test_task_defaults", task.status == "pending" and task.duration_s == 0.0)

    task2 = TaskAssignment(started_at=100.0, completed_at=105.5)
    test("test_task_duration", abs(task2.duration_s - 5.5) < 0.01)

    d3 = task2.to_dict()
    test("test_task_to_dict", "task_id" in d3 and "duration_s" in d3)

    # ── AgentRegistry ──
    reg = AgentRegistry()
    test("test_registry_empty", reg.count == 0)

    reg.register(AgentCard(agent_id="dbg-1", role="debugger", capabilities=["debugging"]))
    reg.register(AgentCard(agent_id="arch-1", role="architect", capabilities=["design"]))
    test("test_registry_count", reg.count == 2)

    test("test_registry_get", reg.get("dbg-1") is not None and reg.get("dbg-1").role == "debugger")
    test("test_registry_get_none", reg.get("nonexistent") is None)

    test("test_registry_find_role", len(reg.find_by_role("debugger")) == 1)
    test("test_registry_find_capability", len(reg.find_by_capability("design")) == 1)

    test("test_registry_all", len(reg.all_agents()) == 2)
    test("test_registry_unregister", reg.unregister("dbg-1") and reg.count == 1)
    test("test_registry_unregister_missing", not reg.unregister("nonexistent"))

    d4 = reg.to_dict()
    test("test_registry_to_dict", "agents" in d4 and d4["count"] == 1)

    # ── DEFAULT_AGENTS ──
    test("test_default_agents_count", len(DEFAULT_AGENTS) >= 6)
    test("test_default_agents_ids",
         all(a.agent_id for a in DEFAULT_AGENTS))

    # ── create_default_registry ──
    default_reg = create_default_registry()
    test("test_default_registry", default_reg.count >= 6)
    test("test_default_registry_debugger", len(default_reg.find_by_role("debugger")) >= 1)

    # ── A2ARouter ──
    router_reg = AgentRegistry()
    router_reg.register(AgentCard(agent_id="echo-1", role="tester"))
    router = A2ARouter(router_reg)
    test("test_router_init", router.message_count == 0)

    # Register echo handler
    async def echo_handler(msg):
        return msg.reply({"echo": msg.payload.get("data", "")})

    router.register_handler("echo-1", echo_handler)

    # Send message
    loop = asyncio.new_event_loop()
    req = A2AMessage(sender="test", recipient="echo-1", payload={"data": "hello"})
    resp = loop.run_until_complete(router.send(req))
    test("test_router_send", resp is not None and resp.payload.get("echo") == "hello")
    test("test_router_message_count", router.message_count == 1)
    test("test_router_conversation", len(router.get_conversation(req.conversation_id)) == 2)

    # Send to unknown agent
    bad_msg = A2AMessage(sender="test", recipient="nonexistent")
    bad_resp = loop.run_until_complete(router.send(bad_msg))
    test("test_router_unknown_agent", bad_resp is None)

    # ── A2ADispatcher ──
    disp_reg = AgentRegistry()
    disp_reg.register(AgentCard(agent_id="worker-1", role="fixer"))
    disp_router = A2ARouter(disp_reg)

    async def ok_handler(msg):
        return msg.reply({"status": "done"})

    disp_router.register_handler("worker-1", ok_handler)

    dispatcher = A2ADispatcher(disp_router, max_concurrent=2)
    tasks_list = [
        TaskAssignment(task_id="t1", assigned_to="worker-1", description="Fix A"),
        TaskAssignment(task_id="t2", assigned_to="worker-1", description="Fix B"),
    ]
    results = loop.run_until_complete(dispatcher.dispatch_parallel(tasks_list))
    test("test_dispatch_count", len(results) == 2)
    test("test_dispatch_completed", all(r.status == "completed" for r in results))

    stats = dispatcher.get_stats()
    test("test_dispatch_stats", stats["total"] == 2 and stats["success_rate"] == 100.0)

    # Empty dispatch
    empty_results = loop.run_until_complete(dispatcher.dispatch_parallel([]))
    test("test_dispatch_empty", len(empty_results) == 0)

    loop.close()

    # ── Results ──
    print(f"\n{'='*50}")
    print(f"Results: {passed} passed, {failed} failed out of {total} tests")
    if failed == 0:
        print("\n✅ All A2A Protocol tests passed!")
    else:
        print(f"\n❌ {failed} test(s) failed!")
    return failed == 0


if __name__ == "__main__":
    success = run_tests()
    sys.exit(0 if success else 1)

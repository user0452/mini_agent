"""Scenario-oriented acceptance tests for the minimal agent."""

import json
import tempfile
import unittest
from pathlib import Path

from mini_agent import MiniAgent, SessionStore, TOOL_SCHEMAS


class ScriptedClient:
    """Deterministic fake OpenAI-compatible model."""

    def __init__(self, replies):
        self.replies = iter(replies)
        self.requests = []

    def chat(self, messages):
        # JSON round-trip gives each assertion an immutable request snapshot.
        self.requests.append(json.loads(json.dumps(messages, ensure_ascii=False)))
        return next(self.replies)


class EndlessToolClient:
    def __init__(self):
        self.call_count = 0

    def chat(self, messages):
        self.call_count += 1
        return {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": f"call_{self.call_count}",
                    "type": "function",
                    "function": {
                        "name": "calculator",
                        "arguments": '{"expression":"1 + 1"}',
                    },
                }
            ],
        }


class LoopAcceptanceTests(unittest.TestCase):
    def test_direct_answer_finishes_without_tool_loop(self):
        reasoning_events = []
        client = ScriptedClient(
            [
                {
                    "role": "assistant",
                    "reasoning_content": "这个问题不需要工具",
                    "content": "可以直接回答。",
                }
            ]
        )
        with tempfile.TemporaryDirectory() as directory:
            store = SessionStore(Path(directory) / "sessions.db")
            answer = MiniAgent(
                client,
                session_store=store,
                reasoning_callback=reasoning_events.append,
            ).run("你好", "user-a", "window-1")
            persisted = json.dumps(
                store.load_messages("user-a", "window-1"),
                ensure_ascii=False,
            )

        self.assertEqual(answer, "可以直接回答。")
        self.assertEqual(len(client.requests), 1)
        self.assertEqual(reasoning_events[0]["reasoning"], "这个问题不需要工具")
        self.assertNotIn("这个问题不需要工具", persisted)

    def test_all_three_registered_tools_execute_in_one_model_step(self):
        schemas = {schema["function"]["name"]: schema for schema in TOOL_SCHEMAS}
        self.assertEqual(set(schemas), {"calculator", "search", "read_docs"})
        for schema in schemas.values():
            function = schema["function"]
            self.assertTrue(function["description"])
            self.assertEqual(function["parameters"]["type"], "object")
            self.assertTrue(function["parameters"]["required"])

        client = ScriptedClient(
            [
                {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "calc_1",
                            "type": "function",
                            "function": {
                                "name": "calculator",
                                "arguments": '{"expression":"20 / 4"}',
                            },
                        },
                        {
                            "id": "search_1",
                            "type": "function",
                            "function": {
                                "name": "search",
                                "arguments": '{"query":"Python 编程"}',
                            },
                        },
                        {
                            "id": "docs_1",
                            "type": "function",
                            "function": {
                                "name": "read_docs",
                                "arguments": '{"path":"acceptance.md"}',
                            },
                        },
                    ],
                },
                {"role": "assistant", "content": "三个工具都已执行。"},
            ]
        )

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            docs_dir = root / "docs"
            docs_dir.mkdir()
            (docs_dir / "acceptance.md").write_text(
                "这是验收文档。",
                encoding="utf-8",
            )
            store = SessionStore(root / "sessions.db")
            agent = MiniAgent(client, docs_dir=docs_dir, session_store=store)
            answer = agent.run("计算、搜索并读取文档", "user-a", "window-1")
            traces = store.list_tool_traces("user-a", "window-1")

        self.assertEqual(answer, "三个工具都已执行。")
        self.assertEqual(len(client.requests), 2)
        tool_messages = [
            message for message in client.requests[1] if message["role"] == "tool"
        ]
        self.assertEqual(len(tool_messages), 3)
        results = {
            message["tool_call_id"]: json.loads(message["content"])
            for message in tool_messages
        }
        self.assertEqual(results["calc_1"]["result"], 5)
        self.assertEqual(results["search_1"]["result"][0]["title"], "Python")
        self.assertEqual(results["docs_1"]["result"], "这是验收文档。")
        self.assertEqual({trace["status"] for trace in traces}, {"succeeded"})
        self.assertEqual({trace["model_step"] for trace in traces}, {1})
        self.assertEqual(len({trace["trace_id"] for trace in traces}), 1)

    def test_tool_result_can_lead_to_another_tool_call(self):
        client = ScriptedClient(
            [
                {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "calc_first",
                            "type": "function",
                            "function": {
                                "name": "calculator",
                                "arguments": '{"expression":"6 * 7"}',
                            },
                        }
                    ],
                },
                {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "search_second",
                            "type": "function",
                            "function": {
                                "name": "search",
                                "arguments": '{"query":"minimal agent"}',
                            },
                        }
                    ],
                },
                {"role": "assistant", "content": "循环结束。"},
            ]
        )
        with tempfile.TemporaryDirectory() as directory:
            store = SessionStore(Path(directory) / "sessions.db")
            answer = MiniAgent(client, session_store=store).run(
                "先计算再搜索",
                "user-a",
                "window-1",
            )
            traces = store.list_tool_traces("user-a", "window-1")

        self.assertEqual(answer, "循环结束。")
        self.assertEqual(len(client.requests), 3)
        self.assertEqual({trace["model_step"] for trace in traces}, {1, 2})
        self.assertEqual(len({trace["trace_id"] for trace in traces}), 1)

    def test_max_steps_stops_an_endless_tool_loop(self):
        client = EndlessToolClient()
        agent = MiniAgent(client, max_steps=2)

        with self.assertRaisesRegex(RuntimeError, "2 轮内没有生成最终回答"):
            agent.run("永远调用工具")

        self.assertEqual(client.call_count, 2)


class SessionAcceptanceTests(unittest.TestCase):
    def test_tool_followup_uses_only_the_selected_window_history(self):
        with tempfile.TemporaryDirectory() as directory:
            store = SessionStore(Path(directory) / "sessions.db")

            window_one_client = ScriptedClient(
                [
                    {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            {
                                "id": "weather_search",
                                "type": "function",
                                "function": {
                                    "name": "search",
                                    "arguments": '{"query":"上海天气"}',
                                },
                            }
                        ],
                    },
                    {"role": "assistant", "content": "已记住窗口1需要带伞。"},
                ]
            )
            MiniAgent(window_one_client, session_store=store).run(
                "查上海天气，记住带伞",
                "user-a",
                "window-1",
            )

            window_two_client = ScriptedClient(
                [{"role": "assistant", "content": "已记住窗口2周五交周报。"}]
            )
            MiniAgent(window_two_client, session_store=store).run(
                "写周报，记住周五提交",
                "user-a",
                "window-2",
            )

            followup_client = ScriptedClient(
                [
                    {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            {
                                "id": "followup_calc",
                                "type": "function",
                                "function": {
                                    "name": "calculator",
                                    "arguments": '{"expression":"2 + 3"}',
                                },
                            }
                        ],
                    },
                    {"role": "assistant", "content": "窗口1追问完成。"},
                ]
            )
            answer = MiniAgent(followup_client, session_store=store).run(
                "继续刚才的话题，再计算2+3",
                "user-a",
                "window-1",
            )

            first_followup_request = json.dumps(
                followup_client.requests[0],
                ensure_ascii=False,
            )

        self.assertEqual(answer, "窗口1追问完成。")
        self.assertIn("查上海天气", first_followup_request)
        self.assertIn("weather_search", first_followup_request)
        self.assertIn("带伞", first_followup_request)
        self.assertNotIn("周报", first_followup_request)
        self.assertNotIn("周五提交", first_followup_request)


if __name__ == "__main__":
    unittest.main()

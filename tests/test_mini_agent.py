import json
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import call, patch

from mini_agent import (
    MiniAgent,
    OpenAICompatibleClient,
    SessionStore,
    calculator,
    read_docs,
    search,
)


class FakeHTTPResponse:
    def __init__(self, body):
        self.body = json.dumps(body).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return False

    def read(self):
        return self.body


class FakeClient:
    def __init__(self, replies):
        self.replies = iter(replies)
        self.requests = []

    def chat(self, messages):
        self.requests.append(list(messages))
        return next(self.replies)


class SummarizingFakeClient:
    def __init__(self):
        self.requests = []
        self.summary_requests = []

    def chat(self, messages):
        self.requests.append(list(messages))
        return {
            "role": "assistant",
            "content": "已处理。",
            "reasoning_content": "这段模型思考不能进入上下文或数据库",
        }

    def summarize(self, messages):
        self.summary_requests.append(list(messages))
        payload = json.loads(messages[-1]["content"])
        return f"分块摘要({payload['target_range']})"


class ToolTests(unittest.TestCase):
    def test_calculator(self):
        self.assertEqual(calculator("(12 + 8) * 3 / 2"), 30)
        with self.assertRaises(ValueError):
            calculator("__import__('os').getcwd()")

    def test_mock_search(self):
        self.assertEqual(search("Python 编程")[0]["title"], "Python")

    def test_read_docs_cannot_escape_root(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "ok.md").write_text("hello", encoding="utf-8")
            self.assertEqual(read_docs("ok.md", root), "hello")
            with self.assertRaises(ValueError):
                read_docs("../secret.txt", root)


class ApiRetryTests(unittest.TestCase):
    def test_network_errors_use_exponential_backoff_and_jitter(self):
        client = OpenAICompatibleClient(
            "http://localhost:8000/v1",
            "",
            "mock-model",
            max_attempts=3,
            backoff_base=0.5,
        )
        success = FakeHTTPResponse(
            {"choices": [{"message": {"role": "assistant", "content": "ok"}}]}
        )

        with (
            patch(
                "mini_agent.urllib.request.urlopen",
                side_effect=[
                    urllib.error.URLError("连接中断"),
                    TimeoutError("请求超时"),
                    success,
                ],
            ) as urlopen,
            patch("mini_agent.random.uniform", side_effect=[0.1, 0.2]),
            patch("mini_agent.time.sleep") as sleep,
        ):
            response = client.chat([])

        self.assertEqual(response["content"], "ok")
        self.assertEqual(urlopen.call_count, 3)
        self.assertEqual(sleep.call_args_list, [call(0.6), call(1.2)])

    def test_network_error_stops_after_three_total_attempts(self):
        client = OpenAICompatibleClient(
            "http://localhost:8000/v1",
            "",
            "mock-model",
            max_attempts=3,
            backoff_base=0,
        )
        with (
            patch(
                "mini_agent.urllib.request.urlopen",
                side_effect=urllib.error.URLError("持续不可用"),
            ) as urlopen,
            patch("mini_agent.time.sleep"),
        ):
            with self.assertRaisesRegex(RuntimeError, "已尝试 3 次"):
                client.chat([])

        self.assertEqual(urlopen.call_count, 3)


class AgentLoopTests(unittest.TestCase):
    def test_tool_call_loops_back_to_model(self):
        trace_events = []
        reasoning_events = []
        fake = FakeClient(
            [
                {
                    "role": "assistant",
                    "content": None,
                    "reasoning_content": "先计算 6 乘以 7",
                    "tool_calls": [
                        {
                            "id": "call_1",
                            "type": "function",
                            "function": {
                                "name": "calculator",
                                "arguments": '{"expression":"6 * 7"}',
                            },
                        }
                    ],
                },
                {"role": "assistant", "content": "答案是 42。"},
            ]
        )
        with tempfile.TemporaryDirectory() as directory:
            store = SessionStore(Path(directory) / "sessions.db")
            answer = MiniAgent(
                fake,
                session_store=store,
                trace_callback=trace_events.append,
                reasoning_callback=reasoning_events.append,
            ).run("6 乘以 7 是多少？")
            persisted = json.dumps(
                store.load_messages("default-user", "default-session"),
                ensure_ascii=False,
            )
            traces = store.list_tool_traces("default-user", "default-session")

        self.assertEqual(answer, "答案是 42。")
        self.assertEqual(len(fake.requests), 2)
        tool_message = fake.requests[1][-1]
        self.assertEqual(tool_message["role"], "tool")
        self.assertEqual(tool_message["tool_call_id"], "call_1")
        self.assertEqual(json.loads(tool_message["content"])["result"], 42)
        self.assertNotIn(
            "reasoning_content",
            json.dumps(fake.requests[1], ensure_ascii=False),
        )
        self.assertIn("tool_calls", persisted)
        self.assertIn('"role": "tool"', persisted)
        self.assertNotIn("reasoning_content", persisted)
        self.assertEqual(len(traces), 1)
        self.assertEqual(traces[0]["tool_name"], "calculator")
        self.assertEqual(traces[0]["status"], "succeeded")
        self.assertEqual(traces[0]["turn_no"], 1)
        self.assertEqual(traces[0]["model_step"], 1)
        self.assertGreaterEqual(traces[0]["duration_ms"], 0)
        self.assertEqual(
            [event["event"] for event in trace_events],
            ["tool.running", "tool.succeeded"],
        )
        self.assertEqual(len(reasoning_events), 1)
        self.assertEqual(reasoning_events[0]["event"], "model.reasoning")
        self.assertEqual(reasoning_events[0]["reasoning"], "先计算 6 乘以 7")
        self.assertNotIn("先计算 6 乘以 7", json.dumps(traces, ensure_ascii=False))

    def test_think_tags_are_displayed_but_not_persisted(self):
        reasoning_events = []
        fake = FakeClient(
            [
                {
                    "role": "assistant",
                    "content": "<think>先分析用户的问题</think>\n最终答案。",
                }
            ]
        )
        with tempfile.TemporaryDirectory() as directory:
            store = SessionStore(Path(directory) / "sessions.db")
            answer = MiniAgent(
                fake,
                session_store=store,
                reasoning_callback=reasoning_events.append,
            ).run("测试思考展示")
            persisted = json.dumps(
                store.load_messages("default-user", "default-session"),
                ensure_ascii=False,
            )

        self.assertEqual(answer, "最终答案。")
        self.assertEqual(reasoning_events[0]["reasoning"], "先分析用户的问题")
        self.assertNotIn("先分析用户的问题", persisted)
        self.assertNotIn("<think>", persisted)
        self.assertIn("最终答案。", persisted)

    def test_failed_tool_trace_and_secret_redaction(self):
        secret = "secret-value-that-must-not-be-stored"
        fake = FakeClient(
            [
                {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call_bad",
                            "type": "function",
                            "function": {
                                "name": "calculator",
                                "arguments": json.dumps(
                                    {"expression": "1 + 1", "token": secret}
                                ),
                            },
                        }
                    ],
                },
                {"role": "assistant", "content": "工具参数有误。"},
            ]
        )
        with tempfile.TemporaryDirectory() as directory:
            store = SessionStore(Path(directory) / "sessions.db")
            MiniAgent(fake, session_store=store).run("执行错误工具调用")
            traces = store.list_tool_traces("default-user", "default-session")

        self.assertEqual(len(traces), 1)
        self.assertEqual(traces[0]["status"], "failed")
        self.assertEqual(traces[0]["error_type"], "TypeError")
        self.assertIn("***REDACTED***", traces[0]["arguments_json"])
        self.assertNotIn(secret, json.dumps(traces, ensure_ascii=False))


class SessionTests(unittest.TestCase):
    def test_same_user_windows_are_isolated_and_can_resume(self):
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "sessions.db"
            store = SessionStore(db_path)

            first_process = FakeClient(
                [
                    {"role": "assistant", "content": "窗口1：已记住天气待办。"},
                    {"role": "assistant", "content": "窗口2：已记住周报待办。"},
                ]
            )
            agent = MiniAgent(first_process, session_store=store)
            agent.run("查上海天气，并记住买伞", "user-a", "window-1")
            agent.run("帮我写周报，并记住周五提交", "user-a", "window-2")

            # Simulate closing and reopening window 1 with a new process/agent.
            second_process = FakeClient(
                [{"role": "assistant", "content": "你在窗口1让我记住买伞。"}]
            )
            reopened_agent = MiniAgent(
                second_process,
                session_store=SessionStore(db_path),
            )
            answer = reopened_agent.run("我之前让你记住什么？", "user-a", "window-1")

            self.assertEqual(answer, "你在窗口1让我记住买伞。")
            sent_messages = second_process.requests[0]
            sent_text = json.dumps(sent_messages, ensure_ascii=False)
            self.assertIn("买伞", sent_text)
            self.assertNotIn("周报", sent_text)
            self.assertEqual(
                {item["session_id"] for item in store.list_sessions("user-a")},
                {"window-1", "window-2"},
            )

    def test_different_users_with_same_session_id_are_isolated(self):
        with tempfile.TemporaryDirectory() as directory:
            store = SessionStore(Path(directory) / "sessions.db")
            store.save_messages("user-a", "window-1", [{"role": "user", "content": "A"}])
            store.save_messages("user-b", "window-1", [{"role": "user", "content": "B"}])

            self.assertEqual(store.load_messages("user-a", "window-1")[0]["content"], "A")
            self.assertEqual(store.load_messages("user-b", "window-1")[0]["content"], "B")


class ContextCompressionTests(unittest.TestCase):
    def test_incremental_summaries_and_rolling_context(self):
        with tempfile.TemporaryDirectory() as directory:
            store = SessionStore(Path(directory) / "sessions.db")
            client = SummarizingFakeClient()
            agent = MiniAgent(client, session_store=store)

            for turn_no in range(1, 16):
                agent.run(f"用户第{turn_no}轮。", "user-a", "window-1")
            agent.wait_for_background()

            # The 16th request must use summary(1-5) + complete turns 6-15.
            agent.run("用户第16轮。", "user-a", "window-1")
            sixteenth_context = json.dumps(client.requests[15], ensure_ascii=False)
            self.assertIn("第 1-5 轮按顺序拼接的分块摘要", sixteenth_context)
            self.assertNotIn("用户第1轮。", sixteenth_context)
            self.assertNotIn("用户第5轮。", sixteenth_context)
            self.assertIn("用户第6轮。", sixteenth_context)
            self.assertIn("用户第15轮。", sixteenth_context)
            self.assertIn("用户第16轮。", sixteenth_context)

            for turn_no in range(17, 21):
                agent.run(f"用户第{turn_no}轮。", "user-a", "window-1")
            agent.wait_for_background()

            summaries = store.list_summaries("user-a", "window-1")
            self.assertEqual(
                [item["end_turn"] for item in summaries],
                [5, 10, 15, 20],
            )
            compressed_ranges = [
                json.loads(request[-1]["content"])["target_range"]
                for request in client.summary_requests
            ]
            self.assertEqual(len(compressed_ranges), 4)
            self.assertEqual(
                set(compressed_ranges),
                {"1-5", "6-10", "11-15", "16-20"},
            )

            # Each five-turn chunk is compressed once and receives no older summary.
            second_summary_request = next(
                request
                for request in client.summary_requests
                if '"target_range": "6-10"' in request[-1]["content"]
            )
            second_summary_input = json.dumps(second_summary_request, ensure_ascii=False)
            self.assertIn("用户第6轮。", second_summary_input)
            self.assertIn("用户第10轮。", second_summary_input)
            self.assertNotIn("用户第1轮。", second_summary_input)
            self.assertNotIn("分块摘要(1-5)", second_summary_input)

            # At turn 21, summary(1-10) is a plain ordered concatenation of two chunks.
            agent.run("用户第21轮。", "user-a", "window-1")
            twenty_first_context = json.dumps(client.requests[20], ensure_ascii=False)
            first_position = twenty_first_context.index("分块摘要(1-5)")
            second_position = twenty_first_context.index("分块摘要(6-10)")
            self.assertLess(first_position, second_position)
            self.assertNotIn("用户第10轮。", twenty_first_context)
            self.assertIn("用户第11轮。", twenty_first_context)
            self.assertIn("用户第20轮。", twenty_first_context)

            # Raw history is never deleted, and private reasoning is never stored.
            raw_history = json.dumps(
                store.load_messages("user-a", "window-1"),
                ensure_ascii=False,
            )
            self.assertIn("用户第1轮。", raw_history)
            self.assertIn("用户第20轮。", raw_history)
            self.assertNotIn("模型思考", raw_history)
            self.assertEqual(store.get_turn_count("user-a", "window-1"), 21)
            self.assertEqual(
                [(item["start_turn"], item["end_turn"]) for item in summaries],
                [(1, 5), (6, 10), (11, 15), (16, 20)],
            )
            agent.close()


if __name__ == "__main__":
    unittest.main()

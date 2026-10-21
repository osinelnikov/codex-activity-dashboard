import json
import pathlib
import sqlite3
import tempfile
import unittest
import io
import threading
import socket
from unittest.mock import patch

import server


def record(kind, payload, timestamp="2026-10-21T10:00:00Z"):
    return json.dumps({"type": kind, "timestamp": timestamp, "payload": payload}).encode()


class ReaderTests(unittest.TestCase):
    def test_incremental_partial_malformed_and_truncate(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "rollout.jsonl"
            reader = server.RolloutReader(path)
            path.write_bytes(record("session_meta", {"id": "abc", "cwd": "/work", "model_provider": "openai"}) + b"\n{" )
            events = reader.read()
            self.assertEqual([e["kind"] for e in events], ["session_meta"])
            self.assertTrue(reader.pending)
            self.assertFalse(reader.offset >= reader.file_size and not reader.pending)
            with path.open("ab") as f:
                f.write(b'"type":"event_msg","payload":{"type":"task_complete"}}\ninvalid\n')
            self.assertEqual([e["kind"] for e in reader.read()], ["session_meta", "task_complete"])
            path.write_bytes(record("session_meta", {"id": "new"}) + b"\n")
            self.assertEqual([e["session_id"] for e in reader.read()], ["new"])

    def test_sensitive_payloads_are_discarded_and_confirmed_metadata_survives(self):
        prompt = record("response_item", {"type": "message", "content": [{"text": "private prompt"}]})
        tool = record("response_item", {"type": "custom_tool_call", "id": "c1", "name": "shell", "input": "secret command"})
        file_change = record("event_msg", {"type":"item_completed", "item":{"type":"FileChange", "changes":{"/tmp/a.java":{"type":"add","content":"secret source"}}}})
        self.assertIsNone(server.sanitize_record(json.loads(prompt)))
        self.assertNotIn("secret command", json.dumps(server.sanitize_record(json.loads(tool))))
        clean = server.sanitize_record(json.loads(file_change))
        self.assertEqual(clean["files"], ["/tmp/a.java"])
        self.assertNotIn("secret source", json.dumps(clean))

    def test_compacted_record_is_counted_without_retaining_payload(self):
        compacted = json.loads(record("compacted", {"transcript":"private"}))
        clean = server.sanitize_record(compacted)
        self.assertEqual(clean["kind"], "compacted")
        self.assertNotIn("private", json.dumps(clean))
        aggregate = server.Dashboard.aggregate({"id":"s","title":"s"}, [clean], 0)
        self.assertEqual(aggregate["compactions"], 1)

    def test_turn_usage_and_cumulative_snapshot_do_not_double_count(self):
        events = [
            {"kind":"token_usage_record","timestamp":"","turn_id":"t1","response_id":"r1","usage":{"input_tokens":100,"cached_input_tokens":20,"output_tokens":10,"total_tokens":110}},
            {"kind":"token_usage_record","timestamp":"","turn_id":"t1","response_id":"r1","usage":{"input_tokens":100,"cached_input_tokens":20,"output_tokens":10,"total_tokens":110}},
            {"kind":"token_count","timestamp":"","turn_id":"t1","usage":{"input_tokens":100,"cached_input_tokens":20,"output_tokens":10,"total_tokens":110},"last":{"total_tokens":110}},
            {"kind":"token_usage_record","timestamp":"","turn_id":"t2","response_id":"r2","usage":{"input_tokens":40,"cached_input_tokens":5,"output_tokens":8,"total_tokens":48}},
        ]
        result = server.Dashboard.aggregate({"id":"x","title":"x"}, events, 0)
        self.assertEqual(result["tokens"]["input_tokens"], 140)
        self.assertEqual(result["tokens"]["output_tokens"], 18)

    def test_real_token_count_payload_nesting_and_parent_metadata(self):
        ev=json.loads(record("event_msg", {"type":"token_count", "info":{"total_token_usage":{"input_tokens":77,"total_tokens":90},"last_token_usage":{"total_tokens":9},"model_context_window":1000},"rate_limits":{"primary":{"used_percent":12,"window_minutes":300,"resets_at":"2026-10-21T15:00:00Z"},"secondary":{"used_percent":5,"window_minutes":10080,"resets_at":"2026-10-27T10:00:00Z"}}}))
        clean=server.sanitize_record(ev)
        self.assertEqual(clean["usage"]["input_tokens"],77)
        self.assertIsNone(clean["usage"]["cached_input_tokens"])
        self.assertEqual(clean["context_window"],1000)
        self.assertEqual(clean["rate_limits"]["primary"]["used_percent"],12)
        self.assertEqual(clean["rate_limits"]["secondary"]["window_minutes"],10080)
        meta=json.loads(record("session_meta", {"id":"child", "source":{"subagent":{"thread_spawn":{"parent_thread_id":"parent"}}}}))
        self.assertEqual(server.sanitize_record(meta)["parent_id"],"parent")
        for source in ("cli", None, {"subagent":"malformed"}):
            meta=json.loads(record("session_meta", {"id":"root", "source":source}))
            self.assertEqual(server.sanitize_record(meta)["parent_id"],"")

    def test_malformed_nested_fields_remain_skippable(self):
        bad_usage=json.loads(record("event_msg", {"type":"token_count","info":{"total_token_usage":"bad","last_token_usage":None}}))
        clean=server.sanitize_record(bad_usage)
        self.assertIsNone(clean["usage"]["total_tokens"])
        bad_duration=json.loads(record("event_msg", {"type":"task_complete","duration_ms":"bad","time_to_first_token_ms":"bad"}))
        clean=server.sanitize_record(bad_duration)
        self.assertIsNone(clean["duration_seconds"])
        self.assertIsNone(clean["first_token_seconds"])

    def test_thread_cumulative_usage_wins_over_mismatched_token_count(self):
        events=[
            {"kind":"token_usage_record","timestamp":"2026-10-21T10:33:22Z","response_id":"r1","usage":{"input_tokens":100,"total_tokens":120},"thread_usage":{"input_tokens":30000000,"total_tokens":36061084}},
            {"kind":"token_count","timestamp":"2026-10-21T10:33:22Z","usage":{"input_tokens":28000000,"total_tokens":34844135}},
        ]
        result=server.Dashboard.aggregate({"id":"s","title":"s"},events,0)
        self.assertEqual(result["tokens"]["total_tokens"],36061084)
        self.assertEqual(result["token_source"],"thread cumulative")
        self.assertEqual(result["token_discrepancy"]["delta"],1216949)

    def test_demo_uses_same_aggregate_contract(self):
        result=server.Dashboard("/no-read",demo=True).session("demo-session")
        self.assertIn("tool_stats",result)
        self.assertTrue(result["active_tools"])
        self.assertTrue(result["files_changed"])
        self.assertTrue(result["agents"])
        self.assertEqual(result["agents"][0]["parent_id"],"demo-session")
        self.assertEqual(result["agents"][0]["depth"],1)
        self.assertEqual(result["model_tokens"]["gpt-6.1"]["total_tokens"],1440)

    def test_rollover_preserves_aggregates(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=pathlib.Path(tmp)/"x.jsonl"
            line=json.dumps({"type":"event_msg","timestamp":"2026-10-21T10:00:00Z","payload":{"type":"task_started"}}).encode()
            compacted=json.dumps({"type":"compacted","timestamp":"2026-10-21T09:59:00Z","payload":{"summary":"private"}}).encode()
            retained_compacted=json.dumps({"type":"compacted","timestamp":"2026-10-21T10:01:00Z"}).encode()
            path.write_bytes(compacted+b"\n"+(line+b"\n")*(server.MAX_EVENTS+2)+retained_compacted+b"\n")
            reader=server.RolloutReader(path); recent=reader.read()
            result=server.Dashboard.aggregate({"id":"s","title":"s"},recent,0)
            result=server.merge_summary(result,reader.summary)
            self.assertEqual(result["turns"]["started"],server.MAX_EVENTS+2)
            self.assertEqual(result["compactions"],2)
            self.assertEqual(len(recent),server.MAX_EVENTS)

    def test_rollover_uses_cumulative_snapshot_and_pairs_cross_window_tool(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=pathlib.Path(tmp)/"x.jsonl"
            lines=[
                {"type":"token_usage_record","timestamp":"2026-10-21T10:00:00Z","payload":{"turn_id":"t1","response_id":"r1","usage":{"input_tokens":100,"total_tokens":100},"thread_token_usage":{"input_tokens":100,"total_tokens":100}}},
                {"type":"event_msg","timestamp":"2026-10-21T10:00:01Z","payload":{"type":"token_count","info":{"total_token_usage":{"input_tokens":95,"total_tokens":95}}}},
                {"type":"response_item","timestamp":"2026-10-21T10:00:02Z","payload":{"type":"custom_tool_call","id":"call-1","name":"demo"}},
            ]
            noise={"type":"event_msg","timestamp":"2026-10-21T10:00:03Z","payload":{"type":"task_started"}}
            lines.extend([noise]*(server.MAX_EVENTS+4))
            lines.extend([
                {"type":"token_usage_record","timestamp":"2026-10-21T10:00:04Z","payload":{"turn_id":"t2","response_id":"r2","usage":{"input_tokens":40,"total_tokens":40}}},
                {"type":"event_msg","timestamp":"2026-10-21T10:00:05Z","payload":{"type":"token_count","info":{"total_token_usage":{"input_tokens":120,"total_tokens":120}}}},
                {"type":"response_item","timestamp":"2026-10-21T10:00:06Z","payload":{"type":"custom_tool_call_output","call_id":"call-1"}},
            ])
            path.write_bytes(b"\n".join(json.dumps(line).encode() for line in lines)+b"\n")
            reader=server.RolloutReader(path); recent=reader.read()
            result=server.merge_summary(server.Dashboard.aggregate({"id":"s","title":"s"},recent,0),reader.summary)
            self.assertEqual(result["tokens"]["input_tokens"],140)
            self.assertEqual(result["token_discrepancy"]["delta"],20)
            call=next(c for c in result["tools"] if c.get("call_id")=="call-1")
            self.assertTrue(call["ended"])


class SafetyTests(unittest.TestCase):
    def test_http_requires_local_host_and_origin(self):
        handler = object.__new__(server.Handler)
        handler.headers = {"Host":"127.0.0.1:8765"}
        self.assertTrue(handler._authorized())
        handler.headers = {"Host":"evil.example"}
        self.assertFalse(handler._authorized())
        handler.headers = {"Host":"localhost:8765", "Origin":"https://evil.example"}
        self.assertFalse(handler._authorized())
        handler.headers = {"Host":"localhost:8765", "Origin":"http://[malformed"}
        self.assertFalse(handler._authorized())

    def test_ipv6_cli_selects_ipv6_server_and_prints_bracketed_url(self):
        class FakeServer:
            def __init__(self, address, handler):
                self.address=address; self.server_port=1234
                self.family=self.address_family
                servers.append(self)
            def serve_forever(self): raise KeyboardInterrupt
            def server_close(self): self.closed=True
        servers=[]; output=io.StringIO()
        with patch.object(server,"ThreadingHTTPServer",FakeServer), patch('sys.argv',["server.py","--demo","--host","::1","--port","0"]), patch('sys.stdout',output):
            server.main()
        self.assertEqual(servers[0].family,socket.AF_INET6)
        self.assertIn('http://[::1]:1234/',output.getvalue())
        self.assertTrue(servers[0].closed)

    def test_http_routes_apply_host_checks_and_never_return_prompt_sentinel(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "sessions" / "rollout.jsonl"
            path.parent.mkdir()
            path.write_bytes(record("session_meta", {"id":"a1","cwd":"/safe","title":"safe"}) + b"\n" +
                             record("response_item", {"type":"message","content":[{"text":"PRIVATE_SENTINEL"}]}) + b"\n")
            dashboard = server.Dashboard(tmp)
            server.Handler.dashboard = dashboard
            response = self.request("GET /api/sessions HTTP/1.1\r\nHost: 127.0.0.1:8765\r\nConnection: close\r\n\r\n")
            self.assertIn(b"200 OK", response)
            self.assertIn(b"style-src-attr 'unsafe-inline'", response)
            self.assertIn(b"script-src 'self'", response)
            self.assertNotIn(b"script-src 'unsafe-inline'", response)
            response = self.request("GET /api/session/a1 HTTP/1.1\r\nHost: 127.0.0.1:8765\r\nConnection: close\r\n\r\n")
            self.assertIn(b"200 OK", response)
            self.assertNotIn(b"PRIVATE_SENTINEL", response)
            denied = self.request("GET /api/sessions HTTP/1.1\r\nHost: localhost\r\nOrigin: https://evil.example\r\nConnection: close\r\n\r\n")
            self.assertIn(b"403 Forbidden", denied)
            traversal = self.request("GET /api/session/..%2Fsecret HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n")
            self.assertIn(b"404 Not Found", traversal)
            write = self.request("POST /api/sessions HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\nContent-Length: 0\r\n\r\n")
            self.assertIn(b"405 Method Not Allowed", write)

    @staticmethod
    def request(request_text):
        class Capture(io.BytesIO):
            def close(self): pass
        class Socket:
            def __init__(self, data): self.input, self.output = io.BytesIO(data), Capture()
            def makefile(self, mode, *args, **kwargs): return self.input if "r" in mode else self.output
            def sendall(self, data): self.output.write(data)
        class FakeServer:
            server_name = "localhost"
            server_port = 8765
        sock=Socket(request_text.encode())
        server.Handler(sock, ("127.0.0.1", 12345), FakeServer())
        return sock.output.getvalue()


class DataShapeTests(unittest.TestCase):
    def test_rollover_unknown_source_does_not_mask_later_usage(self):
        summary=server.reduce_events({},[{"kind":"compacted","timestamp":""}])
        usage={"kind":"token_usage_record","timestamp":"","response_id":"r","usage":{"total_tokens":10}}
        current=server.merge_summary(server.Dashboard.aggregate({"id":"s"},[usage],0,summary),summary)
        self.assertEqual(current["token_source"],"per-response sum")
        summary=server.reduce_events(summary,[usage])
        current=server.merge_summary(server.Dashboard.aggregate({"id":"s"},[],0,summary),summary)
        self.assertEqual(current["token_source"],"per-response sum")

    def test_rollover_merges_agent_metadata_before_trimming(self):
        links={"kind":"agent_link","timestamp":"","parent_id":"p","children":["a"+str(i) for i in range(100)]}
        summary=server.reduce_events({},[links])
        updates=[{"kind":"agent_activity","timestamp":"","agent_id":"a"+str(i),"model":"model"} for i in range(100)]
        current=server.merge_summary(server.Dashboard.aggregate({"id":"s"},updates,0,summary),summary)
        self.assertTrue(all(agent.get("parent_id")=="p" for agent in current["agents"]))
        reduced=server.reduce_events(summary,updates)
        self.assertTrue(all(agent.get("parent_id")=="p" for agent in reduced["agents"]))

    def test_completion_across_rollover_is_one_call_with_error_and_duration(self):
        start={"kind":"tool_start","timestamp":"2026-10-21T10:00:00Z","call_id":"c","name":"tool"}
        end={"kind":"tool_event","timestamp":"2026-10-21T10:00:04Z","item_id":"c","name":"tool","status":"failed","duration_seconds":2.5}
        summary=server.reduce_events({},[start])
        result=server.merge_summary(server.Dashboard.aggregate({"id":"s"},[end],0),summary)
        self.assertEqual(result["tool_stats"]["tool"],{"count":1,"errors":1,"average_seconds":2.5})
        self.assertFalse(result["active_tools"])
        end["duration_seconds"]=None
        result=server.merge_summary(server.Dashboard.aggregate({"id":"s"},[end],0),summary)
        self.assertEqual(result["tool_stats"]["tool"]["average_seconds"],4)

    def test_unknown_duration_and_zero_duration_stay_distinct_after_rollover(self):
        unknown={"kind":"tool_event","timestamp":"2026-10-21T10:00:00Z","item_id":"c","name":"tool","status":"completed","duration_seconds":None}
        result=server.Dashboard.aggregate({"id":"s"},[unknown],0)
        self.assertIsNone(result["turns"]["tool_elapsed_seconds"])
        zero=server.sanitize_record(json.loads(record("event_msg",{"type":"task_complete","duration_ms":0,"time_to_first_token_ms":0})))
        summary=server.reduce_events({},[zero])
        result=server.merge_summary(server.Dashboard.aggregate({"id":"s"},[],0),summary)
        self.assertEqual(result["turns"]["recorded_duration_seconds"],0)
        self.assertEqual(result["turns"]["first_token_latency_seconds"],0)
        self.assertIsNone(server.duration_seconds(float('inf')))

    def test_missing_wall_timestamp_is_not_treated_as_unix_epoch(self):
        result=server.Dashboard.aggregate({"id":"s"},[
            {"kind":"task_started","timestamp":""},
            {"kind":"task_complete","timestamp":"2026-10-21T10:00:00Z"},
        ],0)
        self.assertEqual(result["turns"]["started"],1)
        self.assertIsNone(result["turns"]["wall_seconds"])

    def test_session_read_and_reduction_are_atomic_between_requests(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(server,"MAX_EVENTS",2):
            path=pathlib.Path(tmp)/"sessions"/"r.jsonl"; path.parent.mkdir()
            path.write_bytes(record("session_meta",{"id":"s"})+b"\n"+record("compacted",{})+b"\n")
            dashboard=server.Dashboard(tmp); dashboard.session("s")
            original=server.Dashboard.aggregate
            entered=threading.Event(); release=threading.Event(); second_read=threading.Event()
            results=[]; errors=[]
            def aggregate(*args,**kwargs):
                if threading.current_thread().name=="first":
                    entered.set(); release.wait(2)
                return original(*args,**kwargs)
            reader=next(iter(dashboard.readers.values())); original_read=reader.read
            def read():
                if threading.current_thread().name=="second": second_read.set()
                return original_read()
            def run():
                try: results.append(dashboard.session("s"))
                except Exception as exc: errors.append(exc)
            with patch.object(server.Dashboard,"aggregate",side_effect=aggregate), patch.object(reader,"read",side_effect=read):
                first=threading.Thread(target=run,name="first"); first.start()
                self.assertTrue(entered.wait(2))
                with path.open("ab") as stream: stream.write(record("compacted",{})+b"\n")
                second=threading.Thread(target=run,name="second"); second.start()
                read_before_release=second_read.wait(.1)
                release.set(); first.join(2); second.join(2)
            self.assertFalse(read_before_release,"second request must not advance summary behind first request")
            self.assertFalse(errors)
            self.assertEqual(sorted(value["compactions"] for value in results),[1,2])

    def test_replaced_rollout_resets_reader_even_if_file_did_not_shrink(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=pathlib.Path(tmp)/"r.jsonl"; path.write_bytes(record("compacted",{})+b"\n")
            reader=server.RolloutReader(path); reader.read()
            replacement=path.with_suffix('.new'); replacement.write_bytes(record("session_meta",{"id":"new","cwd":"/replacement"})+b"\n")
            replacement.replace(path)
            self.assertEqual([e["kind"] for e in reader.read()],["session_meta"])

    def test_deep_invalid_json_and_agent_hierarchy_are_bounded(self):
        self.assertIsNone(server.json_line(b'{"value":'+b'['*2000+b'0'+b']'*2000+b'}'))
        agents=server.build_agent_tree([{"agent_id":str(i),"parent_id":str(i-1)} for i in range(1200)])
        self.assertLessEqual(max(agent["depth"] for agent in agents),8)

    def test_recorded_tool_duration_is_not_replaced_by_completion_timestamp(self):
        result=server.Dashboard.aggregate({"id":"s"}, [
            {"kind":"tool_event","timestamp":"2026-10-21T10:00:00Z","item_id":"x","name":"tool","status":"completed","duration_seconds":2.5},
        ], 0)
        self.assertEqual(result["tool_stats"]["tool"]["average_seconds"],2.5)
        self.assertEqual(result["turns"]["tool_elapsed_seconds"],2.5)

    def test_null_token_count_does_not_erase_last_known_total(self):
        events=[server.sanitize_record(json.loads(record("event_msg", payload))) for payload in [
            {"type":"token_count","info":{"total_token_usage":{"total_tokens":100}}},
            {"type":"token_count","info":None,"rate_limits":{"primary":{"used_percent":10}}},
        ]]
        result=server.Dashboard.aggregate({"id":"s"},events,0)
        self.assertEqual(result["tokens"]["total_tokens"],100)
        self.assertEqual(result["limits"]["primary"]["used_percent"],10)

    def test_equal_unidentified_responses_are_not_duplicates(self):
        usage={"kind":"token_usage_record","timestamp":"","turn_id":"t","usage":{"total_tokens":10}}
        result=server.Dashboard.aggregate({"id":"s"},[usage,usage],0)
        self.assertEqual(result["tokens"]["total_tokens"],20)

    def test_rollover_preserves_model_limits_metadata_and_deduplication(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(server,"MAX_EVENTS",3):
            path=pathlib.Path(tmp)/"sessions"/"r.jsonl"; path.parent.mkdir()
            events=[
                record("session_meta",{"id":"s","source":{"subagent":{"thread_spawn":{"parent_thread_id":"p"}}}}),
                record("turn_context",{"model":"model-a","effort":"high"}),
                record("event_msg",{"type":"token_count","info":{"total_token_usage":{"total_tokens":0}},"rate_limits":{"primary":{"used_percent":10}}}),
                record("token_usage_record",{"response_id":"r1","usage":{"total_tokens":10}}),
                record("compacted",{}), record("compacted",{}), record("compacted",{}),
            ]
            path.write_bytes(b"\n".join(events)+b"\n")
            dashboard=server.Dashboard(tmp)
            first=dashboard.session("s")
            self.assertEqual(first["limits"]["primary"]["used_percent"],10)
            with path.open("ab") as stream:
                stream.write(record("token_usage_record",{"response_id":"r1","usage":{"total_tokens":10}})+b"\n")
                stream.write(record("token_usage_record",{"response_id":"r2","usage":{"total_tokens":20}})+b"\n")
            result=dashboard.session("s")
            self.assertEqual(result["tokens"]["total_tokens"],30)
            self.assertEqual(result["model_tokens"],{"model-a":{"total_tokens":30}})
            self.assertEqual(result["agents"][0]["parent_id"],"p")

    def test_malformed_nested_records_do_not_break_following_records(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=pathlib.Path(tmp)/"r.jsonl"
            path.write_bytes(record("response_item",{"type":"file_change","changes":{"bad":"shape"}})+b"\n"+
                             record("event_msg",{"type":"item_completed","item":{"type":"CollabAgentToolCall","receiver_thread_ids":[{},[],"child"]}})+b"\n"+
                             record("compacted",{})+b"\n")
            result=server.Dashboard.aggregate({"id":"s"},server.RolloutReader(path).read(),0)
            self.assertEqual(result["compactions"],1)
            self.assertEqual([a["agent_id"] for a in result["agents"]],["child"])

    def test_reader_bounds_unterminated_lines_and_recovers(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(server,"MAX_LINE_BYTES",256,create=True):
            path=pathlib.Path(tmp)/"r.jsonl"; path.write_bytes(b"x"*500)
            reader=server.RolloutReader(path); reader.read()
            self.assertLessEqual(len(reader.pending),256)
            with path.open("ab") as stream: stream.write(b"x\n"+record("compacted",{})+b"\n")
            self.assertEqual([e["kind"] for e in reader.read()],["compacted"])
            self.assertEqual(reader.skipped_lines,1)

    def test_truncation_resets_freshness(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=pathlib.Path(tmp)/"r.jsonl"; path.write_bytes(record("compacted",{})+b"\n")
            reader=server.RolloutReader(path); reader.read()
            path.write_bytes(b"{}\n"); reader.read()
            self.assertEqual(reader.last_event_time,0)

    def test_old_sqlite_thread_schema_without_parent_column(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=pathlib.Path(tmp); rollout=root/"sessions"/"a.jsonl"; rollout.parent.mkdir()
            rollout.write_bytes(record("session_meta", {"id":"thread-1","cwd":"/x"})+b"\n")
            with sqlite3.connect(root/"state_5.sqlite") as cx:
                cx.execute("CREATE TABLE threads (id TEXT, rollout_path TEXT, title TEXT, cwd TEXT, updated_at INTEGER)")
                cx.execute("INSERT INTO threads VALUES (?,?,?,?,?)",("thread-1",str(rollout),"Title","/x",5))
            rows=server.Dashboard(root).sessions()
            self.assertEqual(rows[0]["title"],"Title")

    def test_model_attribution_tool_errors_and_stale_active_state(self):
        events=[
            {"kind":"turn_context","timestamp":"","model":"model-a","effort":"low"},
            {"kind":"token_usage_record","timestamp":"","response_id":"ra","usage":{"input_tokens":10,"total_tokens":11}},
            {"kind":"turn_context","timestamp":"","model":"model-b","effort":"high"},
            {"kind":"token_usage_record","timestamp":"","response_id":"rb","usage":{"input_tokens":20,"total_tokens":21}},
            {"kind":"tool_event","timestamp":"2026-10-21T10:00:00Z","item_id":"x","name":"McpToolCall · lookup","status":"failed","duration_seconds":2},
            {"kind":"task_started","timestamp":"2026-10-21T10:00:00Z"},
        ]
        result=server.Dashboard.aggregate({"id":"s","title":"s"},events,120)
        self.assertEqual(result["model_tokens"]["model-a"]["total_tokens"],11)
        self.assertEqual(result["model_tokens"]["model-b"]["total_tokens"],21)
        self.assertEqual(result["tool_stats"]["McpToolCall · lookup"]["errors"],1)
        self.assertTrue(result["active_is_stale"])
        self.assertEqual(result["turns"]["recorded_duration_seconds"],None)


if __name__ == "__main__":
    unittest.main()

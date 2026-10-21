#!/usr/bin/env python3
"""Local, read-only Codex activity dashboard. Standard library only."""
from __future__ import annotations

import argparse
import html
import json
import math
import os
import pathlib
import re
import sqlite3
import socket
import threading
import time
from contextlib import closing
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, urlsplit

MAX_EVENTS = 3000
MAX_CALLS = 500
MAX_LINE_BYTES = 8 * 1024 * 1024
SAFE_ID = re.compile(r"^[a-zA-Z0-9_-]{1,100}$")


def safe_text(value, limit=200):
    return str(value)[:limit] if value is not None else ""


def json_line(line):
    try:
        value = json.loads(line)
        return value if isinstance(value, dict) else None
    except (ValueError, UnicodeDecodeError, RecursionError):
        return None


class RolloutReader:
    """Incremental byte reader; holds only bounded sanitized event state."""
    def __init__(self, path):
        self.path = pathlib.Path(path)
        self.offset = 0
        self.pending = b""
        self.events = []
        self.summary = []
        self.last_event_time = 0
        self.file_size = 0
        self.last_read = 0
        self.skipping_line = False
        self.skipped_lines = 0
        self.identity = None
        self.lock = threading.RLock()

    def read(self):
        with self.lock:
            try:
                stat = self.path.stat()
                size = stat.st_size
                self.file_size = size
                identity = (stat.st_dev, stat.st_ino)
                if size < self.offset or (self.identity is not None and identity != self.identity):
                    self.offset, self.pending, self.events, self.summary = 0, b"", [], []
                    self.last_event_time = 0
                    self.skipping_line, self.skipped_lines = False, 0
                self.identity = identity
                with self.path.open("rb") as stream:
                    stream.seek(self.offset)
                    chunk = stream.read(8 * 1024 * 1024)
                    self.offset += len(chunk)
                self.last_read = time.time()
            except OSError:
                return self.events[:]
            pieces = (self.pending + chunk).split(b"\n")
            self.pending = pieces.pop()
            if self.skipping_line:
                if pieces:
                    pieces.pop(0)
                    self.skipping_line = False
                else:
                    self.pending = b""
            if len(self.pending) > MAX_LINE_BYTES:
                self.pending = b""
                self.skipping_line = True
                self.skipped_lines += 1
            for raw in pieces:
                if len(raw) > MAX_LINE_BYTES:
                    self.skipped_lines += 1
                    continue
                record = json_line(raw)
                if record:
                    stamp = parse_time(record.get("timestamp"))
                    if stamp: self.last_event_time = stamp
                    clean = sanitize_record(record)
                    if clean:
                        self.events.append(clean)
            if len(self.events) > MAX_EVENTS:
                dropped, self.events = self.events[:-MAX_EVENTS], self.events[-MAX_EVENTS:]
                self.summary = reduce_events(self.summary, dropped)
            return self.events[:]


def reduce_events(previous, dropped):
    prior = previous if isinstance(previous, dict) else {}
    data = Dashboard.aggregate({"id":"", "title":""}, dropped, 0, prior)
    prior_thread=prior.get("token_source")=="thread cumulative"
    if data["_thread_snapshot_present"]:
        totals = dict(data["tokens"])
        token_source="thread cumulative"
        thread_snapshot=dict(data["_thread_snapshot"])
    elif prior_thread:
        totals=dict(prior.get("tokens",{}))
        for key,value in data["_per_response_tokens"].items(): totals[key]=totals.get(key,0)+value
        token_source="thread cumulative"
        thread_snapshot=totals
    elif data["_token_snapshot_present"]:
        totals=dict(data["tokens"]); token_source=data["token_source"]; thread_snapshot=None
    else:
        totals = dict(prior.get("tokens", {}))
        for key, value in data["tokens"].items(): totals[key] = totals.get(key, 0) + value
        token_source=prior.get("token_source") if prior.get("token_source") not in (None,"unknown") else data["token_source"]; thread_snapshot=None
    tools=apply_tool_ends(merge_tool_calls(prior.get("tools",[])+data["tools"]),data.get("_unmatched_tool_ends",[]))
    has_turn_state=any(e["kind"] in ("task_started","task_complete") for e in dropped)
    count_snapshot=data.get("_token_count_snapshot") or prior.get("count_snapshot")
    discrepancy=None
    if token_source=="thread cumulative" and count_snapshot and totals.get("total_tokens") is not None and count_snapshot.get("total_tokens") is not None and totals["total_tokens"]!=count_snapshot["total_tokens"]:
        discrepancy={"thread_total":totals["total_tokens"],"token_count_total":count_snapshot["total_tokens"],"delta":totals["total_tokens"]-count_snapshot["total_tokens"]}
    return {"tokens":totals,"token_source":token_source,"thread_snapshot":thread_snapshot,"count_snapshot":count_snapshot,"token_discrepancy":discrepancy,
            "seen_responses":data["_seen_responses"], "limits":data["limits"] or prior.get("limits"),
            "meta":next((e for e in reversed(dropped) if e["kind"]=="session_meta"),prior.get("meta")),
            "has_usage_records":prior.get("has_usage_records",False) or data["_usage_records_present"],
            "model_tokens":merge_usage(prior.get("model_tokens",{}),data.get("model_tokens",{})),
            "last_snapshot":next((e["usage"] for e in reversed(dropped) if e["kind"]=="token_count"),prior.get("last_snapshot")),
            "turns_started":prior.get("turns_started",0)+sum(e["kind"]=="task_started" for e in dropped),
            "turns_completed":prior.get("turns_completed",0)+sum(e["kind"]=="task_complete" for e in dropped),
            "compactions":prior.get("compactions",0)+sum(e["kind"]=="compacted" for e in dropped),
            "active_at":data["active_at"] if has_turn_state else prior.get("active_at",""),
            "has_turn_state":prior.get("has_turn_state",False) or has_turn_state,
            "tools":tools[-MAX_CALLS:], "files":list(dict.fromkeys(prior.get("files",[])+data["files_changed"]))[-300:],
            "agents":build_agent_tree(prior.get("agents",[])+data["agents"])[-100:], "skills":(prior.get("skills",[])+data["skills"])[-100:],
            "log":(prior.get("log",[])+data["log"])[-100:], "latest_event_time":next((parse_time(e.get("timestamp")) for e in reversed(dropped) if parse_time(e.get("timestamp"))),prior.get("latest_event_time",0)),
            "model":next((e.get("model") for e in reversed(dropped) if e["kind"]=="turn_context" and e.get("model")),prior.get("model","")),
            "effort":next((e.get("effort") for e in reversed(dropped) if e["kind"]=="turn_context" and e.get("effort")),prior.get("effort","")),
            "context_window":next((e.get("context_window") for e in reversed(dropped) if e.get("context_window")),prior.get("context_window",0)),
            "recorded_duration_seconds":sum_observed(prior.get("recorded_duration_seconds"),data["turns"].get("recorded_duration_seconds")),
            "first_token_latency_seconds":sum_observed(prior.get("first_token_latency_seconds"),data["turns"].get("first_token_latency_seconds")),
            "wall_start":min([x for x in (prior.get("wall_start"),data.get("_wall_start")) if x is not None],default=None),
            "wall_end":max([x for x in (prior.get("wall_end"),data.get("_wall_end")) if x is not None],default=None)}


def merge_summary(result, summary):
    if not isinstance(summary, dict): return result
    count_snapshot=result.get("_token_count_snapshot") or summary.get("count_snapshot")
    if result.get("_thread_snapshot_present"):
        result["token_source"]="thread cumulative"
    elif summary.get("token_source")=="thread cumulative":
        result["tokens"]={k:summary.get("tokens",{}).get(k,0)+result.get("_per_response_tokens",{}).get(k,0) for k in set(summary.get("tokens",{}))|set(result.get("_per_response_tokens",{}))}
        result["token_source"]="thread cumulative"
        result["token_discrepancy"]={"thread_total":result["tokens"].get("total_tokens"),"token_count_total":count_snapshot.get("total_tokens"),"delta":result["tokens"].get("total_tokens")-count_snapshot.get("total_tokens")} if count_snapshot and result["tokens"].get("total_tokens") is not None and count_snapshot.get("total_tokens") is not None and result["tokens"].get("total_tokens")!=count_snapshot.get("total_tokens") else None
    elif result.get("_token_snapshot_present"):
        result["token_source"]=result.get("token_source") or summary.get("token_source")
    else:
        result["tokens"]={k:summary.get("tokens",{}).get(k,0)+result.get("tokens",{}).get(k,0) for k in set(summary.get("tokens",{}))|set(result.get("tokens",{}))}
        result["token_source"]=summary.get("token_source") if summary.get("token_source") not in (None,"unknown") else result.get("token_source")
    if result.get("token_source")=="thread cumulative" and result.get("token_discrepancy") is None and count_snapshot and result.get("tokens",{}).get("total_tokens") is not None and count_snapshot.get("total_tokens") is not None and result["tokens"]["total_tokens"]!=count_snapshot["total_tokens"]:
        result["token_discrepancy"]={"thread_total":result["tokens"]["total_tokens"],"token_count_total":count_snapshot["total_tokens"],"delta":result["tokens"]["total_tokens"]-count_snapshot["total_tokens"]}
    result["model_tokens"]=merge_usage(summary.get("model_tokens",{}),result.get("model_tokens",{}))
    result["limits"] = result.get("limits") or summary.get("limits")
    result["compactions"]+=summary.get("compactions",0)
    result["turns"]["started"]+=summary.get("turns_started",0); result["turns"]["completed"]+=summary.get("turns_completed",0)
    result["turns"]["recorded_duration_seconds"]=sum_observed(summary.get("recorded_duration_seconds"),result["turns"].get("recorded_duration_seconds"))
    result["turns"]["first_token_latency_seconds"]=sum_observed(summary.get("first_token_latency_seconds"),result["turns"].get("first_token_latency_seconds"))
    wall_starts=[x for x in (summary.get("wall_start"),result.get("_wall_start")) if x]
    wall_ends=[x for x in (summary.get("wall_end"),result.get("_wall_end")) if x]
    result["turns"]["wall_seconds"]=max(wall_ends)-min(wall_starts) if wall_starts and wall_ends else None
    for key, source in (("tools","tools"),("files_changed","files"),("agents","agents"),("skills","skills"),("log","log")):
        values=summary.get(source,[])+result.get(key,[])
        if key=="tools": values=merge_tool_calls(values)
        if key=="files_changed": values=list(dict.fromkeys(values))
        if key=="agents": values=build_agent_tree(values)
        result[key]=values[-(MAX_CALLS if key=="tools" else 300 if key=="files_changed" else 100):]
    result["tool_stats"]=tools_statistics(result["tools"])
    result["active_at"] = result.get("active_at") if result.get("_turn_state_seen") else summary.get("active_at","")
    result["tools"]=apply_tool_ends(result["tools"],result.get("_unmatched_tool_ends",[]))[-MAX_CALLS:]
    result["active_tools"]=[c for c in result["tools"] if not c.get("ended")]
    if not result["active_at"] and not result.get("_turn_state_seen") and not summary.get("has_turn_state") and result["active_tools"]:
        result["active_at"]=result["active_tools"][-1]["started"]
    result["active_is_stale"] = bool(result["active_at"] and result["freshness_seconds"]>90)
    result["tool_stats"]=tools_statistics(result["tools"])
    tool_durations=[call["duration_seconds"] for call in result["tools"] if call.get("duration_seconds") is not None]
    result["turns"]["tool_elapsed_seconds"]=sum(tool_durations) if tool_durations else None
    result["agents"]=build_agent_tree(result["agents"])
    result["model"] = result.get("model") or summary.get("model",""); result["effort"] = result.get("effort") or summary.get("effort","")
    result["context_window"] = result.get("context_window") or summary.get("context_window",0)
    for key in ("_seen_responses","_token_snapshot_present","_thread_snapshot_present","_thread_snapshot","_token_count_snapshot","_per_response_tokens","_usage_records_present","_turn_state_seen","_unmatched_tool_ends","_wall_start","_wall_end"):
        result.pop(key,None)
    return result


def merge_usage(left, right):
    merged={model:dict(usage) for model,usage in left.items()}
    for model,usage in right.items():
        target=merged.setdefault(model,{})
        for key,value in usage.items(): target[key]=target.get(key,0)+value
    return merged


def sum_observed(left, right):
    values=[value for value in (left,right) if value is not None]
    return sum(values) if values else None


def merge_tool_calls(calls):
    merged={}
    for index, call in enumerate(calls):
        key=call.get("call_id") or index
        if key in merged:
            started=merged[key].get("started")
            was_open=not merged[key].get("ended")
            merged[key].update({k:v for k,v in call.items() if v is not None and v != ""})
            if started: merged[key]["started"]=started
            if was_open and merged[key].get("ended") and merged[key].get("duration_seconds") is None:
                start=parse_time(started); end=parse_time(merged[key]["ended"])
                merged[key]["duration_seconds"]=max(0,end-start) if start and end else None
        else: merged[key]=dict(call)
    return list(merged.values())


def apply_tool_ends(calls, endings):
    result=[dict(call) for call in calls]
    for ending in endings:
        for call in reversed(result):
            if call.get("call_id")==ending.get("call_id") and not call.get("ended"):
                call["ended"]=ending.get("timestamp","")
                start=parse_time(call.get("started")); end=parse_time(call.get("ended"))
                call["duration_seconds"]=max(0,end-start) if start and end else None
                break
    return result


def tools_statistics(tool_list):
    stats={}
    for call in tool_list:
        name=call.get("name") or "tool"
        stat=stats.setdefault(name,{"count":0,"errors":0,"durations":[]}); stat["count"]+=1
        if call.get("status") and call["status"].lower() not in ("completed","success","succeeded"): stat["errors"]+=1
        if call.get("duration_seconds") is not None: stat["durations"].append(call["duration_seconds"])
    for stat in stats.values():
        durations=stat.pop("durations"); stat["average_seconds"]=sum(durations)/len(durations) if durations else None
    return stats


def build_agent_tree(agents):
    nodes={}
    for item in agents:
        agent_id=item.get("agent_id")
        if agent_id:
            nodes.setdefault(agent_id,{}).update(item)
    def depth(node, visiting=None):
        visiting=set() if visiting is None else visiting
        if len(visiting) >= 8: return 0
        parent=node.get("parent_id")
        if not parent or parent not in nodes or parent in visiting: return 1 if parent else 0
        return min(8,depth(nodes[parent],visiting|{parent})+1)
    result=list(nodes.values())
    for node in result: node["depth"]=depth(node)
    return sorted(result,key=lambda item:(item["depth"],item.get("agent_path") or item.get("agent_id") or ""))


def sanitize_record(record):
    kind, p = record.get("type"), record.get("payload")
    if kind == "compacted":
        return {"kind": kind, "timestamp": safe_text(record.get("timestamp"), 40)}
    if not isinstance(p, dict):
        return None
    stamp = safe_text(record.get("timestamp"), 40)
    if kind == "session_meta":
        source = p.get("source") if isinstance(p.get("source"), dict) else {}
        subagent = source.get("subagent") if isinstance(source.get("subagent"), dict) else {}
        spawn = subagent.get("thread_spawn") if isinstance(subagent.get("thread_spawn"), dict) else {}
        return {"kind": kind, "timestamp": stamp, "session_id": safe_text(p.get("id") or p.get("session_id"), 100),
                "parent_id": safe_text(p.get("parent_thread_id") or spawn.get("parent_thread_id"), 100), "cwd": safe_text(p.get("cwd"), 500),
                "model": safe_text(p.get("model"), 80), "role": safe_text(p.get("agent_role"), 80),
                "agent_path": safe_text(p.get("agent_path"), 300), "source": safe_text(source.get("kind") if isinstance(source, dict) else source, 80)}
    if kind == "turn_context":
        return {"kind": kind, "timestamp": stamp, "model": safe_text(p.get("model"), 80), "effort": safe_text(p.get("effort"), 30),
                "context_window": positive_int(p.get("model_context_window"))}
    if kind == "token_usage_record":
        u = p.get("usage") or {}
        if not isinstance(u, dict): return None
        thread_usage=p.get("thread_token_usage") if isinstance(p.get("thread_token_usage"),dict) else {}
        thread_totals={k:positive_int(thread_usage[k]) if thread_usage.get(k) is not None else None for k in ("input_tokens","cached_input_tokens","cache_write_input_tokens","output_tokens","reasoning_output_tokens","total_tokens")}
        return {"kind": kind, "timestamp": stamp, "turn_id": safe_text(p.get("turn_id"), 100), "response_id": safe_text(p.get("response_id"), 100),
                "usage": {key: positive_int(u[key]) if u.get(key) is not None else None for key in ("input_tokens", "cached_input_tokens", "cache_write_input_tokens", "output_tokens", "reasoning_output_tokens", "total_tokens")},
                "thread_usage":thread_totals}
    if kind == "event_msg":
        subtype = p.get("type")
        if subtype == "token_count":
            info = p.get("info") if isinstance(p.get("info"), dict) else p
            total = info.get("total_token_usage") if isinstance(info.get("total_token_usage"),dict) else {}
            last = info.get("last_token_usage") if isinstance(info.get("last_token_usage"),dict) else {}
            return {"kind": "token_count", "timestamp": stamp, "turn_id": safe_text(p.get("turn_id"), 100),
                    "usage": {k:positive_int(total[k]) if total.get(k) is not None else None for k in ("input_tokens", "cached_input_tokens", "output_tokens", "reasoning_output_tokens", "total_tokens")}, "last": {k:positive_int(last[k]) if last.get(k) is not None else None for k in ("input_tokens", "cached_input_tokens", "output_tokens", "reasoning_output_tokens", "total_tokens")},
                    "context_window": positive_int(info.get("model_context_window") or p.get("model_context_window")), "rate_limits": sanitize_limits(info.get("rate_limits") or info.get("primary_rate_limit") or p.get("rate_limits"), stamp)}
        if subtype in ("task_started", "task_complete", "item_completed"):
            item = p.get("item") if isinstance(p.get("item"), dict) else {}
            item_type = safe_text(item.get("type"), 40)
            if item_type == "FileChange":
                changes = item.get("changes") if isinstance(item.get("changes"), dict) else {}
                return {"kind":"file_change", "timestamp":stamp, "files":[safe_text(x,500) for x in changes.keys()][:100], "counts":{safe_text(x,500):safe_text((v or {}).get("type"),20) for x,v in list(changes.items())[:100] if isinstance(v,dict)}}
            if item_type in ("CommandExecution", "McpToolCall"):
                return {"kind":"tool_event", "timestamp":stamp, "item_id":safe_text(item.get("id"),100), "name":safe_text(item.get("tool") or item_type,100),
                        "status":safe_text(item.get("status"),30), "duration_seconds":duration_seconds(item.get("duration")), "turn_id":safe_text(p.get("turn_id"),100)}
            if item_type == "CollabAgentToolCall":
                return {"kind":"agent_link", "timestamp":stamp, "parent_id":safe_text(item.get("sender_thread_id"),100), "children":[safe_text(child,100) for child in item["receiver_thread_ids"][:20] if isinstance(child,str) and child] if isinstance(item.get("receiver_thread_ids"),list) else []}
            if item_type == "SubAgentActivity":
                return {"kind":"agent_activity", "timestamp":stamp, "agent_id":safe_text(item.get("agent_thread_id"),100), "agent_path":safe_text(item.get("agent_path"),300), "model":safe_text(item.get("model"),80), "effort":safe_text(item.get("reasoning_effort"),30)}
            duration_ms=p.get("duration_ms")
            duration_value=item.get("duration") if item.get("duration") is not None else (duration_ms/1000 if isinstance(duration_ms,(int,float)) else None)
            first_token_ms=p.get("time_to_first_token_ms")
            return {"kind": subtype, "timestamp": stamp, "turn_id": safe_text(p.get("turn_id"), 100), "item_type": item_type,
                    "item_id": safe_text(item.get("id"), 100), "status": safe_text(item.get("status"), 30),
                    "started_at": safe_text(p.get("started_at"),40), "duration_seconds":duration_seconds(duration_value),
                    "first_token_seconds": duration_seconds(first_token_ms / 1000 if isinstance(first_token_ms,(int,float)) else None)}
        return None
    if kind == "response_item":
        typ = p.get("type")
        # Deliberately discard messages, prompts, command text, tool arguments and outputs.
        if typ in ("function_call", "custom_tool_call"):
            name = safe_text(p.get("name") or p.get("tool_name"), 100)
            return {"kind": "tool_start", "timestamp": stamp, "call_id": safe_text(p.get("call_id") or p.get("id"), 100), "name": name}
        if typ in ("function_call_output", "custom_tool_call_output"):
            return {"kind": "tool_end", "timestamp": stamp, "call_id": safe_text(p.get("call_id") or p.get("id"), 100)}
        if typ == "skill_prompt":
            return {"kind":"skill_observed", "timestamp":stamp, "name":safe_text(p.get("name"),100)}
        if typ == "file_change":
            changes = p.get("changes") if isinstance(p.get("changes"),list) else []
            return {"kind": "file_change", "timestamp": stamp, "files": [safe_text(x.get("path"), 500) for x in changes[:100] if isinstance(x, dict)]}
        return None
    return None


def positive_int(v):
    try: return max(0, int(v))
    except (ValueError, TypeError, OverflowError): return 0


def duration_seconds(value):
    if isinstance(value,dict): return max(0,positive_int(value.get("secs"))) + max(0,positive_int(value.get("nanos"))) / 1_000_000_000
    if isinstance(value,(int,float)) and math.isfinite(value): return max(0,value)
    return None


def sanitize_limits(limits, observed):
    if not isinstance(limits, dict): return None
    out = {"observed_at": observed}
    for window_name in ("primary", "secondary"):
        window=limits.get(window_name)
        if isinstance(window,dict):
            values={k:window[k] for k in ("used_percent","remaining","window_minutes","resets_at","reset_at") if isinstance(window.get(k),(str,int,float))}
            if values: out[window_name]=values
    values={k:limits[k] for k in ("used_percent","remaining","window_minutes","resets_at","reset_at") if isinstance(limits.get(k),(str,int,float))}
    if values: out["primary"]=values
    return out if len(out)>1 else None


class Dashboard:
    def __init__(self, root, demo=False):
        self.root = pathlib.Path(root).expanduser().resolve()
        self.demo = demo
        self.readers = {}
        self.lock = threading.RLock()
        self._sessions_cache = None
        self._sessions_cache_at = 0
        self.demo_sessions = self._demo_data() if demo else None

    @staticmethod
    def _demo_data():
        return [{"id": "demo-session", "title": "Demo: safe summary", "cwd": "~/demo", "updated_at": int(time.time()),
                 "events": [{"kind":"session_meta","timestamp":time.strftime("%Y-%m-%dT%H:%M:%SZ",time.gmtime()),"session_id":"demo-session","parent_id":"","cwd":"~/demo","model":"gpt-6.1","role":"","agent_path":"","source":""},
                            {"kind":"turn_context","timestamp":"","model":"gpt-6.1","effort":"medium","context_window":128000},
                            {"kind":"task_started","timestamp":time.strftime("%Y-%m-%dT%H:%M:%SZ",time.gmtime()),"turn_id":"demo-turn"},
                            {"kind":"token_usage_record","timestamp":"","turn_id":"demo-turn","response_id":"demo-response","usage":{"input_tokens":1200,"cached_input_tokens":300,"output_tokens":240,"reasoning_output_tokens":30,"total_tokens":1440}},
                            {"kind":"token_count","timestamp":"","turn_id":"demo-turn","usage":{"input_tokens":1200,"cached_input_tokens":300,"output_tokens":240,"reasoning_output_tokens":30,"total_tokens":1440},"last":{"input_tokens":1200,"cached_input_tokens":300,"output_tokens":240,"reasoning_output_tokens":30,"total_tokens":1440},"context_window":128000,"rate_limits":{"observed_at":"2026-10-21T10:00:00Z","primary":{"used_percent":22,"window_minutes":300,"resets_at":"2026-10-21T15:00:00Z"},"secondary":{"used_percent":8,"window_minutes":10080,"resets_at":"2026-10-27T10:00:00Z"}}},
                            {"kind":"tool_event","timestamp":"2026-10-21T10:00:02Z","item_id":"demo-tool","name":"McpToolCall · search","status":"failed","duration_seconds":1.4},
                            {"kind":"tool_start","timestamp":"2026-10-21T10:00:04Z","call_id":"demo-active","name":"CommandExecution"},
                            {"kind":"agent_link","timestamp":"","parent_id":"demo-session","children":["demo-agent-1"]},
                            {"kind":"agent_activity","timestamp":"","agent_id":"demo-agent-1","agent_path":"reviewer","model":"gpt-6-luna","effort":"low"},
                            {"kind":"file_change","timestamp":"","files":["src/example.py"]},
                            {"kind":"skill_observed","timestamp":"","name":"demo-skill"}],"freshness_seconds":0}]

    def sessions(self):
        if self.demo: return [{k:v for k,v in s.items() if k!="events"} for s in self.demo_sessions]
        with self.lock:
            if self._sessions_cache is not None and time.time()-self._sessions_cache_at < 10:
                return self._sessions_cache[:]
        db = self.root / "state_5.sqlite"
        rows = []
        try:
            uri = "file:" + quote(str(db), safe="/") + "?mode=ro"
            with closing(sqlite3.connect(uri, uri=True, timeout=.2)) as cx:
                cx.row_factory = sqlite3.Row
                columns = {r[1] for r in cx.execute("PRAGMA table_info(threads)")}
                fields = [x for x in ("id","rollout_path","title","cwd","model","reasoning_effort","agent_role","agent_path","parent_thread_id","updated_at","archived") if x in columns]
                if "id" in columns and "rollout_path" in columns:
                    query = "SELECT " + ",".join(fields) + " FROM threads"
                    if "updated_at" in columns: query += " ORDER BY updated_at DESC"
                    rows = [dict(r) for r in cx.execute(query + " LIMIT 2000")]
        except (OSError, sqlite3.Error): pass
        out, known = [], set()
        for row in rows:
            path = pathlib.Path(row.get("rollout_path") or "")
            if not path.is_absolute(): path = self.root / path
            path = self._validated_path(path)
            if path and path.exists():
                out.append({"id": row["id"], "title": safe_text(row.get("title") or row["id"], 160), "cwd": safe_text(row.get("cwd"), 250),
                            "updated_at": positive_int(row.get("updated_at")), "model": safe_text(row.get("model"), 80), "effort": safe_text(row.get("reasoning_effort"), 30),
                            "role": safe_text(row.get("agent_role"), 50), "agent_path": safe_text(row.get("agent_path"), 250), "parent_id": safe_text(row.get("parent_thread_id"), 100), "path": str(path)})
                known.add(str(path))
        if not out:
            for p in sorted((self.root / "sessions").glob("**/*.jsonl"), key=lambda x:x.stat().st_mtime, reverse=True)[:1000] if (self.root/"sessions").exists() else []:
                path = self._validated_path(p)
                if not path or str(path) in known: continue
                first = self._first_meta(path)
                if first: out.append({"id": first.get("session_id") or path.stem, "title": path.stem[:160], "cwd": first.get("cwd", ""), "updated_at": int(path.stat().st_mtime), "path": str(path), **{k:first.get(k,"") for k in ("model","role","agent_path","parent_id")}})
        with self.lock:
            self._sessions_cache, self._sessions_cache_at = out, time.time()
        return out

    def _validated_path(self, path):
        try:
            resolved = path.resolve(strict=True)
            if resolved.is_relative_to(self.root) and resolved.is_file() and resolved.suffix == ".jsonl": return resolved
        except (OSError, RuntimeError): pass
        return None

    @staticmethod
    def _first_meta(path):
        try:
            with path.open("rb") as f:
                while True:
                    line=f.readline(MAX_LINE_BYTES+1)
                    if not line: break
                    if len(line)>MAX_LINE_BYTES:
                        while line and not line.endswith(b"\n"): line=f.readline(MAX_LINE_BYTES+1)
                        continue
                    rec=json_line(line)
                    clean=sanitize_record(rec) if rec else None
                    if clean and clean["kind"]=="session_meta": return clean
        except OSError: pass
        return None

    def session(self, sid):
        if self.demo:
            demo = self.demo_sessions[0] if sid == "demo-session" else None
            if not demo: return None
            value=self.aggregate(demo,demo["events"],0)
            value["loading_progress"]={"bytes_read":1,"file_size":1,"complete":True}
            for key in ("_seen_responses","_token_snapshot_present","_thread_snapshot_present","_thread_snapshot","_token_count_snapshot","_per_response_tokens","_usage_records_present","_turn_state_seen","_unmatched_tool_ends","_wall_start","_wall_end"): value.pop(key,None)
            return value
        if not SAFE_ID.fullmatch(sid): return None
        record = next((x for x in self.sessions() if x["id"] == sid), None)
        if not record: return None
        path = self._validated_path(pathlib.Path(record["path"]))
        if not path: return None
        with self.lock:
            reader=self.readers.get(str(path))
            if reader is None:
                if len(self.readers)>=32: self.readers.pop(next(iter(self.readers)))
                reader=RolloutReader(path); self.readers[str(path)]=reader
        with reader.lock:
            return self._read_session(record, path, reader)

    def _read_session(self, record, path, reader):
        events=reader.read()
        meta=next((e for e in events if e["kind"]=="session_meta"),None) or (reader.summary.get("meta") if isinstance(reader.summary,dict) else None)
        if meta:
            for key, source in (("parent_id","parent_id"),("agent_path","agent_path"),("role","role"),("model","model")):
                if meta.get(source) and not record.get(key): record[key]=meta[source]
        try: freshness_source = reader.last_event_time or path.stat().st_mtime
        except OSError: freshness_source = reader.last_event_time
        freshness = max(0, time.time() - freshness_source) if freshness_source else time.time()
        result = self.aggregate(record, events, freshness, reader.summary if isinstance(reader.summary,dict) else None)
        if reader.summary: result = merge_summary(result, reader.summary)
        result["loading_progress"]={"bytes_read":reader.offset,"file_size":reader.file_size,"complete":reader.offset>=reader.file_size and not reader.pending and not reader.skipping_line,"skipped_lines":reader.skipped_lines}
        for key in ("_seen_responses","_token_snapshot_present","_thread_snapshot_present","_thread_snapshot","_token_count_snapshot","_per_response_tokens","_usage_records_present","_turn_state_seen","_unmatched_tool_ends","_wall_start","_wall_end"):
            result.pop(key,None)
        return result

    @staticmethod
    def aggregate(record, events, freshness, prior=None):
        prior = prior or {}
        calls={}; paired_starts=set(); tools=[]; files=[]; skills=[]; turns=[]; agent_map={}; model=prior.get("model") or record.get("model",""); effort=prior.get("effort") or record.get("effort",""); context=0; tokens={}; model_tokens={}; limits=None; compactions=0; now=""; recent=[]
        seen_responses=list(prior.get("seen_responses",[])); seen_usage=set(seen_responses); usage_by_response={}; latest_snapshot=None; snapshot_index=-1; latest_thread_snapshot=None; thread_snapshot_index=-1
        for event_index, ev in enumerate(events):
            if ev["kind"]=="compacted": compactions+=1
            elif ev["kind"]=="turn_context": model=ev.get("model") or model; effort=ev.get("effort") or effort; context=ev.get("context_window") or context
            elif ev["kind"] in ("token_usage_record","token_count"):
                usage=ev.get("usage") or {}; ident=(ev.get("turn_id"),ev.get("response_id"),tuple(sorted(usage.items())))
                if ev["kind"]=="token_count":
                    if any(value is not None for value in usage.values()):
                        latest_snapshot=usage; snapshot_index=event_index
                    context=ev.get("context_window") or context; limits=ev.get("rate_limits") or limits
                else:
                    thread_usage=ev.get("thread_usage") or {}
                    if any(value is not None for value in thread_usage.values()):
                        latest_thread_snapshot=thread_usage; thread_snapshot_index=event_index
                    response_id=ev.get("response_id")
                    response_key=response_id or event_index
                    if not response_id or response_id not in seen_usage:
                        if response_id:
                            seen_usage.add(response_id); seen_responses.append(response_id)
                        usage_by_response[response_key]=(usage, model or "unknown model", event_index)
                    context=ev.get("context_window") or context
            elif ev["kind"]=="task_started": now=ev["timestamp"]
            elif ev["kind"]=="task_complete": now=""
            elif ev["kind"]=="tool_event":
                key=ev.get("item_id") or f"event-{len(calls)}"
                previous=calls.get(key,{})
                calls[key]={"call_id":key,"name":ev.get("name"),"started":previous.get("started") or ev["timestamp"],"ended":ev["timestamp"],"duration_seconds":ev.get("duration_seconds"),"status":ev.get("status")}
            elif ev["kind"]=="tool_start":
                key=ev.get("call_id") or f"{ev.get('name')}:{ev['timestamp']}"; calls[key]={"call_id":key,"name":ev.get("name") or "tool", "started":ev["timestamp"], "ended":""}
                paired_starts.add(key)
            elif ev["kind"]=="tool_end":
                call=calls.get(ev.get("call_id"));
                if call: call["ended"]=ev["timestamp"]
            elif ev["kind"]=="file_change": files.extend(ev.get("files",[]))
            elif ev["kind"]=="skill_observed": skills.append({"name":ev.get("name") or "skill", "source":"observed event"})
            elif ev["kind"]=="agent_activity":
                aid=ev.get("agent_id") or "unknown-agent"; agent_map.setdefault(aid,{"agent_id":aid}).update({k:ev.get(k) for k in ("agent_path","model","effort") if ev.get(k)})
            elif ev["kind"]=="agent_link":
                for child in ev.get("children",[]): agent_map.setdefault(child,{"agent_id":child})["parent_id"]=ev.get("parent_id")
            elif ev["kind"]=="token_count" and ev.get("rate_limits"): limits=ev["rate_limits"]
        has_thread_snapshot=bool(latest_thread_snapshot)
        has_count_snapshot=bool(latest_snapshot) and any(value is not None for value in latest_snapshot.values())
        if has_thread_snapshot:
            tokens={k:positive_int(v) for k,v in latest_thread_snapshot.items() if v is not None}
        elif has_count_snapshot:
            tokens={k:positive_int(v) for k,v in latest_snapshot.items() if v is not None}
        per_response_tokens={}
        if usage_by_response:
            for usage, usage_model, response_index in usage_by_response.values():
                model_tokens.setdefault(usage_model,{})
                for key,value in usage.items():
                    if value is None: continue
                    model_tokens[usage_model][key]=model_tokens[usage_model].get(key,0)+positive_int(value)
                    per_response_tokens[key]=per_response_tokens.get(key,0)+positive_int(value)
                    if (not has_thread_snapshot and not has_count_snapshot) or (has_thread_snapshot and response_index > thread_snapshot_index) or (not has_thread_snapshot and has_count_snapshot and response_index > snapshot_index):
                        tokens[key]=tokens.get(key,0)+positive_int(value)
        if record.get("model_tokens") and not model_tokens: model_tokens=record["model_tokens"]
        for c in calls.values():
            a=parse_time(c["started"]); b=parse_time(c["ended"])
            if c.get("duration_seconds") is None and c.get("call_id") in paired_starts:
                c["duration_seconds"]=max(0,b-a) if a and b else None
        tool_list=list(calls.values())[-MAX_CALLS:]
        active=[c for c in tool_list if not c["ended"]]
        if not now and active and not any(e["kind"] in ("task_started","task_complete") for e in events) and not prior.get("has_turn_state"): now=active[-1]["started"]
        starts=[parse_time(e["timestamp"]) for e in events if e["kind"]=="task_started" and parse_time(e["timestamp"])]
        ends=[parse_time(e["timestamp"]) for e in events if e["kind"]=="task_complete" and parse_time(e["timestamp"])]
        recorded_turn_durations=[e["duration_seconds"] for e in events if e["kind"]=="task_complete" and e.get("duration_seconds") is not None]
        first_token_latencies=[e["first_token_seconds"] for e in events if e["kind"]=="task_complete" and e.get("first_token_seconds") is not None]
        for e in events:
            if e["kind"]=="token_count": recent.append({"timestamp":e["timestamp"],"total_tokens":e["usage"].get("total_tokens",0)})
        if record.get("parent_id"):
            agent_map[record["id"]]={"agent_id":record["id"],"parent_id":record["parent_id"],"agent_path":record.get("agent_path",""),"model":model,"effort":effort}
        agents=build_agent_tree(agent_map.values())
        tool_stats=tools_statistics(tool_list)
        return {"id":record["id"],"title":record.get("title",record["id"]),"cwd":record.get("cwd",""),"model":model,"effort":effort,
                "freshness_seconds":int(freshness),"active_at":now,"active_is_stale":bool(now and freshness>90),"context_window":context,
                "tokens":tokens,"token_source":"thread cumulative" if has_thread_snapshot else "token_count cumulative" if has_count_snapshot else "per-response sum" if usage_by_response else "unknown",
                "token_discrepancy":{"thread_total":latest_thread_snapshot.get("total_tokens"),"token_count_total":latest_snapshot.get("total_tokens"),"delta":latest_thread_snapshot.get("total_tokens")-latest_snapshot.get("total_tokens")} if has_thread_snapshot and has_count_snapshot and latest_thread_snapshot.get("total_tokens") is not None and latest_snapshot.get("total_tokens") is not None and latest_thread_snapshot.get("total_tokens")!=latest_snapshot.get("total_tokens") else None,
                "model_tokens":model_tokens,"limits":limits,"tools":tool_list,"tool_stats":tool_stats,"active_tools":active,"files_changed":list(dict.fromkeys(files))[-300:],
                "turns":{"started":sum(e["kind"]=="task_started" for e in events),"completed":sum(e["kind"]=="task_complete" for e in events),"recorded_duration_seconds":sum(recorded_turn_durations) if recorded_turn_durations else None,
                         "first_token_latency_seconds":sum(first_token_latencies) if first_token_latencies else None,
                         "tool_elapsed_seconds":sum(c["duration_seconds"] for c in tool_list if c.get("duration_seconds") is not None) if any(c.get("duration_seconds") is not None for c in tool_list) else None,
                         "wall_seconds":max(ends,default=0)-min(starts) if starts and ends else None},
                "agents":agents[-100:],"skills":skills[-100:],"compactions":compactions,"recent_calls":recent[-100:],"log":[{"timestamp":e.get("timestamp"),"kind":e["kind"],"label":e.get("name") or e.get("item_type") or e["kind"]} for e in events[-100:]],"cost":None,
                "_seen_responses":seen_responses[-MAX_EVENTS:],"_token_snapshot_present":has_thread_snapshot or has_count_snapshot,"_thread_snapshot_present":has_thread_snapshot,
                "_thread_snapshot":latest_thread_snapshot,"_token_count_snapshot":latest_snapshot,"_per_response_tokens":per_response_tokens,"_usage_records_present":bool(usage_by_response),
                "_turn_state_seen":any(e["kind"] in ("task_started","task_complete") for e in events),
                "_unmatched_tool_ends":[e for e in events if e["kind"]=="tool_end" and e.get("call_id") not in calls],
                "_wall_start":min(starts) if starts else None,"_wall_end":max(ends) if ends else None}


def parse_time(value):
    import datetime
    try: return datetime.datetime.fromisoformat(value.replace("Z","+00:00")).timestamp()
    except (ValueError, AttributeError, OverflowError): return 0


class Handler(BaseHTTPRequestHandler):
    dashboard = None
    def log_message(self, *_): pass
    def _send(self, code, body, content_type="application/json; charset=utf-8"):
        raw=body.encode("utf-8")
        self.send_response(code); self.send_header("Content-Type",content_type); self.send_header("Content-Length",str(len(raw))); self.send_header("X-Content-Type-Options","nosniff"); self.send_header("Cache-Control","no-store"); self.send_header("Content-Security-Policy","default-src 'self'; style-src 'self'; style-src-attr 'unsafe-inline'; script-src 'self'; object-src 'none'; base-uri 'none'"); self.end_headers(); self.wfile.write(raw)
    def _authorized(self):
        host_header=self.headers.get("Host","")
        host=host_header[1:host_header.find("]")] if host_header.startswith("[") and "]" in host_header else host_header.rsplit(":",1)[0] if ":" in host_header else host_header
        host=host.lower()
        if host not in ("127.0.0.1","localhost","::1"): return False
        origin=self.headers.get("Origin")
        try: return not origin or urlsplit(origin).hostname in ("127.0.0.1","localhost","::1")
        except ValueError: return False
    def do_POST(self): self._send(405,"{}")
    def do_GET(self):
        if not self._authorized(): return self._send(403,"{}")
        u=urlsplit(self.path)
        if u.path=="/":
            base=pathlib.Path(__file__).parent/"static"/"index.html"
            return self._send(200,base.read_text(),"text/html; charset=utf-8")
        if u.path in ("/app.js", "/style.css"):
            asset=pathlib.Path(__file__).parent/"static"/u.path.lstrip("/")
            mime="application/javascript; charset=utf-8" if u.path.endswith(".js") else "text/css; charset=utf-8"
            return self._send(200,asset.read_text(),mime)
        if u.path=="/api/sessions":
            q=parse_qs(u.query).get("q",[""])[0][:160].casefold()
            rows=[x for x in self.dashboard.sessions() if q in (x.get("title","")+" "+x.get("cwd","")+" "+x["id"]).casefold()]
            return self._send(200,json.dumps(rows,ensure_ascii=False))
        if u.path.startswith("/api/session/"):
            sid=u.path.rsplit("/",1)[-1]; value=self.dashboard.session(sid)
            return self._send(200 if value else 404,json.dumps(value or {"error":"not_found"},ensure_ascii=False))
        return self._send(404,"{}")


def main():
    ap=argparse.ArgumentParser(description=__doc__); ap.add_argument("--codex-home",default=os.environ.get("CODEX_HOME",str(pathlib.Path.home()/".codex"))); ap.add_argument("--host",default="127.0.0.1"); ap.add_argument("--port",type=int,default=8765); ap.add_argument("--demo",action="store_true"); args=ap.parse_args()
    if args.host not in ("127.0.0.1","localhost","::1"): ap.error("only loopback hosts are allowed")
    Handler.dashboard=Dashboard(args.codex_home,args.demo)
    class LocalServer(ThreadingHTTPServer):
        address_family = socket.AF_INET6 if args.host == "::1" else socket.AF_INET
    server=LocalServer((args.host,args.port),Handler)
    display_host = "[::1]" if args.host == "::1" else args.host
    print(f"Codex Activity Dashboard: http://{display_host}:{server.server_port}/ (press Ctrl-C to stop)")
    try: server.serve_forever()
    except KeyboardInterrupt: pass
    finally: server.server_close()

if __name__=="__main__": main()

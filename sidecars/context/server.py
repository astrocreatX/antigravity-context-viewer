#!/usr/bin/env python3
import os
import sys
import re
import json
import time
import sqlite3
import subprocess
import urllib.request
from datetime import datetime, timezone
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

def get_db_path():
    app_data = os.environ.get("ANTIGRAVITY_APP_DATA_DIR", os.path.expanduser("~/.gemini/antigravity"))
    return os.path.join(app_data, "conversation_summaries.db")

def get_view_times():
    """Reads last_user_view_time from annotations/*.pbtxt files (updated when user opens conversation)."""
    app_data = os.environ.get("ANTIGRAVITY_APP_DATA_DIR", os.path.expanduser("~/.gemini/antigravity"))
    ann_dir = os.path.join(app_data, "annotations")
    view_times = {}
    if os.path.isdir(ann_dir):
        for fname in os.listdir(ann_dir):
            if fname.endswith(".pbtxt"):
                cid = fname[:-6]
                fpath = os.path.join(ann_dir, fname)
                try:
                    with open(fpath, "r", encoding="utf-8") as f:
                        content = f.read()
                        m_sec = re.search(r"last_user_view_time\s*:\s*\{\s*seconds\s*:\s*(\d+)", content)
                        m_nano = re.search(r"nanos\s*:\s*(\d+)", content)
                        sec = int(m_sec.group(1)) if m_sec else 0
                        nano = int(m_nano.group(1)) if m_nano else 0
                        view_times[cid] = sec + (nano / 1e9)
                except Exception:
                    pass
    return view_times

def get_conversations_list():
    db_path = get_db_path()
    app_data = os.environ.get("ANTIGRAVITY_APP_DATA_DIR", os.path.expanduser("~/.gemini/antigravity"))
    brain_dir = os.path.join(app_data, "brain")
    ann_dir = os.path.join(app_data, "annotations")
    view_times = {}
    ann_titles = {}

    if os.path.isdir(ann_dir):
        for fname in os.listdir(ann_dir):
            if fname.endswith(".pbtxt"):
                cid = fname[:-6]
                fpath = os.path.join(ann_dir, fname)
                try:
                    with open(fpath, "r", encoding="utf-8") as f:
                        content = f.read()
                        m_title = re.search(r'title\s*:\s*"([^"]+)"', content)
                        if m_title:
                            ann_titles[cid] = m_title.group(1)
                        m_sec = re.search(r"last_user_view_time\s*:\s*\{\s*seconds\s*:\s*(\d+)", content)
                        m_nano = re.search(r"nanos\s*:\s*(\d+)", content)
                        sec = int(m_sec.group(1)) if m_sec else 0
                        nano = int(m_nano.group(1)) if m_nano else 0
                        view_times[cid] = sec + (nano / 1e9)
                except Exception:
                    pass

    convs = []
    if os.path.isfile(db_path):
        try:
            conn = sqlite3.connect(db_path, timeout=1.0)
            c = conn.cursor()
            c.execute("""
                SELECT conversation_id, title, preview, last_modified_time, last_user_input_time, workspace_uris 
                FROM conversation_summaries 
                WHERE killed = 0 
                ORDER BY last_modified_time DESC
                LIMIT 40
            """)
            rows = c.fetchall()
            conn.close()

            for r in rows:
                cid, title, preview, lmod_str, linput_str, ws_uris = r

                # Only include active conversations that have transcripts on disk
                tp = os.path.join(brain_dir, cid, ".system_generated", "logs", "transcript.jsonl")
                cdb = os.path.join(app_data, "conversations", f"{cid}.db")
                if not os.path.isfile(tp) and not os.path.isfile(cdb):
                    continue

                # Title resolution: title -> preview -> annotations -> fallback
                res_title = title.strip() if title else ""
                if not res_title and preview:
                    res_title = preview.strip()
                if not res_title and cid in ann_titles:
                    res_title = ann_titles[cid]
                if not res_title:
                    res_title = "Untitled session"

                # Convert SQLite datetime to epoch timestamp
                lmod_ts = 0.0
                if lmod_str:
                    try:
                        dt_clean = lmod_str.split("+")[0].strip()
                        lmod_ts = datetime.fromisoformat(dt_clean).timestamp()
                    except Exception:
                        pass

                linput_ts = 0.0
                if linput_str:
                    try:
                        dt_clean = linput_str.split("+")[0].strip()
                        linput_ts = datetime.fromisoformat(dt_clean).timestamp()
                    except Exception:
                        pass

                # Check last_user_view_time (when user opened/viewed conversation in UI)
                v_time = view_times.get(cid, 0.0)

                # Prioritize actual user view time (open tab in UI).
                effective_time = v_time if v_time > 0 else (linput_ts if linput_ts > 0 else lmod_ts)

                convs.append({
                    "id": cid,
                    "title": res_title,
                    "view_time": v_time,
                    "input_time": linput_ts,
                    "mod_time": lmod_ts,
                    "effective_time": effective_time,
                    "workspace": ws_uris
                })
        except Exception:
            pass

    # Sort strictly by effective user activity (view time or user message time)
    convs.sort(key=lambda x: x["effective_time"], reverse=True)
    return convs[:25]

def get_transcript_for_conv(conv_id):
    app_data = os.environ.get("ANTIGRAVITY_APP_DATA_DIR", os.path.expanduser("~/.gemini/antigravity"))
    return os.path.join(app_data, "brain", conv_id, ".system_generated", "logs", "transcript.jsonl")


def get_token_info(conv_id=None, is_new=False):
    convs = get_conversations_list()

    # If user is on "+ New Conversation" screen
    if is_new or conv_id == "new":
        return {
            "conv_id": "new",
            "title": "New Conversation",
            "is_new": True,
            "total": 0, "max": 1_000_000, "pct": 0.0, "pct_round": 0,
            "cached": 0, "cached_pct": 0.0,
            "input": 0, "input_pct": 0.0,
            "output": 0, "output_pct": 0.0,
            "free": 1_000_000, "free_pct": 100.0,
            "workspace_path": None,
            "conversations": convs
        }

    target_id = conv_id

    # If "auto" or empty: pick the session the user is currently viewing/active in
    if not target_id or target_id == "auto":
        if convs:
            target_id = convs[0]["id"]
        else:
            target_id = os.environ.get("ANTIGRAVITY_CONVERSATION_ID", "")

    transcript_file = get_transcript_for_conv(target_id) if target_id else None

    # Fallback to scanning brain directory ONLY if no explicit session was requested
    if (not conv_id or conv_id == "auto") and (not transcript_file or not os.path.isfile(transcript_file)):
        app_data = os.environ.get("ANTIGRAVITY_APP_DATA_DIR", os.path.expanduser("~/.gemini/antigravity"))
        brain_dir = os.path.join(app_data, "brain")
        candidates = []
        if os.path.exists(brain_dir):
            try:
                for entry in os.listdir(brain_dir):
                    tp = os.path.join(brain_dir, entry, ".system_generated", "logs", "transcript.jsonl")
                    if os.path.isfile(tp):
                        candidates.append((os.path.getmtime(tp), tp, entry))
            except Exception:
                pass
        if candidates:
            candidates.sort(key=lambda x: x[0], reverse=True)
            transcript_file = candidates[0][1]
            target_id = candidates[0][2]

    curr_title = "Active session"
    for c in convs:
        if c["id"] == target_id:
            curr_title = c["title"]
            break

    if not transcript_file or not os.path.isfile(transcript_file):
        return {
            "conv_id": target_id or "unknown",
            "title": curr_title,
            "total": 0, "max": 1_000_000, "pct": 0.0, "pct_round": 0,
            "cached": 0, "cached_pct": 0.0,
            "input": 0, "input_pct": 0.0,
            "output": 0, "output_pct": 0.0,
            "free": 1_000_000, "free_pct": 100.0,
            "conversations": convs
        }

    last_step = None
    total_output = 0
    workspace_path = None
    try:
        with open(transcript_file, "r", encoding="utf-8") as f:
            for line in f:
                if '"input_tokens"' in line:
                    try:
                        d = json.loads(line)
                        if "input_tokens" in d:
                            last_step = d
                            total_output += d.get("output_tokens", 0)
                    except Exception:
                        pass
                if not workspace_path and '"workspace' in line:
                    try:
                        d = json.loads(line)
                        ws_list = d.get("workspacePaths") or d.get("workspace_paths")
                        if ws_list and len(ws_list) > 0:
                            workspace_path = ws_list[0]
                    except Exception:
                        pass
    except Exception:
        pass

    if not last_step:
        return {
            "conv_id": target_id,
            "title": curr_title,
            "total": 0, "max": 1_000_000, "pct": 0.0, "pct_round": 0,
            "cached": 0, "cached_pct": 0.0,
            "input": 0, "input_pct": 0.0,
            "output": 0, "output_pct": 0.0,
            "free": 1_000_000, "free_pct": 100.0,
            "workspace_path": workspace_path,
            "conversations": convs
        }

    inp = last_step.get("input_tokens", 0)
    cached = last_step.get("cache_read_tokens", 0)
    total = inp + cached
    max_c = 1_000_000
    pct = (total / max_c) * 100
    pct_round = int(round(pct))
    cached_pct = (cached / max_c) * 100
    input_pct = (inp / max_c) * 100
    output_pct = (total_output / max_c) * 100
    free = max(0, max_c - total)
    free_pct = (free / max_c) * 100

    return {
        "conv_id": target_id,
        "title": curr_title,
        "total": total,
        "max": max_c,
        "pct": round(pct, 1),
        "pct_round": pct_round,
        "cached": cached,
        "cached_pct": round(cached_pct, 1),
        "input": inp,
        "input_pct": round(input_pct, 1),
        "output": total_output,
        "output_pct": round(output_pct, 1),
        "free": free,
        "free_pct": round(free_pct, 1),
        "workspace_path": workspace_path,
        "conversations": convs
    }

def get_git_info(suggested_ws=None):
    ws = suggested_ws or os.environ.get("WORKSPACE_DIR", "")
    if not ws or not os.path.exists(ws):
        ws = os.getcwd()
    
    if not os.path.exists(os.path.join(ws, ".git")):
        if os.path.exists(os.path.join(os.getcwd(), ".git")):
            ws = os.getcwd()

    branch = "unknown"
    is_worktree = False
    worktree_name = ""
    worktree_path = ""
    
    try:
        branch = subprocess.check_output(
            ["git", "-C", ws, "rev-parse", "--abbrev-ref", "HEAD"],
            stderr=subprocess.DEVNULL, text=True
        ).strip()
    except Exception:
        pass

    try:
        wt_out = subprocess.check_output(
            ["git", "-C", ws, "worktree", "list", "--porcelain"],
            stderr=subprocess.DEVNULL, text=True
        ).strip()
        lines = wt_out.splitlines()
        worktrees = []
        for line in lines:
            if line.startswith("worktree "):
                worktrees.append(line.split(" ", 1)[1])
        
        if worktrees:
            main_wt = worktrees[0]
            real_ws = os.path.realpath(ws)
            real_main = os.path.realpath(main_wt)
            if real_ws != real_main and any(real_ws == os.path.realpath(w) for w in worktrees[1:]):
                is_worktree = True
                worktree_name = os.path.basename(ws)
                worktree_path = ws
    except Exception:
        pass

    dot_git = os.path.join(ws, ".git")
    if os.path.isfile(dot_git):
        is_worktree = True
        worktree_name = os.path.basename(ws)
        worktree_path = ws

    ws_short = ws.replace(os.path.expanduser("~"), "~")
    parent_dir = os.path.dirname(ws_short)
    ws_base = os.path.basename(ws)

    return {
        "workspace_name": ws_base,
        "workspace_path_short": f"{ws_base} · {parent_dir}",
        "full_path": ws_short,
        "branch": branch,
        "is_worktree": is_worktree,
        "worktree_name": worktree_name,
        "worktree_path": worktree_path.replace(os.path.expanduser("~"), "~")
    }

HTML_PAGE = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Context & Environment</title>
  <style>
    :root {
      color-scheme: dark light;
      --bg: #282828;
      --card-bg: #32302f;
      --card-border: rgba(235, 219, 178, 0.12);
      --card-active-border: rgba(125, 174, 163, 0.6);
      --text: #ebdbb2;
      --text-muted: #928374;
      --blue: #7daea3;
      --orange: #e78a4e;
      --green: #a9b665;
      --gray: rgba(146, 131, 116, 0.35);
      --select-bg: #3c3836;
      --pill-bg: rgba(235, 219, 178, 0.08);
      --pill-border: rgba(235, 219, 178, 0.14);
      --tooltip-bg: #1d2021;
    }

    @media (prefers-color-scheme: light) {
      :root {
        --bg: #fbf1c7;
        --card-bg: #f4ebd5;
        --card-border: rgba(60, 56, 54, 0.14);
        --card-active-border: rgba(69, 112, 122, 0.6);
        --text: #282828;
        --text-muted: #7c6f64;
        --blue: #45707a;
        --orange: #c35e0a;
        --green: #6c782e;
        --gray: rgba(168, 153, 132, 0.35);
        --select-bg: #ebdbb2;
        --pill-bg: rgba(60, 56, 54, 0.06);
        --pill-border: rgba(60, 56, 54, 0.14);
        --tooltip-bg: #282828;
      }
    }

    body.vscode-light {
      --bg: #fbf1c7;
      --card-bg: #f4ebd5;
      --card-border: rgba(60, 56, 54, 0.14);
      --card-active-border: rgba(69, 112, 122, 0.6);
      --text: #282828;
      --text-muted: #7c6f64;
      --blue: #45707a;
      --orange: #c35e0a;
      --green: #6c782e;
      --gray: rgba(168, 153, 132, 0.35);
      --select-bg: #ebdbb2;
      --pill-bg: rgba(60, 56, 54, 0.06);
      --pill-border: rgba(60, 56, 54, 0.14);
      --tooltip-bg: #282828;
    }

    * { box-sizing: border-box; margin: 0; padding: 0; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; }
    html, body {
      background: var(--bg) !important;
      color: var(--text);
      font-size: 13px;
      line-height: 1.4;
      user-select: none;
      -webkit-font-smoothing: antialiased;
    }
    body {
      padding: 12px;
    }
    .container {
      max-width: 100%;
      margin: 0 auto;
      display: flex;
      flex-direction: column;
      gap: 10px;
    }
    .session-bar {
      display: flex;
      align-items: center;
      gap: 6px;
      margin-bottom: 2px;
    }
    .session-select {
      flex: 1;
      min-width: 0;
      background: var(--select-bg);
      border: 1px solid var(--card-border);
      color: var(--text);
      font-size: 11px;
      padding: 5px 8px;
      border-radius: 6px;
      outline: none;
      cursor: pointer;
      text-overflow: ellipsis;
      white-space: nowrap;
      overflow: hidden;
    }
    .btn-auto-reset {
      background: var(--pill-bg);
      border: 1px solid var(--card-border);
      color: var(--text-muted);
      font-size: 11px;
      font-weight: 500;
      padding: 5px 9px;
      border-radius: 6px;
      cursor: pointer;
      white-space: nowrap;
      transition: all 0.15s ease;
      display: flex;
      align-items: center;
      gap: 4px;
      flex-shrink: 0;
    }
    .btn-auto-reset:hover {
      background: rgba(255, 255, 255, 0.1);
      color: var(--text);
    }
    .btn-auto-reset.active-manual {
      background: var(--blue);
      border-color: var(--blue);
      color: #ffffff;
      font-weight: 600;
    }
    .card {
      background: var(--card-bg);
      border: 1px solid var(--card-border);
      border-radius: 8px;
      padding: 10px 12px;
      transition: border-color 0.15s ease;
    }
    .card-header {
      display: flex;
      align-items: center;
      justify-content: space-between;
      cursor: pointer;
      font-size: 12px;
      font-weight: 500;
      color: var(--text);
    }
    .card-header-left {
      display: flex;
      align-items: center;
      gap: 6px;
    }
    .chevron {
      width: 10px;
      height: 10px;
      color: var(--text-muted);
      transition: transform 0.15s ease;
      display: inline-block;
      flex-shrink: 0;
    }
    .chevron.open {
      transform: rotate(90deg);
    }
    .env-row {
      display: flex;
      align-items: center;
      gap: 8px;
      margin-top: 8px;
      color: var(--text);
      font-size: 12px;
    }
    .env-row .icon {
      color: var(--text-muted);
      width: 14px;
      height: 14px;
      flex-shrink: 0;
    }
    .branch-pill {
      display: inline-flex;
      align-items: center;
      gap: 6px;
      background: var(--pill-bg);
      border: 1px solid var(--pill-border);
      padding: 2px 7px;
      border-radius: 5px;
      font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
      font-size: 11px;
      cursor: pointer;
      max-width: 200px;
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
      transition: background 0.15s ease;
    }
    .branch-pill:hover {
      filter: brightness(1.1);
    }
    .worktree-label {
      color: var(--text-muted);
      font-size: 11px;
      margin-left: 4px;
    }
    .tooltip-container {
      position: relative;
    }
    .tooltip {
      visibility: hidden;
      opacity: 0;
      position: absolute;
      bottom: 125%;
      left: 0;
      background: var(--tooltip-bg);
      color: #fafafa;
      padding: 4px 8px;
      border-radius: 4px;
      font-size: 11px;
      white-space: nowrap;
      z-index: 100;
      box-shadow: 0 4px 12px rgba(0,0,0,0.3);
      border: 1px solid var(--card-border);
      transition: opacity 0.15s ease;
      pointer-events: none;
    }
    .tooltip-container:hover .tooltip {
      visibility: visible;
      opacity: 1;
    }
    .pill-preview {
      display: flex;
      align-items: center;
      gap: 8px;
    }
    .pill-bar {
      width: 28px;
      height: 7px;
      background: var(--gray);
      border-radius: 3px;
      overflow: hidden;
    }
    .pill-fill {
      height: 100%;
      background: var(--blue);
      border-radius: 3px;
      transition: width 0.3s ease;
    }
    .pill-text {
      font-size: 12px;
      font-weight: 600;
      color: var(--text);
    }
    .context-expanded {
      display: none;
      margin-top: 10px;
      animation: fadeIn 0.15s ease;
    }
    .context-expanded.open {
      display: block;
    }
    @keyframes fadeIn {
      from { opacity: 0; transform: translateY(-2px); }
      to { opacity: 1; transform: translateY(0); }
    }
    .headline {
      display: flex;
      justify-content: space-between;
      align-items: baseline;
      margin-bottom: 7px;
    }
    .headline-main {
      font-size: 14px;
      font-weight: 600;
    }
    .headline-sub {
      font-size: 12px;
      color: var(--text-muted);
      font-variant-numeric: tabular-nums;
    }
    .progress-bar-container {
      width: 100%;
      height: 5px;
      background: var(--gray);
      border-radius: 3px;
      display: flex;
      overflow: hidden;
      margin-bottom: 10px;
    }
    .bar-segment {
      height: 100%;
      transition: width 0.3s ease;
    }
    .bar-cached { background: var(--blue); }
    .bar-input { background: var(--orange); }
    .bar-output { background: var(--green); }
    .breakdown-list {
      display: flex;
      flex-direction: column;
      gap: 6px;
      margin-bottom: 10px;
    }
    .breakdown-item {
      display: flex;
      justify-content: space-between;
      align-items: center;
      font-size: 11px;
    }
    .item-label-group {
      display: flex;
      align-items: center;
      gap: 6px;
      color: var(--text);
    }
    .dot {
      width: 7px;
      height: 7px;
      border-radius: 2px;
      flex-shrink: 0;
    }
    .item-value-group {
      display: flex;
      gap: 10px;
      font-variant-numeric: tabular-nums;
    }
    .item-val {
      color: var(--text-muted);
      width: 44px;
      text-align: right;
    }
    .item-pct {
      color: var(--text);
      font-weight: 500;
      width: 36px;
      text-align: right;
    }
    .footer-actions {
      display: flex;
      justify-content: space-between;
      align-items: center;
      border-top: 1px solid var(--card-border);
      padding-top: 8px;
      margin-top: 4px;
    }
    .footer-note {
      font-size: 10px;
      color: var(--text-muted);
    }
    .btn-compact {
      background: var(--pill-bg);
      border: 1px solid var(--card-border);
      color: var(--text);
      padding: 3px 8px;
      border-radius: 5px;
      font-size: 10px;
      font-weight: 500;
      cursor: pointer;
    }
    .usage-details {
      display: none;
      margin-top: 8px;
    }
    .usage-details.open {
      display: block;
    }
    .usage-group {
      margin-bottom: 8px;
    }
    .usage-group:last-child {
      margin-bottom: 0;
    }
    .usage-group-title {
      font-size: 11px;
      font-weight: 600;
      color: var(--text);
      margin-bottom: 4px;
      margin-top: 6px;
    }
    .usage-group:first-child .usage-group-title {
      margin-top: 0;
    }
    .usage-row {
      display: flex;
      justify-content: space-between;
      align-items: flex-start;
      padding: 3px 0;
      font-size: 11px;
    }
    .usage-label {
      color: var(--text-muted);
      font-size: 11px;
    }
    .usage-value-container {
      display: flex;
      flex-direction: column;
      align-items: flex-end;
    }
    .usage-value-row {
      display: flex;
      align-items: center;
      gap: 6px;
    }
    .usage-pct {
      font-size: 11px;
      font-weight: 500;
      color: var(--text);
      font-variant-numeric: tabular-nums;
    }
    .usage-reset {
      font-size: 10px;
      color: var(--text-muted);
      margin-top: 1px;
      font-variant-numeric: tabular-nums;
    }
    .ring-svg {
      width: 14px;
      height: 14px;
      flex-shrink: 0;
    }
    .toast {
      position: fixed;
      bottom: 12px;
      left: 50%;
      transform: translateX(-50%);
      background: #2563eb;
      color: white;
      padding: 4px 10px;
      border-radius: 5px;
      font-size: 11px;
      opacity: 0;
      transition: opacity 0.2s ease;
      pointer-events: none;
      z-index: 1000;
    }
    .toast.show {
      opacity: 1;
    }
  </style>
</head>
<body>
  <div class="container">
    <div class="session-bar">
      <select class="session-select" id="sessionSelect" onchange="onSessionSelectChange(this.value)">
        <option value="auto">Auto (Current session)</option>
      </select>
      <button class="btn-auto-reset" id="btnAutoReset" onclick="resetToAuto()" title="Click to set Auto mode">Set Auto</button>
    </div>

    <div class="card">
      <div class="card-header" id="envToggle">
        <div class="card-header-left">
          <span>Environment</span>
          <svg class="chevron open" id="envChevron" viewBox="0 0 16 16"><path d="M6 4l4 4-4 4" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/></svg>
        </div>
      </div>
      <div id="envDetails">
        <div class="env-row tooltip-container">
          <svg class="icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"/></svg>
          <span id="wsPath" style="font-weight:500;">...</span>
          <div class="tooltip" id="wsTooltip">...</div>
        </div>
        
        <div class="env-row">
          <div class="tooltip-container">
            <div class="branch-pill" id="branchPill" onclick="copyBranch()">
              <svg style="width:12px; height:12px; flex-shrink:0;" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><line x1="6" y1="3" x2="6" y2="15"/><circle cx="18" cy="6" r="3"/><circle cx="6" cy="18" r="3"/><path d="M18 9a9 9 0 0 1-9 9"/></svg>
              <span id="gitBranch">...</span>
            </div>
            <div class="tooltip" id="branchTooltip">Branch (click to copy)</div>
          </div>
          <span id="wtLabel" class="worktree-label" style="display:none;">Worktree</span>
        </div>
      </div>
    </div>

    <div class="card" id="contextCard" style="border-color: var(--card-active-border);">
      <div class="card-header" id="contextToggle">
        <div class="card-header-left">
          <span>Context</span>
          <svg class="chevron open" id="contextChevron" viewBox="0 0 16 16"><path d="M6 4l4 4-4 4" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/></svg>
        </div>
        <div class="pill-preview" id="pillPreview">
          <span class="pill-text" id="pillPct">0%</span>
          <div class="pill-bar">
            <div class="pill-fill" id="pillFill" style="width: 0%;"></div>
          </div>
        </div>
      </div>

      <div class="context-expanded open" id="contextDetails">
        <div class="headline">
          <span class="headline-main" id="pctMain">0% used</span>
          <span class="headline-sub" id="tokensSub">0k / 1M</span>
        </div>

        <div class="progress-bar-container">
          <div class="bar-segment bar-cached" id="barCached" style="width: 0%;"></div>
          <div class="bar-segment bar-input" id="barInput" style="width: 0%;"></div>
          <div class="bar-segment bar-output" id="barOutput" style="width: 0%;"></div>
        </div>

        <div class="breakdown-list">
          <div class="breakdown-item">
            <div class="item-label-group">
              <span class="dot" style="background:var(--blue)"></span>
              <span>Prompt cache</span>
            </div>
            <div class="item-value-group">
              <span class="item-val" id="valCached">0k</span>
              <span class="item-pct" id="pctCached">0.0%</span>
            </div>
          </div>

          <div class="breakdown-item">
            <div class="item-label-group">
              <span class="dot" style="background:var(--orange)"></span>
              <span>Messages / Input</span>
            </div>
            <div class="item-value-group">
              <span class="item-val" id="valInput">0k</span>
              <span class="item-pct" id="pctInput">0.0%</span>
            </div>
          </div>

          <div class="breakdown-item">
            <div class="item-label-group">
              <span class="dot" style="background:var(--green)"></span>
              <span>Generated replies</span>
            </div>
            <div class="item-value-group">
              <span class="item-val" id="valOutput">0k</span>
              <span class="item-pct" id="pctOutput">0.0%</span>
            </div>
          </div>

          <div class="breakdown-item">
            <div class="item-label-group">
              <span class="dot" style="background:var(--gray)"></span>
              <span style="color:var(--text-muted)">Free space</span>
            </div>
            <div class="item-value-group">
              <span class="item-val" id="valFree">1M</span>
              <span class="item-pct" id="pctFree" style="color:var(--text-muted)">100.0%</span>
            </div>
          </div>
        </div>

        <div class="footer-actions">
          <span class="footer-note" id="bufferNote">Ready</span>
          <button class="btn-compact" onclick="copyCompact()">/compact</button>
        </div>
      </div>
    </div>

    <div class="card" id="usageCard">
      <div class="card-header" id="usageToggle">
        <div class="card-header-left">
          <span>Usage</span>
          <svg class="chevron open" id="usageChevron" viewBox="0 0 16 16"><path d="M6 4l4 4-4 4" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/></svg>
        </div>
      </div>
      <div class="usage-details open" id="usageDetails">
        <div id="usageContent"></div>
      </div>
    </div>
  </div>

  <div class="toast" id="toast">Copied to clipboard!</div>

  <script>
    let isContextOpen = true;
    let isEnvOpen = true;
    let isUsageOpen = true;
    let currentBranch = "";
    const urlParams = new URLSearchParams(window.location.search);
    const hostConvId = urlParams.get('conversationId') || urlParams.get('conv_id');
    const isNewDraft = urlParams.has('token') && !hostConvId;
    let selectedConvId = 'auto';
    let lastOptionsSignature = "";
    let lastUsageSignature = "";

    function formatK(num) {
      if (num >= 1000000) return (num / 1000000).toFixed(1) + 'M';
      if (num >= 1000) return (num / 1000).toFixed(1) + 'k';
      return String(num);
    }

    function showToast(msg) {
      const t = document.getElementById('toast');
      t.textContent = msg;
      t.classList.add('show');
      setTimeout(() => t.classList.remove('show'), 1800);
    }

    function copyBranch() {
      if (!currentBranch) return;
      navigator.clipboard.writeText(currentBranch);
      showToast('Branch copied: ' + currentBranch);
    }

    function copyCompact() {
      navigator.clipboard.writeText('/compact');
      showToast('Copied: /compact');
    }

    function onSessionSelectChange(val) {
      selectedConvId = val;
      updateAutoButtonState();
      updateData();
    }

    function resetToAuto() {
      selectedConvId = 'auto';
      const select = document.getElementById('sessionSelect');
      if (select) select.value = 'auto';
      updateAutoButtonState();
      updateData();
      showToast('Set to Auto (open session)');
    }

    function updateAutoButtonState() {
      const btn = document.getElementById('btnAutoReset');
      if (!btn) return;
      btn.textContent = 'Set Auto';
      if (selectedConvId && selectedConvId !== 'auto') {
        btn.classList.add('active-manual');
        btn.title = 'Manual session selected. Click to reset to Auto.';
      } else {
        btn.classList.remove('active-manual');
        btn.title = 'In Auto mode (following open session).';
      }
    }

    function updateSessionOptions(convs, activeId) {
      const select = document.getElementById('sessionSelect');
      if (!select) return;

      // Do NOT modify DOM if user is actively interacting with the select
      if (document.activeElement === select) {
        return;
      }

      // Avoid DOM re-renders and scroll resets if options haven't changed
      const sig = convs.map(c => c.id + ':' + c.title).join('|') + '|' + selectedConvId + '|' + isNewDraft;
      if (sig === lastOptionsSignature) {
        if (select.value !== selectedConvId) {
          select.value = selectedConvId;
        }
        return;
      }
      lastOptionsSignature = sig;

      const autoLabel = isNewDraft ? 'Auto (New Conversation)' : 'Auto (Current session)';
      let html = `<option value="auto">${autoLabel}</option>`;
      for (const c of convs) {
        const isSel = (c.id === selectedConvId) ? 'selected' : '';
        html += `<option value="${c.id}" ${isSel}>${escapeHtml(c.title)}</option>`;
      }
      select.innerHTML = html;
      select.value = selectedConvId;
    }

    function escapeHtml(str) {
      return str.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
    }

    function renderUsage(groups) {
      const container = document.getElementById('usageContent');
      if (!container || !groups || groups.length === 0) return;

      const sig = JSON.stringify(groups);
      if (sig === lastUsageSignature) return;
      lastUsageSignature = sig;

      let html = '';
      for (const g of groups) {
        html += `<div class="usage-group">`;
        html += `<div class="usage-group-title">${escapeHtml(g.name)}</div>`;
        for (const b of g.buckets) {
          const frac = Math.max(0, Math.min(1, b.fraction));
          const offset = (44 * (1 - frac)).toFixed(1);
          let ringColor = 'var(--green)';
          if (frac <= 0.2) ringColor = '#ef4444';
          else if (frac <= 0.45) ringColor = 'var(--orange)';

          html += `
            <div class="usage-row">
              <div class="usage-label">${escapeHtml(b.label)}</div>
              <div class="usage-value-container">
                <div class="usage-value-row">
                  <span class="usage-pct">${b.pct}% left</span>
                  <svg class="ring-svg" viewBox="0 0 20 20">
                    <circle cx="10" cy="10" r="7" stroke="var(--gray)" stroke-width="2.5" fill="none" />
                    <circle cx="10" cy="10" r="7" stroke="${ringColor}" stroke-width="2.5" fill="none"
                            stroke-dasharray="44" stroke-dashoffset="${offset}"
                            stroke-linecap="round" transform="rotate(-90 10 10)" />
                  </svg>
                </div>
                ${b.reset_label ? `<div class="usage-reset">${escapeHtml(b.reset_label)}</div>` : ''}
              </div>
            </div>
          `;
        }
        html += `</div>`;
      }
      container.innerHTML = html;
    }

    async function updateData() {
      try {
        let url = '/api/status';
        if (selectedConvId && selectedConvId !== 'auto') {
          url += '?conv_id=' + encodeURIComponent(selectedConvId);
        } else if (isNewDraft) {
          url += '?is_new=1';
        } else if (hostConvId) {
          url += '?conv_id=' + encodeURIComponent(hostConvId);
        }
        const res = await fetch(url, { cache: 'no-store' });
        if (!res.ok) return;
        const data = await res.json();

        if (data.context.conversations) {
          updateSessionOptions(data.context.conversations, data.context.conv_id);
        }

        document.getElementById('wsPath').textContent = data.git.workspace_path_short;
        document.getElementById('wsTooltip').textContent = data.git.full_path;
        
        currentBranch = data.git.branch;
        document.getElementById('gitBranch').textContent = currentBranch;
        document.getElementById('branchTooltip').textContent = 'Branch: ' + currentBranch + ' (click to copy)';
        
        const wtLabel = document.getElementById('wtLabel');
        if (data.git.is_worktree) {
          wtLabel.style.display = 'inline';
          if (data.git.worktree_path) {
            document.getElementById('wsTooltip').textContent = 'Worktree: ' + data.git.worktree_path;
          }
        } else {
          wtLabel.style.display = 'none';
        }

        const ctx = data.context;
        document.getElementById('pillPct').textContent = ctx.pct_round + '%';
        document.getElementById('pillFill').style.width = Math.min(100, ctx.pct) + '%';

        document.getElementById('pctMain').textContent = ctx.pct_round + '% used';
        document.getElementById('tokensSub').textContent = formatK(ctx.total) + ' / 1M';

        document.getElementById('barCached').style.width = ctx.cached_pct + '%';
        document.getElementById('barInput').style.width = ctx.input_pct + '%';
        document.getElementById('barOutput').style.width = ctx.output_pct + '%';

        document.getElementById('valCached').textContent = formatK(ctx.cached);
        document.getElementById('pctCached').textContent = ctx.cached_pct.toFixed(1) + '%';

        document.getElementById('valInput').textContent = formatK(ctx.input);
        document.getElementById('pctInput').textContent = ctx.input_pct.toFixed(1) + '%';

        document.getElementById('valOutput').textContent = formatK(ctx.output);
        document.getElementById('pctOutput').textContent = ctx.output_pct.toFixed(1) + '%';

        document.getElementById('valFree').textContent = formatK(ctx.free);
        document.getElementById('pctFree').textContent = ctx.free_pct.toFixed(1) + '%';

        if (ctx.is_new) {
          document.getElementById('bufferNote').textContent = 'New Conversation';
        } else {
          const buffer = Math.max(0, 800000 - ctx.total);
          document.getElementById('bufferNote').textContent = formatK(buffer) + ' until compact';
        }

        if (data.usage) {
          renderUsage(data.usage);
        }
      } catch (e) {}
    }

    document.getElementById('contextToggle').addEventListener('click', () => {
      isContextOpen = !isContextOpen;
      const details = document.getElementById('contextDetails');
      const chevron = document.getElementById('contextChevron');
      const card = document.getElementById('contextCard');
      details.classList.toggle('open', isContextOpen);
      chevron.classList.toggle('open', isContextOpen);
      card.style.borderColor = isContextOpen ? 'var(--card-active-border)' : 'var(--card-border)';
    });

    document.getElementById('envToggle').addEventListener('click', () => {
      isEnvOpen = !isEnvOpen;
      const details = document.getElementById('envDetails');
      const chevron = document.getElementById('envChevron');
      details.style.display = isEnvOpen ? 'block' : 'none';
      chevron.classList.toggle('open', isEnvOpen);
    });

    document.getElementById('usageToggle').addEventListener('click', () => {
      isUsageOpen = !isUsageOpen;
      const details = document.getElementById('usageDetails');
      const chevron = document.getElementById('usageChevron');
      details.classList.toggle('open', isUsageOpen);
      chevron.classList.toggle('open', isUsageOpen);
    });

    // Auto-reset when user clicks/switches between sessions in the UI
    window.addEventListener('focus', () => {
      if (selectedConvId !== 'auto') {
        resetToAuto();
      }
    });
    document.addEventListener('visibilitychange', () => {
      if (!document.hidden && selectedConvId !== 'auto') {
        resetToAuto();
      }
    });

    updateAutoButtonState();
    updateData();
    setInterval(updateData, 1500);
  </script>
</body>
</html>
"""

class RequestHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        # 1. Strict loopback IP verification
        client_ip = self.client_address[0]
        if client_ip not in ("127.0.0.1", "::1", "localhost"):
            self.send_error(403, "Forbidden: Local access only")
            return

        # 2. Strict Host header verification (Anti-DNS-Rebinding)
        host_header = self.headers.get("Host", "").split(":")[0].lower()
        if host_header not in ("127.0.0.1", "localhost"):
            self.send_error(403, "Forbidden: Invalid Host header")
            return

        # 3. Block cross-site browser requests (Anti-CSRF / Anti-Web-Scraping)
        sec_site = self.headers.get("Sec-Fetch-Site", "").lower()
        if sec_site == "cross-site":
            self.send_error(403, "Forbidden: Cross-site requests blocked")
            return

        url = urlparse(self.path)
        qs = parse_qs(url.query)
        target_conv = qs.get("conv_id", [None])[0] or qs.get("conversationId", [None])[0]
        
        if url.path == "/api/status":
            is_new = qs.get("is_new", ["0"])[0] == "1"
            ctx = get_token_info(target_conv, is_new=is_new)
            git = get_git_info(ctx.get("workspace_path"))
            usage = get_usage_quota()
            data = {"context": ctx, "git": git, "usage": usage}
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(json.dumps(data).encode("utf-8"))
        else:
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(HTML_PAGE.encode("utf-8"))

    def log_message(self, format, *args):
        pass

def main():
    port = int(os.environ.get("ANTIGRAVITY_SIDECAR_WEB_PORT", 0))
    if port == 0:
        port = 54321

    server = None
    for p in [port, 54322, 54323, 0]:
        try:
            server = HTTPServer(("127.0.0.1", p), RequestHandler)
            break
        except Exception:
            continue

    if not server:
        sys.exit(1)

    print(f"Context Sidecar running on http://127.0.0.1:{server.server_port}")
    sys.stdout.flush()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()

if __name__ == "__main__":
    main()

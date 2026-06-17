#!/usr/bin/env python3
"""
肿瘤患者生活质量调查 — 数据收集与管理服务器

支持两种模式:
  局域网模式: python3 survey_app.py
  云端模式:   BASE_URL=https://xxx.onrender.com python3 survey_app.py

用法:
  方式1: 双击「启动问卷.command」（局域网）
  方式2: 终端运行 python3 survey_app.py
  方式3: Docker 部署到云端（见云端部署指南.md）
"""

import sqlite3
import socket
import qrcode
import os
import io
import csv
import json
import sys
from datetime import datetime

from flask import Flask, request, jsonify, g, Response, send_from_directory

# ── Configuration ──────────────────────────────────────────
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.environ.get("DATA_DIR", SCRIPT_DIR)
DB_PATH = os.path.join(DATA_DIR, "survey_data.db")
HTML_FILE = "QLQ-Combined-Survey.html"
PORT = int(os.environ.get("PORT", 8080))
BASE_URL = os.environ.get("BASE_URL", "")

app = Flask(__name__, static_folder=SCRIPT_DIR, static_url_path="")

# ── Database ───────────────────────────────────────────────

def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH, detect_types=sqlite3.PARSE_DECLTYPES)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA journal_mode=WAL")
        g.db.execute("PRAGMA foreign_keys=ON")
    return g.db

@app.teardown_appcontext
def close_db(exception):
    db = g.pop("db", None)
    if db is not None:
        db.close()

def init_db():
    os.makedirs(DATA_DIR, exist_ok=True)
    db = sqlite3.connect(DB_PATH)
    db.executescript("""
        CREATE TABLE IF NOT EXISTS submissions (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            patient_id  TEXT    NOT NULL DEFAULT '',
            initials    TEXT    NOT NULL DEFAULT '',
            birthdate   TEXT    DEFAULT '',
            filldate    TEXT    NOT NULL DEFAULT '',
            has_stoma   TEXT    DEFAULT '',
            gender      TEXT    DEFAULT '',
            ip_address  TEXT    DEFAULT '',
            created_at  TEXT    NOT NULL DEFAULT (datetime('now','localtime'))
        );
        CREATE TABLE IF NOT EXISTS responses (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            submission_id   INTEGER NOT NULL REFERENCES submissions(id) ON DELETE CASCADE,
            scale           TEXT    NOT NULL,
            question_number INTEGER NOT NULL,
            question_text   TEXT    DEFAULT '',
            answer_value    INTEGER,
            UNIQUE(submission_id, scale, question_number)
        );
        CREATE TABLE IF NOT EXISTS scores (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            submission_id   INTEGER NOT NULL REFERENCES submissions(id) ON DELETE CASCADE,
            scale           TEXT    NOT NULL,
            score_name      TEXT    NOT NULL,
            score_value     REAL,
            UNIQUE(submission_id, scale, score_name)
        );
        CREATE INDEX IF NOT EXISTS idx_resp_sub ON responses(submission_id);
        CREATE INDEX IF NOT EXISTS idx_scores_sub ON scores(submission_id);
        CREATE INDEX IF NOT EXISTS idx_sub_date ON submissions(created_at);
    """)
    # Migration
    try:
        db.execute("ALTER TABLE submissions ADD COLUMN patient_id TEXT NOT NULL DEFAULT ''")
    except sqlite3.OperationalError:
        pass
    db.commit()
    db.close()

# ── Scoring Engine ─────────────────────────────────────────

def score_c30(responses):
    scores = {}
    vals = [responses.get(i) for i in range(1, 6) if responses.get(i) is not None]
    if vals:
        raw = sum(vals) / len(vals)
        scores["physical"] = round((1 - (raw - 1) / 3) * 100)
    vals = [responses.get(i) for i in range(6, 8) if responses.get(i) is not None]
    if vals:
        raw = sum(vals) / len(vals)
        scores["role"] = round((1 - (raw - 1) / 3) * 100)
    vals = [responses.get(i) for i in (29, 30) if responses.get(i) is not None]
    if vals:
        raw = sum(vals) / len(vals)
        scores["global_qol"] = round(((raw - 1) / 6) * 100)
    vals = [responses.get(i) for i in range(8, 29) if responses.get(i) is not None]
    if vals:
        raw = sum(vals) / len(vals)
        scores["symptoms"] = round(((raw - 1) / 3) * 100)
    return scores

def score_anl27(responses):
    vals = [v for k, v in responses.items() if v is not None]
    if vals:
        raw = sum(vals) / len(vals)
        return {"total": round(((raw - 1) / 3) * 100)}
    return {}

def score_wexner(responses):
    vals = [responses.get(i, 0) for i in range(1, 6)]
    return {"total": sum(vals)}

# ── Routes ─────────────────────────────────────────────────

@app.route("/health")
def health():
    return jsonify({"status": "ok"})

@app.route("/")
def serve_survey():
    return send_from_directory(SCRIPT_DIR, HTML_FILE)

@app.route("/api/submit", methods=["POST"])
def submit_survey():
    data = request.get_json(silent=True)
    if not data:
        return jsonify({"success": False, "error": "无效的 JSON 数据"}), 400

    patient = data.get("patient", {})
    raw_responses = data.get("responses", [])
    if not raw_responses:
        return jsonify({"success": False, "error": "问卷数据为空"}), 400

    patient_id = (patient.get("patient_id") or "").strip()
    initials = (patient.get("initials") or "").strip()
    birthdate = patient.get("birthdate", "")
    filldate = patient.get("filldate", "")
    has_stoma = patient.get("has_stoma", "")
    gender = patient.get("gender", "")
    ip = request.remote_addr or ""

    db = get_db()
    cur = db.execute("""
        INSERT INTO submissions (patient_id, initials, birthdate, filldate, has_stoma, gender, ip_address)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    """, (patient_id, initials, birthdate, filldate, has_stoma, gender, ip))
    submission_id = cur.lastrowid

    c30_resp, anl27_resp, wexner_resp = {}, {}, {}
    for r in raw_responses:
        scale = r.get("scale", "")
        qnum = r.get("number", 0)
        qtext = r.get("text", "")
        value = r.get("value")
        db.execute("""
            INSERT OR REPLACE INTO responses (submission_id, scale, question_number, question_text, answer_value)
            VALUES (?, ?, ?, ?, ?)
        """, (submission_id, scale, qnum, qtext, value))
        if scale == "c30": c30_resp[qnum] = value
        elif scale == "anl27": anl27_resp[qnum] = value
        elif scale == "wexner": wexner_resp[qnum] = value

    c30_scores = score_c30(c30_resp)
    for name, val in c30_scores.items():
        db.execute("INSERT INTO scores (submission_id, scale, score_name, score_value) VALUES (?, 'c30', ?, ?)",
                   (submission_id, name, val))
    anl27_scores = score_anl27(anl27_resp)
    for name, val in anl27_scores.items():
        db.execute("INSERT INTO scores (submission_id, scale, score_name, score_value) VALUES (?, 'anl27', ?, ?)",
                   (submission_id, name, val))
    wexner_scores = score_wexner(wexner_resp)
    for name, val in wexner_scores.items():
        db.execute("INSERT INTO scores (submission_id, scale, score_name, score_value) VALUES (?, 'wexner', ?, ?)",
                   (submission_id, name, val))
    db.commit()

    print(f"[✓] #{submission_id} — 病历号: {patient_id} — {initials} — {filldate}")
    return jsonify({"success": True, "id": submission_id,
                     "scores": {"c30": c30_scores, "anl27": anl27_scores, "wexner": wexner_scores}})

@app.route("/api/admin/stats")
def admin_stats():
    db = get_db()
    total = db.execute("SELECT COUNT(*) FROM submissions").fetchone()[0]
    if total == 0:
        return jsonify({"total_submissions": 0, "message": "暂无数据"})
    avgs = {}
    for scale, names in [("c30", ["physical","role","global_qol","symptoms"]), ("anl27", ["total"]), ("wexner", ["total"])]:
        for name in names:
            row = db.execute("SELECT ROUND(AVG(score_value),1) FROM scores WHERE scale=? AND score_name=?", (scale, name)).fetchone()
            avgs[f"{scale}_{name}"] = round(row[0], 1) if row and row[0] else None
    trends = db.execute("""
        SELECT s.filldate, ROUND(AVG(sc.score_value),1) as avg_score, COUNT(DISTINCT s.id) as cnt
        FROM scores sc JOIN submissions s ON sc.submission_id=s.id
        WHERE sc.scale='c30' AND sc.score_name='global_qol'
        GROUP BY s.filldate ORDER BY s.filldate DESC LIMIT 30
    """).fetchall()
    wex_dist = {"normal": 0, "mild": 0, "moderate": 0, "severe": 0}
    for r in db.execute("SELECT score_value FROM scores WHERE scale='wexner' AND score_name='total'").fetchall():
        v = r["score_value"] or 0
        if v == 0: wex_dist["normal"] += 1
        elif v <= 5: wex_dist["mild"] += 1
        elif v <= 10: wex_dist["moderate"] += 1
        else: wex_dist["severe"] += 1
    date_range = db.execute("SELECT MIN(filldate) as first, MAX(filldate) as last FROM submissions").fetchone()
    return jsonify({
        "total_submissions": total,
        "date_range": {"first": date_range["first"], "last": date_range["last"]},
        "averages": avgs,
        "trends": [{"date": r["filldate"], "avg": r["avg_score"], "count": r["cnt"]} for r in trends],
        "distributions": {"wexner_severity": wex_dist},
    })

@app.route("/api/admin/responses")
def admin_responses_list():
    db = get_db()
    page = request.args.get("page", 1, type=int)
    per_page = request.args.get("per_page", 20, type=int)
    offset = (page - 1) * per_page
    total = db.execute("SELECT COUNT(*) FROM submissions").fetchone()[0]
    rows = db.execute("""
        SELECT s.id, s.patient_id, s.initials, s.filldate, s.has_stoma, s.gender, s.created_at,
            (SELECT sc.score_value FROM scores sc WHERE sc.submission_id=s.id AND sc.scale='c30' AND sc.score_name='global_qol') as c30_qol,
            (SELECT sc.score_value FROM scores sc WHERE sc.submission_id=s.id AND sc.scale='anl27' AND sc.score_name='total') as anl27_total,
            (SELECT sc.score_value FROM scores sc WHERE sc.submission_id=s.id AND sc.scale='wexner' AND sc.score_name='total') as wexner_total
        FROM submissions s ORDER BY s.id DESC LIMIT ? OFFSET ?
    """, (per_page, offset)).fetchall()
    items = [{"id": r["id"], "patient_id": r["patient_id"], "initials": r["initials"], "filldate": r["filldate"],
              "has_stoma": r["has_stoma"], "gender": r["gender"], "created_at": r["created_at"],
              "c30_global_qol": r["c30_qol"], "anl27_total": r["anl27_total"], "wexner_total": r["wexner_total"]} for r in rows]
    return jsonify({"total": total, "page": page, "per_page": per_page, "items": items})

@app.route("/api/admin/response/<int:sub_id>")
def admin_response_detail(sub_id):
    db = get_db()
    sub = db.execute("SELECT * FROM submissions WHERE id=?", (sub_id,)).fetchone()
    if not sub:
        return jsonify({"error": "未找到"}), 404
    responses = db.execute("SELECT scale, question_number, question_text, answer_value FROM responses WHERE submission_id=? ORDER BY scale, question_number", (sub_id,)).fetchall()
    scores = db.execute("SELECT scale, score_name, score_value FROM scores WHERE submission_id=?", (sub_id,)).fetchall()
    return jsonify({
        "submission": {"id": sub["id"], "patient_id": sub["patient_id"], "initials": sub["initials"],
                        "birthdate": sub["birthdate"], "filldate": sub["filldate"],
                        "has_stoma": sub["has_stoma"], "gender": sub["gender"], "created_at": sub["created_at"]},
        "responses": [{"scale": r["scale"], "number": r["question_number"], "text": r["question_text"], "value": r["answer_value"]} for r in responses],
        "scores": [{"scale": r["scale"], "name": r["score_name"], "value": r["score_value"]} for r in scores],
    })

@app.route("/api/admin/export/csv")
def export_csv():
    db = get_db()
    output = io.StringIO()
    writer = csv.writer(output)
    header = ["ID", "病历号", "姓名缩写", "出生日期", "填写日期", "造口袋", "性别",
              "C30-躯体功能", "C30-角色功能", "C30-整体健康", "C30-症状负担", "ANL27-症状总分", "Wexner-总分"]
    all_qnums = ([(i, f"Q{i}") for i in range(1, 31)] + [(i, f"Q{i}") for i in range(31, 58)] + [(i, f"Wexner-Q{i}") for i in range(1, 6)])
    for _, ql in all_qnums:
        header.append(f"{ql}-分值"); header.append(f"{ql}-题干")
    writer.writerow(header)
    subs = db.execute("SELECT * FROM submissions ORDER BY id").fetchall()
    for sub in subs:
        scores_map = {}
        for sc in db.execute("SELECT * FROM scores WHERE submission_id=?", (sub["id"],)):
            scores_map[f"{sc['scale']}_{sc['score_name']}"] = sc["score_value"]
        row = [sub["id"], sub["patient_id"], sub["initials"], sub["birthdate"], sub["filldate"],
               sub["has_stoma"], sub["gender"],
               scores_map.get("c30_physical",""), scores_map.get("c30_role",""),
               scores_map.get("c30_global_qol",""), scores_map.get("c30_symptoms",""),
               scores_map.get("anl27_total",""), scores_map.get("wexner_total","")]
        resp_map = {}
        for r in db.execute("SELECT * FROM responses WHERE submission_id=?", (sub["id"],)):
            resp_map[f"{r['scale']}_{r['question_number']}"] = (r["answer_value"], r["question_text"])
        for qnum, _ in all_qnums:
            key = f"c30_{qnum}" if qnum <= 30 else (f"anl27_{qnum}" if qnum <= 57 else f"wexner_{qnum - 56}")
            if key in resp_map:
                row.append(resp_map[key][0]); row.append(resp_map[key][1])
            else:
                row.append(""); row.append("")
        writer.writerow(row)
    output.seek(0)
    return Response(output.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": f"attachment; filename=survey_export_{datetime.now().strftime('%Y%m%d_%H%M')}.csv"})

# ── Admin Dashboard HTML ────────────────────────────────────

ADMIN_HTML = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>管理后台 — 量表数据</title>
<style>
  :root { --primary:#2563eb; --primary-light:#eff6ff; --border:#e2e8f0; --text:#1e293b; --text-light:#64748b; --bg:#f8fafc; --white:#fff; --success:#10b981; --warning:#f59e0b; --danger:#ef4444; }
  * { box-sizing:border-box; margin:0; padding:0; }
  body { font-family:-apple-system,BlinkMacSystemFont,"PingFang SC","Microsoft YaHei",sans-serif; background:var(--bg); color:var(--text); line-height:1.6; }
  .container { max-width:960px; margin:0 auto; padding:16px; }
  .header { background:linear-gradient(135deg,#1e40af,#3b82f6); color:white; padding:20px 16px; border-radius:0 0 16px 16px; margin-bottom:16px; text-align:center; }
  .header h1 { font-size:20px; } .header p { font-size:13px; opacity:0.85; margin-top:4px; }
  .tabs { display:flex; gap:4px; margin-bottom:16px; background:var(--white); border-radius:12px; padding:4px; box-shadow:0 1px 3px rgba(0,0,0,.06); }
  .tab { flex:1; text-align:center; padding:10px; border-radius:10px; font-size:14px; font-weight:600; cursor:pointer; color:var(--text-light); transition:all .2s; }
  .tab.active { background:var(--primary); color:white; }
  .card { background:var(--white); border-radius:12px; padding:16px; margin-bottom:12px; box-shadow:0 1px 3px rgba(0,0,0,.05); }
  .card h3 { font-size:15px; color:#1e40af; margin-bottom:8px; }
  .stat-row { display:flex; justify-content:space-between; padding:6px 0; border-bottom:1px solid var(--border); font-size:14px; }
  .stat-val { font-weight:700; color:var(--primary); }
  .bar-wrap { background:var(--border); border-radius:6px; height:24px; overflow:hidden; margin:4px 0; }
  .bar-fill { height:100%; border-radius:6px; display:flex; align-items:center; padding-left:8px; font-size:11px; font-weight:600; color:white; }
  table { width:100%; border-collapse:collapse; font-size:13px; }
  th,td { padding:8px 10px; text-align:left; border-bottom:1px solid var(--border); }
  th { background:var(--primary-light); color:var(--primary); font-weight:600; }
  tr:hover td { background:#f1f5f9; }
  .clickable { cursor:pointer; color:var(--primary); font-weight:600; }
  .detail-panel { display:none; background:#f8fafc; border-radius:8px; padding:12px; margin:8px 0; }
  .btn { display:inline-block; padding:10px 20px; border:none; border-radius:10px; font-size:14px; font-weight:600; cursor:pointer; }
  .btn-primary { background:var(--primary); color:white; } .btn-success { background:var(--success); color:white; }
  .badge { display:inline-block; padding:2px 8px; border-radius:6px; font-size:11px; font-weight:600; }
  .badge-green { background:#dcfce7; color:#166534; } .badge-yellow { background:#fef3c7; color:#92400e; } .badge-red { background:#fee2e2; color:#991b1b; }
  .pagination { display:flex; gap:8px; align-items:center; justify-content:center; margin-top:12px; }
  .pagination button { padding:6px 16px; border:1px solid var(--border); border-radius:8px; background:white; cursor:pointer; font-size:13px; }
  .pagination button:disabled { opacity:.4; cursor:default; }
  .loading,.empty { text-align:center; padding:40px; color:var(--text-light); }
</style>
</head>
<body>
<div class="container">
  <div class="header"><h1>📊 量表数据管理面板</h1><p id="headerInfo">加载中...</p></div>
  <div class="tabs">
    <div class="tab active" onclick="switchTab('overview')">📈 概览统计</div>
    <div class="tab" onclick="switchTab('browse')">📋 浏览记录</div>
    <div class="tab" onclick="switchTab('export')">💾 导出数据</div>
  </div>
  <div id="tab-overview"><div class="card" id="overviewContent"><div class="loading">加载中...</div></div></div>
  <div id="tab-browse" style="display:none">
    <div class="card" id="browseContent"><div class="loading">加载中...</div></div>
    <div class="pagination" id="pagination"></div>
  </div>
  <div id="tab-export" style="display:none">
    <div class="card">
      <h3>💾 导出全部数据为 CSV</h3>
      <p style="font-size:14px;color:var(--text-light);margin:8px 0">CSV 可用 Excel、SPSS、R 等软件打开。<br>每行代表一个患者，含所有题目原始答案和评分。</p>
      <button class="btn btn-success" onclick="window.location.href='/api/admin/export/csv'">⬇ 下载 CSV</button>
    </div>
  </div>
</div>
<script>
let currentTab='overview',currentPage=1,detailCache={};

function switchTab(tab) {
  currentTab=tab;
  document.querySelectorAll('.tab').forEach((t,i)=>{t.classList.toggle('active',['overview','browse','export'][i]===tab);});
  document.getElementById('tab-overview').style.display=tab==='overview'?'block':'none';
  document.getElementById('tab-browse').style.display=tab==='browse'?'block':'none';
  document.getElementById('tab-export').style.display=tab==='export'?'block':'none';
  if(tab==='overview') loadOverview();
  if(tab==='browse') loadBrowse(1);
}

async function loadOverview() {
  const el=document.getElementById('overviewContent');
  el.innerHTML='<div class="loading">加载中...</div>';
  try {
    const resp=await fetch('/api/admin/stats'),data=await resp.json();
    if(data.total_submissions===0){el.innerHTML='<div class="empty">📭 暂无数据</div>';document.getElementById('headerInfo').textContent='暂无数据';return;}
    document.getElementById('headerInfo').textContent=`共 ${data.total_submissions} 份提交 | ${data.date_range?.first||'—'} ~ ${data.date_range?.last||'—'}`;
    const avg=data.averages||{};
    const labels={c30_physical:['C30-躯体功能','越高越好'],c30_role:['C30-角色功能','越高越好'],c30_global_qol:['C30-整体健康/QoL','越高越好'],c30_symptoms:['C30-症状负担','越低越好'],anl27_total:['ANL27-肛门癌症状','越低越好'],wexner_total:['Wexner-失禁总分(0-20)','越低越好']};
    let html='<h3>📊 各量表平均分</h3>';
    for(const[key,label]of Object.entries(labels)){
      const v=avg[key];
      if(v!==null&&v!==undefined){
        const pct=key==='wexner_total'?Math.round(v/20*100):Math.round(v);
        const color=pct>60?'#ef4444':pct>30?'#f59e0b':'#10b981';
        html+=`<div style="margin-bottom:8px"><div style="display:flex;justify-content:space-between;font-size:13px;margin-bottom:2px"><span>${label[0]} <small style="color:#64748b">(${label[1]})</small></span><span style="font-weight:700">${v}</span></div><div class="bar-wrap"><div class="bar-fill" style="width:${Math.min(pct,100)}%;background:${color}">${v}</div></div></div>`;
      }
    }
    const dist=data.distributions?.wexner_severity||{};
    html+='<h3 style="margin-top:16px">📊 Wexner 失禁严重度分布</h3>';
    for(const[k,label]of Object.entries({normal:'正常(0)',mild:'轻度(1-5)',moderate:'中度(6-10)',severe:'重度(11-20)'})){
      const cnt=dist[k]||0,pct=data.total_submissions>0?Math.round(cnt/data.total_submissions*100):0;
      html+=`<div style="margin-bottom:6px"><div style="display:flex;justify-content:space-between;font-size:13px"><span>${label}</span><span>${cnt} 人 (${pct}%)</span></div><div class="bar-wrap"><div class="bar-fill" style="width:${pct}%;background:#6366f1">${pct}%</div></div></div>`;
    }
    if(data.trends?.length>0){
      html+='<h3 style="margin-top:16px">📈 C30整体健康趋势</h3><table><tr><th>日期</th><th>平均分</th><th>人数</th></tr>';
      data.trends.forEach(t=>{html+=`<tr><td>${t.date}</td><td><b>${t.avg}</b></td><td>${t.count}</td></tr>`;});
      html+='</table>';
    }
    el.innerHTML=html;
  }catch(e){el.innerHTML='<div class="empty">⚠️ 无法加载数据</div>';}
}

async function loadBrowse(page) {
  currentPage=page;
  const el=document.getElementById('browseContent');
  el.innerHTML='<div class="loading">加载中...</div>';
  try {
    const resp=await fetch(`/api/admin/responses?page=${page}&per_page=15`),data=await resp.json();
    if(data.items.length===0){el.innerHTML='<div class="empty">📭 暂无记录</div>';return;}
    let html='<div style="overflow-x:auto"><table><tr><th>ID</th><th>病历号</th><th>姓名</th><th>日期</th><th>造口袋</th><th>性别</th><th>C30-QoL</th><th>ANL27</th><th>Wexner</th><th>详情</th></tr>';
    data.items.forEach(item=>{
      const wexBadge=item.wexner_total===0?'badge-green':item.wexner_total<=5?'badge-yellow':'badge-red';
      html+=`<tr><td>${item.id}</td><td><b>${item.patient_id||'—'}</b></td><td>${item.initials||'—'}</td><td>${item.filldate}</td><td>${item.has_stoma||'—'}</td><td>${item.gender||'—'}</td><td><b>${item.c30_global_qol??'—'}</b></td><td>${item.anl27_total??'—'}</td><td><span class="badge ${wexBadge}">${item.wexner_total??'—'}</span></td><td><span class="clickable" onclick="toggleDetail(${item.id},this)">展开</span></td></tr>`;
      html+=`<tr id="detail-${item.id}" style="display:none"><td colspan="10"><div class="detail-panel" id="detail-content-${item.id}">加载中...</div></td></tr>`;
    });
    html+='</table></div>';
    const totalPages=Math.ceil(data.total/data.per_page);
    html+=`<div class="pagination"><button ${page<=1?'disabled':''} onclick="loadBrowse(${page-1})">‹ 上一页</button><span style="font-size:13px">第 ${page}/${totalPages} 页 (共 ${data.total} 条)</span><button ${page>=totalPages?'disabled':''} onclick="loadBrowse(${page+1})">下一页 ›</button></div>`;
    el.innerHTML=html;
  }catch(e){el.innerHTML='<div class="empty">⚠️ 无法加载</div>';}
}

async function toggleDetail(id,el){
  const row=document.getElementById('detail-'+id),content=document.getElementById('detail-content-'+id);
  if(row.style.display==='table-row'){row.style.display='none';el.textContent='展开';return;}
  row.style.display='table-row';el.textContent='收起';
  if(detailCache[id]){content.innerHTML=detailCache[id];return;}
  try {
    const resp=await fetch('/api/admin/response/'+id),data=await resp.json();
    let h=`<b>病历号: ${data.submission.patient_id||'—'}</b> | 姓名: ${data.submission.initials||'—'} | 生日: ${data.submission.birthdate||'—'} | 填写: ${data.submission.filldate} | 造口袋: ${data.submission.has_stoma||'—'} | 性别: ${data.submission.gender||'—'}`;
    h+='<br><b>评分: </b>';
    data.scores.forEach(s=>{h+=`<span style="margin-right:12px">${s.scale}-${s.name}: <b>${s.value}</b></span>`;});
    h+='<hr style="margin:8px 0">';
    for(const[sc,label]of Object.entries({c30:'QLQ-C30',anl27:'QLQ-ANL27',wexner:'Wexner'})){
      const items=data.responses.filter(r=>r.scale===sc);
      if(!items.length) continue;
      h+=`<b>${label}:</b><br>`;
      items.forEach(r=>{h+=`<span style="display:inline-block;margin:2px 6px;font-size:12px">Q${r.number}: <b>${r.value??'—'}</b></span>`;});
      h+='<br>';
    }
    detailCache[id]=h;content.innerHTML=h;
  }catch(e){content.innerHTML='加载失败';}
}
loadOverview();
</script>
</body>
</html>"""

@app.route("/admin")
def serve_admin():
    return ADMIN_HTML

# ── QR Code ────────────────────────────────────────────────

def get_local_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(1)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"

def generate_qr_png(url, filepath):
    qr = qrcode.QRCode(version=1, error_correction=qrcode.constants.ERROR_CORRECT_M, box_size=10, border=4)
    qr.add_data(url)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")
    img.save(filepath)

def print_qr_terminal(url):
    qr = qrcode.QRCode(version=1, error_correction=qrcode.constants.ERROR_CORRECT_L, box_size=1, border=2)
    qr.add_data(url)
    qr.make(fit=True)
    qr.print_ascii()

# ── Main ───────────────────────────────────────────────────

def main():
    os.chdir(SCRIPT_DIR)
    os.makedirs(DATA_DIR, exist_ok=True)
    init_db()

    # Determine public URL
    if BASE_URL:
        url = BASE_URL
    else:
        ip = get_local_ip()
        url = f"http://{ip}:{PORT}/"

    admin_url = f"{url.rstrip('/')}/admin"
    qr_path = os.path.join(DATA_DIR, "survey_qrcode.png")
    generate_qr_png(url, qr_path)

    is_cloud = bool(BASE_URL)

    print()
    print("╔" + "═" * 54 + "╗")
    print("║" + ("   ☁️  肿瘤患者生活质量调查 — 云端版" if is_cloud else "   📋  肿瘤患者生活质量调查 — 数据收集服务器").center(48) + "║")
    print("╠" + "═" * 54 + "╣")
    print("║" + " " * 54 + "║")
    print("║" + "  🔲 请患者扫描下方二维码填写问卷：".center(48) + "║")
    print("║" + " " * 54 + "║")
    print("╚" + "═" * 54 + "╝")
    print()
    print_qr_terminal(url)
    print()
    print("═" * 56)
    print(f"  📱 患者问卷:  {url}")
    print(f"  📊 管理后台:  {admin_url}")
    print(f"  🖨️  二维码图片: {qr_path}")
    print("─" * 56)
    print(f"  💾 数据库:      {DB_PATH}")
    print("─" * 56)
    print()

    if is_cloud:
        print("  ☁️  云端部署模式 — 患者随时随地可扫码填写")
    else:
        print("  🟢 局域网模式：")
        print("     1. 确保手机与电脑连接同一 WiFi")
        print("     2. 患者用相机扫描上方二维码 → 填写问卷")
        print("     3. 医生打开管理后台查看统计")
        print()
        print("  💡 如需患者随时随地方便访问 → 请参考「云端部署指南.md」")
    print()
    print("  🛑 按 Ctrl+C 停止服务器")
    print("═" * 56)
    print()
    sys.stdout.flush()

    app.run(host="0.0.0.0", port=PORT, debug=False)

if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""本地血压与睡眠记录工具。仅使用 Python 标准库。"""

from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import csv
import io
import json
import os
import re
import sqlite3
import socket
from pathlib import Path
from urllib.parse import parse_qs, urlparse

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = str(BASE_DIR / "health_records.db")
HOST, PORT = "0.0.0.0", int(os.environ.get("HEALTH_PORT", "8765"))


def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db():
    with db() as conn:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS blood_pressure (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          measured_at TEXT NOT NULL,
          systolic INTEGER NOT NULL,
          diastolic INTEGER NOT NULL,
          pulse INTEGER,
          note TEXT DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS sleep (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          sleep_date TEXT NOT NULL,
          bedtime TEXT,
          wake_time TEXT,
          duration REAL,
          quality INTEGER,
          note TEXT DEFAULT ''
        );
        """)
        # 兼容之前版本创建的 NOT NULL 睡眠表，让睡眠细项可以留空。
        columns = {row[1]: row[3] for row in conn.execute("PRAGMA table_info(sleep)")}
        if columns.get("bedtime") == 1 or columns.get("wake_time") == 1 or columns.get("duration") == 1 or columns.get("quality") == 1:
            conn.executescript("""
            ALTER TABLE sleep RENAME TO sleep_legacy;
            CREATE TABLE sleep (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              sleep_date TEXT NOT NULL,
              bedtime TEXT,
              wake_time TEXT,
              duration REAL,
              quality INTEGER,
              note TEXT DEFAULT ''
            );
            INSERT INTO sleep (id,sleep_date,bedtime,wake_time,duration,quality,note)
              SELECT id,sleep_date,bedtime,wake_time,duration,quality,note FROM sleep_legacy;
            DROP TABLE sleep_legacy;
            """)
        column_names = {row[1] for row in conn.execute("PRAGMA table_info(sleep)")}
        if "blood_pressure_id" not in column_names:
            conn.execute("ALTER TABLE sleep ADD COLUMN blood_pressure_id INTEGER")


def rows(table, limit=50):
    with db() as conn:
        if table == "blood_pressure":
            return [dict(r) for r in conn.execute(
                "SELECT * FROM blood_pressure ORDER BY measured_at DESC, id DESC LIMIT ?", (limit,))]
        return [dict(r) for r in conn.execute(
            "SELECT * FROM sleep ORDER BY sleep_date DESC, id DESC LIMIT ?", (limit,))]


def stats():
    with db() as conn:
        bp = conn.execute("SELECT COUNT(*) n, ROUND(AVG(systolic),1) sys, ROUND(AVG(diastolic),1) dia, ROUND(AVG(pulse),1) pulse FROM blood_pressure").fetchone()
        sl = conn.execute("SELECT COUNT(*) n, ROUND(AVG(duration),1) duration, ROUND(AVG(quality),1) quality FROM sleep").fetchone()
        return {"bp": dict(bp), "sleep": dict(sl)}


def json_bytes(data):
    return json.dumps(data, ensure_ascii=False).encode("utf-8")


def csv_bytes(table):
    output = io.StringIO(newline="")
    writer = csv.writer(output)
    if table == "health":
        writer.writerow(["血压编号", "测量时间", "收缩压(mmHg)", "舒张压(mmHg)", "心率(次/分钟)", "血压备注", "睡眠日期", "睡眠时长(小时)", "入睡时间", "起床时间", "睡眠质量(1-5)", "睡眠备注"])
        sleep_by_bp = {item.get("blood_pressure_id"): item for item in rows("sleep", 100000) if item.get("blood_pressure_id")}
        used_sleep_ids = set()
        for item in rows("blood_pressure", 100000):
            sleep = sleep_by_bp.get(item["id"], {})
            if sleep:
                used_sleep_ids.add(sleep["id"])
            writer.writerow([item["id"], item["measured_at"], item["systolic"], item["diastolic"], item["pulse"] or "", item["note"], sleep.get("sleep_date", ""), sleep.get("duration") or "", sleep.get("bedtime") or "", sleep.get("wake_time") or "", sleep.get("quality") or "", sleep.get("note", "")])
        for sleep in rows("sleep", 100000):
            if sleep["id"] not in used_sleep_ids:
                writer.writerow(["", "", "", "", "", "", sleep["sleep_date"], sleep["duration"] or "", sleep["bedtime"] or "", sleep["wake_time"] or "", sleep["quality"] or "", sleep["note"]])
        return ("\ufeff" + output.getvalue()).encode("utf-8")
    if table == "blood_pressure":
        writer.writerow(["编号", "测量时间", "收缩压(mmHg)", "舒张压(mmHg)", "心率(次/分钟)", "备注"])
        data = rows(table, 100000)
        for item in data:
            writer.writerow([item["id"], item["measured_at"], item["systolic"], item["diastolic"], item["pulse"] or "", item["note"]])
    else:
        writer.writerow(["编号", "日期", "入睡时间", "起床时间", "睡眠时长(小时)", "睡眠质量(1-5)", "备注"])
        data = rows(table, 100000)
        for item in data:
            writer.writerow([item["id"], item["sleep_date"], item["bedtime"], item["wake_time"], item["duration"], item["quality"], item["note"]])
    return ("\ufeff" + output.getvalue()).encode("utf-8")


class Handler(BaseHTTPRequestHandler):
    def send_json(self, data, status=200):
        body = json_bytes(data)
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/sw.js":
            body = SW.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/javascript; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if path == "/manifest.webmanifest":
            body = json_bytes({
                "name": "健康记录", "short_name": "健康记录", "start_url": "/",
                "display": "standalone", "background_color": "#f3f8f7", "theme_color": "#197b78",
                "description": "记录血压与睡眠的本地健康工具"
            })
            self.send_response(200)
            self.send_header("Content-Type", "application/manifest+json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if path == "/api/export":
            export_type = parse_qs(urlparse(self.path).query).get("type", [""])[0]
            if export_type not in ("health", "blood_pressure", "sleep"):
                self.send_json({"error": "不支持的导出类型"}, 400)
                return
            body = csv_bytes(export_type)
            filename = "health-records.csv" if export_type == "health" else "blood-pressure-records.csv" if export_type == "blood_pressure" else "sleep-records.csv"
            self.send_response(200)
            self.send_header("Content-Type", "text/csv; charset=utf-8")
            self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if path == "/api/data":
            self.send_json({"blood_pressure": rows("blood_pressure"), "sleep": rows("sleep"), "stats": stats()})
            return
        if path == "/":
            body = PAGE.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_error(404)

    def do_POST(self):
        path = urlparse(self.path).path
        length = int(self.headers.get("Content-Length", 0))
        try:
            payload = json.loads(self.rfile.read(length))
            with db() as conn:
                if path == "/api/blood-pressure":
                    cursor = conn.execute("INSERT INTO blood_pressure (measured_at,systolic,diastolic,pulse,note) VALUES (?,?,?,?,?)", (
                        payload["measured_at"], int(payload["systolic"]), int(payload["diastolic"]),
                        int(payload["pulse"]) if payload.get("pulse") else None, payload.get("note", "").strip()))
                elif path == "/api/sleep":
                    cursor = conn.execute("INSERT INTO sleep (sleep_date,bedtime,wake_time,duration,quality,note,blood_pressure_id) VALUES (?,?,?,?,?,?,?)", (
                        payload["sleep_date"], payload.get("bedtime") or None, payload.get("wake_time") or None,
                        float(payload["duration"]) if payload.get("duration") else None,
                        int(payload["quality"]) if payload.get("quality") else None, payload.get("note", "").strip(), payload.get("blood_pressure_id")))
                else:
                    self.send_error(404); return
            self.send_json({"ok": True, "id": cursor.lastrowid}, 201)
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            self.send_json({"error": f"数据格式不正确：{exc}"}, 400)

    def do_DELETE(self):
        parsed = urlparse(self.path)
        path, query = parsed.path, parse_qs(parsed.query)
        table = "blood_pressure" if path == "/api/blood-pressure" else "sleep" if path == "/api/sleep" else None
        try:
            record_id = int(query["id"][0])
            if not table: raise ValueError("unknown table")
            with db() as conn:
                if table == "blood_pressure":
                    conn.execute("DELETE FROM sleep WHERE blood_pressure_id = ?", (record_id,))
                conn.execute(f"DELETE FROM {table} WHERE id = ?", (record_id,))
            self.send_json({"ok": True})
        except (KeyError, ValueError):
            self.send_json({"error": "缺少有效的记录编号"}, 400)

    def log_message(self, *_):
        pass


PAGE = r'''<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>健康记录</title><meta name="theme-color" content="#197b78"><meta name="apple-mobile-web-app-capable" content="yes"><meta name="apple-mobile-web-app-status-bar-style" content="default"><link rel="manifest" href="/manifest.webmanifest"><style>
:root{--ink:#19333a;--muted:#71858a;--line:#e4eeee;--bg:#f3f8f7;--card:#fff;--teal:#197b78;--coral:#e17c62;--shadow:0 10px 30px #214b4512}
*{box-sizing:border-box}body{margin:0;background:var(--bg);font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC",sans-serif;color:var(--ink)}
.shell{max-width:1120px;margin:auto;padding:34px 22px 60px}.top{display:flex;justify-content:space-between;align-items:flex-end;gap:20px;margin-bottom:28px}.eyebrow{color:var(--teal);font-size:12px;letter-spacing:2px;font-weight:800}.title{font-size:34px;margin:7px 0 0;letter-spacing:-1px}.sub{color:var(--muted);margin:8px 0 0}.date{color:var(--muted);font-size:14px}.grid{display:grid;grid-template-columns:1fr 1fr;gap:18px}.card{background:var(--card);border-radius:18px;padding:23px;box-shadow:var(--shadow);border:1px solid #e9f1f0}.card h2{font-size:18px;margin:0 0 4px}.card small{color:var(--muted)}.form{margin-top:20px;display:grid;grid-template-columns:1fr 1fr;gap:12px}.field{display:flex;flex-direction:column;gap:6px}.field.full{grid-column:1/-1}label{font-size:13px;color:#526c70}input,textarea,select{font:inherit;border:1px solid var(--line);border-radius:10px;padding:10px 11px;color:var(--ink);outline:0;background:#fbfdfd}input:focus,textarea:focus,select:focus{border-color:var(--teal);box-shadow:0 0 0 3px #197b7817}textarea{resize:vertical;min-height:42px}button{border:0;border-radius:10px;padding:11px 16px;font-weight:700;cursor:pointer;background:var(--teal);color:white}.form button{grid-column:1/-1;margin-top:4px}.sleep button{background:var(--coral)}.stats{display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin:18px 0}.stat{background:#fff;border-radius:14px;padding:17px 18px;border:1px solid var(--line)}.stat .num{font-size:25px;font-weight:800;margin-top:4px}.stat .label{font-size:12px;color:var(--muted)}.section{margin-top:28px}.section-head{display:flex;align-items:center;justify-content:space-between;margin-bottom:12px}.section h2{font-size:20px;margin:0}.table-wrap{background:#fff;border:1px solid var(--line);border-radius:16px;overflow:auto}.table{width:100%;border-collapse:collapse;min-width:650px}.table th,.table td{text-align:left;padding:13px 16px;border-bottom:1px solid #edf3f2;font-size:14px}.table th{font-size:12px;color:var(--muted);font-weight:600;background:#fbfdfd}.table tr:last-child td{border:0}.pill{display:inline-block;padding:4px 9px;border-radius:20px;font-size:12px;background:#e9f5f3;color:var(--teal)}.delete{background:none;color:#b08079;padding:4px 8px;font-weight:500}.empty{text-align:center;color:var(--muted);padding:30px}.toast{position:fixed;right:20px;bottom:20px;background:var(--ink);color:white;padding:12px 16px;border-radius:10px;display:none}.note{font-size:12px;color:var(--muted);margin-top:18px}@media(max-width:760px){.top{display:block}.date{margin-top:12px}.grid{grid-template-columns:1fr}.stats{grid-template-columns:1fr 1fr}.title{font-size:29px}}
</style></head><body><main class="shell"><div class="top"><div><div class="eyebrow">DAILY HEALTH LOG</div><h1 class="title">身体状态，逐日可见</h1><p class="sub">记录血压与睡眠，慢慢找到自己的节奏。</p></div><div class="date" id="today"></div></div>
<div class="grid"><section class="card"><h2>记录血压</h2><small>建议在安静状态下测量</small><form class="form" id="bp-form"><div class="field full"><label>测量时间</label><input id="bp-time" type="datetime-local" required></div><div class="field"><label>收缩压 / 高压 (mmHg)</label><input id="sys" type="number" min="50" max="300" placeholder="例如 120" required></div><div class="field"><label>舒张压 / 低压 (mmHg)</label><input id="dia" type="number" min="30" max="200" placeholder="例如 80" required></div><div class="field"><label>心率 (次/分钟，可选)</label><input id="pulse" type="number" min="30" max="250" placeholder="例如 72"></div><div class="field full"><label>备注</label><textarea id="bp-note" placeholder="比如：早晨、运动后、服药后"></textarea></div><button type="submit">保存血压记录</button></form></section>
<section class="card sleep"><h2>记录睡眠</h2><small>记录昨晚的入睡与起床情况</small><form class="form" id="sleep-form"><div class="field"><label>日期</label><input id="sleep-date" type="date" required></div><div class="field"><label>睡眠时长 (小时)</label><input id="duration" type="number" min="0" max="24" step="0.1" placeholder="例如 7.5" required></div><div class="field"><label>入睡时间</label><input id="bedtime" type="time" required></div><div class="field"><label>起床时间</label><input id="wake" type="time" required></div><div class="field"><label>睡眠质量</label><select id="quality"><option value="5">5 · 很好</option><option value="4">4 · 较好</option><option value="3" selected>3 · 一般</option><option value="2">2 · 较差</option><option value="1">1 · 很差</option></select></div><div class="field"><label>备注</label><input id="sleep-note" placeholder="比如：夜醒次数、午睡"></div><button type="submit">保存睡眠记录</button></form></section></div>
<div class="stats"><div class="stat"><div class="label">平均收缩压</div><div class="num" id="avg-sys">—</div></div><div class="stat"><div class="label">平均舒张压</div><div class="num" id="avg-dia">—</div></div><div class="stat"><div class="label">平均睡眠</div><div class="num" id="avg-sleep">—</div></div><div class="stat"><div class="label">睡眠质量</div><div class="num" id="avg-quality">—</div></div></div>
<section class="section"><div class="section-head"><h2>血压记录</h2><div><span class="pill" id="bp-count">0 条</span> <a class="export" href="/api/export?type=blood_pressure">导出 CSV</a></div></div><div class="table-wrap"><table class="table"><thead><tr><th>时间</th><th>血压</th><th>心率</th><th>备注</th><th></th></tr></thead><tbody id="bp-list"></tbody></table></div></section>
<section class="section"><div class="section-head"><h2>睡眠记录</h2><div><span class="pill" id="sleep-count">0 条</span> <a class="export" href="/api/export?type=sleep">导出 CSV</a></div></div><div class="table-wrap"><table class="table"><thead><tr><th>日期</th><th>时段</th><th>时长</th><th>质量</th><th>备注</th><th></th></tr></thead><tbody id="sleep-list"></tbody></table></div><p class="note">数据仅保存在本机目录的 health_records.db 文件中。健康记录仅供自我追踪，异常数值请咨询专业医务人员。</p></section></main><div class="toast" id="toast"></div>
<script>
const $=id=>document.getElementById(id); const pad=n=>String(n).padStart(2,'0');
function setDefaults(){const d=new Date();$('today').textContent=d.toLocaleDateString('zh-CN',{year:'numeric',month:'long',day:'numeric',weekday:'long'});$('bp-time').value=`${d.getFullYear()}-${pad(d.getMonth()+1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`;const y=new Date(d-86400000);$('sleep-date').value=`${y.getFullYear()}-${pad(y.getMonth()+1)}-${pad(y.getDate())}`}
function esc(s){return String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]))}
function empty(text){return `<tr><td colspan="6" class="empty">${text}</td></tr>`}
async function load(){const d=await (await fetch('/api/data')).json(); const s=d.stats; $('avg-sys').textContent=s.bp.sys??'—';$('avg-dia').textContent=s.bp.dia??'—';$('avg-sleep').textContent=s.sleep.duration?s.sleep.duration+' h':'—';$('avg-quality').textContent=s.sleep.quality?s.sleep.quality+' / 5':'—';$('bp-count').textContent=s.bp.n+' 条';$('sleep-count').textContent=s.sleep.n+' 条';
$('bp-list').innerHTML=d.blood_pressure.length?d.blood_pressure.map(x=>`<tr><td>${esc(x.measured_at.replace('T',' '))}</td><td><b>${x.systolic}</b> / <b>${x.diastolic}</b> mmHg</td><td>${x.pulse??'—'}</td><td>${esc(x.note)||'—'}</td><td><button class="delete" onclick="del('blood-pressure',${x.id})">删除</button></td></tr>`).join(''):empty('还没有血压记录');
$('sleep-list').innerHTML=d.sleep.length?d.sleep.map(x=>`<tr><td>${esc(x.sleep_date)}</td><td>${x.bedtime} — ${x.wake_time}</td><td><b>${x.duration}</b> h</td><td>${'★'.repeat(x.quality)}${'☆'.repeat(5-x.quality)}</td><td>${esc(x.note)||'—'}</td><td><button class="delete" onclick="del('sleep',${x.id})">删除</button></td></tr>`).join(''):empty('还没有睡眠记录')}
async function save(url,payload){const r=await fetch(url,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});if(!r.ok)throw new Error('保存失败');show('已保存');await load()}
$('bp-form').onsubmit=e=>{e.preventDefault();save('/api/blood-pressure',{measured_at:$('bp-time').value,systolic:$('sys').value,diastolic:$('dia').value,pulse:$('pulse').value,note:$('bp-note').value}).then(()=>e.target.reset()).catch(()=>show('请检查输入内容'))};
$('sleep-form').onsubmit=e=>{e.preventDefault();save('/api/sleep',{sleep_date:$('sleep-date').value,bedtime:$('bedtime').value,wake_time:$('wake').value,duration:$('duration').value,quality:$('quality').value,note:$('sleep-note').value}).then(()=>e.target.reset()).catch(()=>show('请检查输入内容'))};
async function del(type,id){if(!confirm('确定删除这条记录吗？'))return;await fetch(`/api/${type}?id=${id}`,{method:'DELETE'});show('已删除');load()} function show(t){$('toast').textContent=t;$('toast').style.display='block';setTimeout(()=>$('toast').style.display='none',1800)}setDefaults();load();
</script><script>if('serviceWorker' in navigator) navigator.serviceWorker.register('/sw.js').catch(()=>{});</script></body></html>'''

SW = """const CACHE = 'health-record-v2';
const ASSETS = ['/'];
self.addEventListener('install', event => {
  event.waitUntil(caches.open(CACHE).then(cache => cache.addAll(ASSETS)));
  self.skipWaiting();
});
self.addEventListener('activate', event => event.waitUntil(
  caches.keys().then(keys => Promise.all(keys.filter(key => key !== CACHE).map(key => caches.delete(key)))
    .then(() => self.clients.claim())
));
self.addEventListener('fetch', event => {
  if (event.request.url.includes('/api/')) return;
  if (event.request.mode === 'navigate') {
    event.respondWith(fetch(event.request).then(response => {
      const copy = response.clone(); caches.open(CACHE).then(cache => cache.put('/', copy)); return response;
    }).catch(() => caches.match('/')));
    return;
  }
  event.respondWith(caches.match(event.request).then(cached => cached || fetch(event.request)));
});"""

# 将睡眠表单收进健康记录主面板，并在手机端保持紧凑布局。
PAGE = PAGE.replace("</body>", """<script>
const panel=document.querySelector('.grid .card');
const sleepPanel=document.querySelector('.grid .card.sleep');
if(panel&&sleepPanel){
  sleepPanel.classList.remove('card'); sleepPanel.classList.add('subsection');
  panel.appendChild(sleepPanel); document.querySelector('.grid').style.display='block';
  sleepPanel.style.marginTop='20px'; sleepPanel.style.paddingTop='20px';
  sleepPanel.style.borderTop='1px solid #e4eeee'; sleepPanel.style.boxShadow='none'; sleepPanel.style.borderRadius='0';
}
const sleepForm=document.getElementById('sleep-form');
if(sleepForm){
  sleepForm.querySelectorAll('input,select,textarea').forEach(el=>el.removeAttribute('required'));
  document.getElementById('sleep-date').setAttribute('required','');
  const quality=document.getElementById('quality');
  quality.insertAdjacentHTML('afterbegin','<option value="">未填写</option>'); quality.value='';
}
document.querySelectorAll('.export').forEach(el=>{el.style.display='inline-block';el.style.padding='3px 8px';el.style.border='1px solid #cfe4e1';el.style.borderRadius='999px';el.style.fontSize='12px';el.style.color='#197b78';el.style.textDecoration='none';el.style.marginLeft='6px'});
</script></body>""")

# 服务端直接合并两个表单，避免旧浏览器或缓存导致仍显示为两个面板。
PAGE = PAGE.replace(
    '</form></section>\n<section class="card sleep">',
    '</form><div class="divider"></div><div class="subsection sleep">'
)
PAGE = PAGE.replace(
    '</form></section></div>\n<div class="stats">',
    '</form></div></section></div>\n<div class="stats">'
)
# 将睡眠字段直接并入血压表单：页面只保留一个保存按钮。
PAGE = PAGE.replace(
    '</form><div class="divider"></div><div class="subsection sleep"><h2>记录睡眠</h2><small>记录昨晚的入睡与起床情况</small><form class="form" id="sleep-form">',
    '<div class="divider"></div><div class="subsection sleep"><h2>睡眠记录（可选）</h2><small>填写后将与本次血压记录一起保存</small>'
)
PAGE = PAGE.replace(
    '<input id="sleep-date" type="date" required>',
    '<input id="sleep-date" type="date">'
)
PAGE = PAGE.replace(
    '<button type="submit">保存睡眠记录</button></form></div></section></div>',
    '</div><button type="submit">保存健康记录</button></form></section></div>'
)
PAGE = PAGE.replace(
    "$('bp-form').onsubmit=e=>{e.preventDefault();save('/api/blood-pressure',{measured_at:$('bp-time').value,systolic:$('sys').value,diastolic:$('dia').value,pulse:$('pulse').value,note:$('bp-note').value}).then(()=>e.target.reset()).catch(()=>show('请检查输入内容'))};",
    "$('bp-form').onsubmit=async e=>{e.preventDefault();try{await save('/api/blood-pressure',{measured_at:$('bp-time').value,systolic:$('sys').value,diastolic:$('dia').value,pulse:$('pulse').value,note:$('bp-note').value});const hasSleep=$('duration').value||$('bedtime').value||$('wake').value||$('sleep-note').value||$('quality').value;if(hasSleep)await save('/api/sleep',{sleep_date:$('sleep-date').value||new Date().toISOString().slice(0,10),bedtime:$('bedtime').value,wake_time:$('wake').value,duration:$('duration').value,quality:$('quality').value,note:$('sleep-note').value});e.target.reset();show('健康记录已保存')}catch(err){show('请检查输入内容')}};"
)
PAGE = PAGE.replace(
    "$('sleep-form').onsubmit=e=>{e.preventDefault();save('/api/sleep',{sleep_date:$('sleep-date').value,bedtime:$('bedtime').value,wake_time:$('wake').value,duration:$('duration').value,quality:$('quality').value,note:$('sleep-note').value}).then(()=>e.target.reset()).catch(()=>show('请检查输入内容'))};",
    ""
)
# 睡眠字段保留原来的两列网格，但不再创建独立表单。
PAGE = PAGE.replace(
    '<div class="subsection sleep"><h2>睡眠记录（可选）</h2><small>填写后将与本次血压记录一起保存</small>',
    '<div class="subsection sleep"><h2>睡眠记录（可选）</h2><small>填写后将与本次血压记录一起保存</small><div class="form sleep-fields">'
)
PAGE = PAGE.replace(
    '<input id="duration" type="number" min="0" max="24" step="0.1" placeholder="例如 7.5" required>',
    '<input id="duration" type="number" min="0" max="24" step="0.1" placeholder="例如 7.5">'
)
PAGE = PAGE.replace(
    '<input id="bedtime" type="time" required>',
    '<input id="bedtime" type="time">'
)
PAGE = PAGE.replace(
    '<input id="wake" type="time" required>',
    '<input id="wake" type="time">'
)
PAGE = PAGE.replace(
    '<select id="quality"><option value="5">',
    '<select id="quality"><option value="">未填写</option><option value="5">'
)
PAGE = PAGE.replace(
    '<input id="sleep-note" placeholder="比如：夜醒次数、午睡"></div></div><button type="submit">保存健康记录',
    '<input id="sleep-note" placeholder="比如：夜醒次数、午睡"></div></div></div><button type="submit">保存健康记录'
)
PAGE = PAGE.replace(
    '<button type="submit">保存血压记录</button><div class="divider">',
    '<div class="divider">'
)
PAGE = PAGE.replace(
    '<option value="3" selected>3 · 一般</option>',
    '<option value="3">3 · 一般</option>'
)
# 历史记录也统一成一个列表：按日期把睡眠信息显示在血压记录同一行。
bp_start = '<section class="section"><div class="section-head"><h2>血压记录</h2>'
bp_pos = PAGE.find(bp_start)
bp_end = PAGE.find('</section>', bp_pos) + len('</section>')
sleep_start = '<section class="section"><div class="section-head"><h2>睡眠记录</h2>'
sleep_pos = PAGE.find(sleep_start)
sleep_end = PAGE.find('</section>', sleep_pos) + len('</section>')
if bp_pos >= 0 and sleep_pos >= 0:
    combined_history = '''<section class="section"><div class="section-head"><h2>血压记录</h2><div><span class="pill" id="bp-count">0 条</span> <a class="export" href="/api/export?type=health">导出 CSV</a></div></div><div class="table-wrap"><table class="table"><thead><tr><th>时间</th><th>血压</th><th>心率</th><th>睡眠</th><th>质量</th><th>备注</th><th></th></tr></thead><tbody id="bp-list"></tbody></table></div><p class="note">睡眠信息已按日期合并到血压记录中。数据仅保存在本机目录的 health_records.db 文件中。</p></section>'''
    PAGE = PAGE[:bp_pos] + combined_history + PAGE[bp_end:sleep_pos] + PAGE[sleep_end:]
    PAGE = PAGE.replace(
        "const s=d.stats; $('avg-sys')",
        "const s=d.stats; const sleepByDate=Object.fromEntries(d.sleep.map(x=>[x.sleep_date,x])); $('avg-sys')"
    )
    PAGE = re.sub(
        r"\$\('bp-list'\)\.innerHTML=.*?\n\$\('sleep-list'\)\.innerHTML=.*?",
        "$('bp-list').innerHTML=d.blood_pressure.length?d.blood_pressure.map(x=>{const sl=sleepByDate[x.measured_at.slice(0,10)]||{};return `<tr><td>${esc(x.measured_at.replace('T',' '))}</td><td><b>${x.systolic}</b> / <b>${x.diastolic}</b> mmHg</td><td>${x.pulse??'—'}</td><td>${sl.duration?sl.duration+' h':'—'}</td><td>${sl.quality?'★'.repeat(sl.quality):'—'}</td><td>${esc(x.note)||esc(sl.note)||'—'}</td><td><button class=\"delete\" onclick=\"del('blood-pressure',${x.id})\">删除</button></td></tr>`}).join(''):empty('还没有记录')",
        PAGE,
        flags=re.S
    )

# 最终页面采用单一、响应式健康记录面板；不再依赖前面的兼容性拼接布局。
PAGE = r'''<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta name="theme-color" content="#167a74"><title>健康记录</title><style>
:root{--ink:#16383b;--muted:#6b8588;--line:#dceaea;--bg:#f3f8f7;--teal:#167a74;--soft:#e8f4f2;--white:#fff}*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font-family:-apple-system,BlinkMacSystemFont,"PingFang SC","Segoe UI",sans-serif}.wrap{max-width:960px;margin:auto;padding:28px 18px 56px}header{margin-bottom:22px}h1{font-size:30px;margin:0 0 6px}header p,.hint{color:var(--muted);margin:0}.card{background:var(--white);border:1px solid var(--line);border-radius:18px;padding:24px;box-shadow:0 8px 28px #17413b0d}.card h2,.records h2{font-size:20px;margin:0 0 5px}.form-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:14px;margin-top:18px}.field{display:flex;flex-direction:column;gap:6px}.full{grid-column:1/-1}label{font-size:13px;font-weight:600;color:#587277}input,textarea,select{width:100%;border:1px solid var(--line);border-radius:10px;background:#fbfdfd;color:var(--ink);font:inherit;padding:11px 12px;outline:0}input:focus,textarea:focus,select:focus{border-color:var(--teal);box-shadow:0 0 0 3px #167a7414}textarea{min-height:48px;resize:vertical}.divider{height:1px;background:var(--line);margin:25px 0 20px}.optional{color:var(--muted);font-size:13px}.save{width:100%;margin-top:22px;border:0;border-radius:10px;padding:13px;background:var(--teal);color:#fff;font:inherit;font-weight:700;cursor:pointer}.summary{display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin:20px 0}.stat{background:#fff;border:1px solid var(--line);border-radius:14px;padding:15px}.stat small{color:var(--muted)}.stat strong{display:block;font-size:25px;margin-top:4px}.records{margin-top:28px}.records-head{display:flex;align-items:center;justify-content:space-between;margin-bottom:11px}.right{display:flex;gap:8px;align-items:center}.badge,.export{border-radius:99px;padding:4px 9px;font-size:12px}.badge{background:var(--soft);color:var(--teal)}.export{border:1px solid #b9ded9;color:var(--teal);text-decoration:none}.table-box{overflow:auto;background:#fff;border:1px solid var(--line);border-radius:15px}table{border-collapse:collapse;width:100%;min-width:740px}th,td{padding:13px 15px;text-align:left;border-bottom:1px solid #edf3f2;font-size:14px}th{background:#fbfdfd;color:var(--muted);font-size:12px}tr:last-child td{border:0}.del{border:0;background:none;color:#b07068;cursor:pointer;font:inherit}.empty{text-align:center;color:var(--muted);padding:28px}.toast{position:fixed;bottom:20px;left:50%;transform:translateX(-50%);background:var(--ink);color:white;padding:10px 14px;border-radius:9px;display:none}@media(max-width:620px){.wrap{padding:20px 14px}.card{padding:18px}.form-grid{grid-template-columns:1fr}.summary{grid-template-columns:1fr 1fr}h1{font-size:26px}}
</style></head><body><main class="wrap"><header><h1>健康记录</h1><p>一次保存血压和睡眠信息，睡眠内容可不填。</p></header><section class="card"><h2>新增记录</h2><p class="hint">血压为必填项；睡眠会附在本次记录中。</p><form id="health-form"><div class="form-grid"><div class="field full"><label>测量时间 *</label><input id="bp-time" type="datetime-local" required></div><div class="field"><label>收缩压 / 高压 (mmHg) *</label><input id="sys" type="number" min="50" max="300" placeholder="例如 120" required></div><div class="field"><label>舒张压 / 低压 (mmHg) *</label><input id="dia" type="number" min="30" max="200" placeholder="例如 80" required></div><div class="field"><label>心率（可选）</label><input id="pulse" type="number" min="30" max="250" placeholder="例如 72"></div><div class="field"><label>血压备注（可选）</label><input id="bp-note" placeholder="例如：早起、服药后"></div></div><div class="divider"></div><h2>睡眠信息 <span class="optional">（可选）</span></h2><div class="form-grid"><div class="field"><label>睡眠日期</label><input id="sleep-date" type="date"></div><div class="field"><label>睡眠时长（小时）</label><input id="duration" type="number" min="0" max="24" step="0.1" placeholder="例如 7.5"></div><div class="field"><label>入睡时间</label><input id="bedtime" type="time"></div><div class="field"><label>起床时间</label><input id="wake" type="time"></div><div class="field"><label>睡眠质量</label><select id="quality"><option value="">未填写</option><option value="5">5 · 很好</option><option value="4">4 · 较好</option><option value="3">3 · 一般</option><option value="2">2 · 较差</option><option value="1">1 · 很差</option></select></div><div class="field"><label>睡眠备注</label><input id="sleep-note" placeholder="例如：夜醒、午睡"></div></div><button class="save" type="submit">保存健康记录</button></form></section><section class="summary"><div class="stat"><small>平均收缩压</small><strong id="avg-sys">—</strong></div><div class="stat"><small>平均舒张压</small><strong id="avg-dia">—</strong></div><div class="stat"><small>平均睡眠</small><strong id="avg-sleep">—</strong></div><div class="stat"><small>睡眠质量</small><strong id="avg-quality">—</strong></div></section><section class="records"><div class="records-head"><h2>血压记录</h2><div class="right"><span class="badge" id="count">0 条</span><a class="export" href="/api/export?type=health">导出 CSV</a></div></div><div class="table-box"><table><thead><tr><th>时间</th><th>血压</th><th>心率</th><th>睡眠</th><th>质量</th><th>备注</th><th></th></tr></thead><tbody id="list"></tbody></table></div></section></main><div class="toast" id="toast"></div><script>
const $=id=>document.getElementById(id),pad=n=>String(n).padStart(2,'0'),esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));function toast(s){$('toast').textContent=s;$('toast').style.display='block';setTimeout(()=>$('toast').style.display='none',1800)}function defaults(){let d=new Date();$('bp-time').value=`${d.getFullYear()}-${pad(d.getMonth()+1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`;d=new Date(d-86400000);$('sleep-date').value=`${d.getFullYear()}-${pad(d.getMonth()+1)}-${pad(d.getDate())}`}async function request(url,opts){const r=await fetch(url,opts);if(!r.ok)throw Error();return r.json()}async function load(){const d=await request('/api/data');const st=d.stats,sleeps=Object.fromEntries(d.sleep.filter(x=>x.blood_pressure_id).map(x=>[x.blood_pressure_id,x]));$('avg-sys').textContent=st.bp.sys??'—';$('avg-dia').textContent=st.bp.dia??'—';$('avg-sleep').textContent=st.sleep.duration?st.sleep.duration+' h':'—';$('avg-quality').textContent=st.sleep.quality?st.sleep.quality+' / 5':'—';$('count').textContent=st.bp.n+' 条';$('list').innerHTML=d.blood_pressure.length?d.blood_pressure.map(x=>{let s=sleeps[x.id]||{};return `<tr><td>${esc(x.measured_at.replace('T',' '))}</td><td><b>${x.systolic}</b> / <b>${x.diastolic}</b></td><td>${x.pulse??'—'}</td><td>${s.duration?s.duration+' h':'—'}</td><td>${s.quality?'★'.repeat(s.quality):'—'}</td><td>${esc(x.note)||esc(s.note)||'—'}</td><td><button class="del" onclick="removeRecord(${x.id})">删除</button></td></tr>`}).join(''):'<tr><td colspan="7" class="empty">还没有记录</td></tr>'}async function removeRecord(id){if(!confirm('确定删除这条健康记录吗？'))return;await request('/api/blood-pressure?id='+id,{method:'DELETE'});toast('已删除');load()}$('health-form').onsubmit=async e=>{e.preventDefault();try{const bp=await request('/api/blood-pressure',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({measured_at:$('bp-time').value,systolic:$('sys').value,diastolic:$('dia').value,pulse:$('pulse').value,note:$('bp-note').value})});const hasSleep=['duration','bedtime','wake','quality','sleep-note'].some(id=>$(id).value);if(hasSleep)await request('/api/sleep',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({blood_pressure_id:bp.id,sleep_date:$('sleep-date').value||$('bp-time').value.slice(0,10),duration:$('duration').value,bedtime:$('bedtime').value,wake_time:$('wake').value,quality:$('quality').value,note:$('sleep-note').value})});e.target.reset();defaults();await load();toast('已保存')}catch{toast('请检查血压必填项')}};defaults();load();</script></body></html>'''
PAGE = PAGE.replace('<div class="field"><label>睡眠日期</label><input id="sleep-date" type="date"></div>', '')
PAGE = PAGE.replace('<div class="field"><label>入睡时间</label><input id="bedtime" type="time"></div>', '')
PAGE = PAGE.replace('<div class="field"><label>起床时间</label><input id="wake" type="time"></div>', '')
PAGE = PAGE.replace('</body>', '<script>if("serviceWorker" in navigator) navigator.serviceWorker.register("/sw.js").catch(()=>{});</script></body>')
PAGE = PAGE.replace('id="bp-time" type="datetime-local" required', 'id="bp-time" type="datetime-local"')
PAGE = PAGE.replace('id="sys" type="number" min="50" max="300" placeholder="例如 120" required', 'id="sys" type="number" min="50" max="300" placeholder="例如 120"')
PAGE = PAGE.replace('id="dia" type="number" min="30" max="200" placeholder="例如 80" required', 'id="dia" type="number" min="30" max="200" placeholder="例如 80"')
PAGE = re.sub(
    r"\$\('health-form'\)\.onsubmit=async e=>\{.*?\};defaults\(\);load\(\);",
    "$('health-form').onsubmit=async e=>{e.preventDefault();try{const hasBp=$('sys').value||$('dia').value||$('pulse').value||$('bp-note').value;const hasSleep=['duration','quality','sleep-note'].some(id=>$(id).value);if(!hasBp&&!hasSleep)throw Error('empty');let bp=null;if(hasBp){if(!$('sys').value||!$('dia').value)throw Error('bp');bp=await request('/api/blood-pressure',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({measured_at:$('bp-time').value||new Date().toISOString().slice(0,16),systolic:$('sys').value,diastolic:$('dia').value,pulse:$('pulse').value,note:$('bp-note').value})})}if(hasSleep)await request('/api/sleep',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({blood_pressure_id:bp?bp.id:null,sleep_date:$('bp-time').value.slice(0,10)||new Date().toISOString().slice(0,10),duration:$('duration').value,quality:$('quality').value,note:$('sleep-note').value})});e.target.reset();defaults();await load();toast('已保存')}catch(err){toast(err.message==='bp'?'收缩压和舒张压需要一起填写':err.message==='empty'?'至少填写血压或睡眠信息':'保存失败')}};defaults();load();",
    PAGE,
    flags=re.S
)
# 只有睡眠、没有血压的记录也显示在同一个“血压记录”面板中。
PAGE = re.sub(
    r"\$\('list'\)\.innerHTML=.*?\}async function removeRecord",
    "const rows=[];const linked=new Set();d.blood_pressure.forEach(x=>{const s=sleeps[x.id]||{};if(s.id)linked.add(s.id);rows.push({date:x.measured_at,html:`<tr><td>${esc(x.measured_at.replace('T',' '))}</td><td><b>${x.systolic}</b> / <b>${x.diastolic}</b></td><td>${x.pulse??'—'}</td><td>${s.duration?s.duration+' h':'—'}</td><td>${s.quality?'★'.repeat(s.quality):'—'}</td><td>${esc(x.note)||esc(s.note)||'—'}</td><td><button class=\"del\" onclick=\"removeRecord(${x.id})\">删除</button></td></tr>`})});d.sleep.filter(s=>!linked.has(s.id)).forEach(s=>rows.push({date:s.sleep_date,html:`<tr><td>${esc(s.sleep_date)}</td><td>—</td><td>—</td><td>${s.duration?s.duration+' h':'—'}</td><td>${s.quality?'★'.repeat(s.quality):'—'}</td><td>${esc(s.note)||'—'}</td><td><button class=\"del\" onclick=\"removeSleep(${s.id})\">删除</button></td></tr>`}));rows.sort((a,b)=>b.date.localeCompare(a.date));$('list').innerHTML=rows.length?rows.map(r=>r.html).join(''):'<tr><td colspan=\"7\" class=\"empty\">还没有记录</td></tr>'}async function removeRecord",
    PAGE,
    flags=re.S
)
PAGE = PAGE.replace(
    "async function removeRecord(id){if(!confirm('确定删除这条健康记录吗？'))return;await request('/api/blood-pressure?id='+id,{method:'DELETE'});toast('已删除');load()}",
    "async function removeRecord(id){if(!confirm('确定删除这条健康记录吗？'))return;await request('/api/blood-pressure?id='+id,{method:'DELETE'});toast('已删除');load()}async function removeSleep(id){if(!confirm('确定删除这条睡眠记录吗？'))return;await request('/api/sleep?id='+id,{method:'DELETE'});toast('已删除');load()}"
)
PAGE = re.sub(r'<section class="summary">.*?</section>', '', PAGE, flags=re.S)
PAGE = re.sub(
    r"const st=d\.stats,sleeps=.*?;\$\('avg-quality'\)\.textContent=.*?;",
    "const sleeps=Object.fromEntries(d.sleep.filter(x=>x.blood_pressure_id).map(x=>[x.blood_pressure_id,x]));",
    PAGE,
    flags=re.S
)
PAGE = PAGE.replace("$('count').textContent=st.bp.n+' 条';", "$('count').textContent=d.blood_pressure.length+' 条';")
# 删除已移除睡眠日期输入框后的旧初始化代码，避免脚本提前报错。
PAGE = re.sub(r";d=new Date\(d-86400000\);\$\('sleep-date'\)\.value=`[^`]*`", "", PAGE)
PAGE = PAGE.replace(
    "async function request(url,opts){const r=await fetch(url,opts);if(!r.ok)throw Error();return r.json()}",
    "async function request(url,opts){const r=await fetch(url,opts);if(!r.ok){let detail='HTTP '+r.status;try{const body=await r.json();detail=body.error||detail}catch{}throw Error(detail)}return r.json()}"
)


if __name__ == "__main__":
    init_db()
    try:
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        probe.connect(("8.8.8.8", 80))
        local_ip = probe.getsockname()[0]
        probe.close()
    except OSError:
        local_ip = "本机局域网 IP"
    print(f"电脑访问：http://127.0.0.1:{PORT}")
    print(f"手机访问：http://{local_ip}:{PORT}（手机和电脑需连接同一 Wi-Fi）")
    try:
        ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()
    except OSError as exc:
        if getattr(exc, "errno", None) in (48, 98):
            print(f"端口 {PORT} 已被占用，已有程序正在运行，请直接打开 http://127.0.0.1:{PORT}")
        else:
            raise

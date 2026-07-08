# -*- coding: utf-8 -*-
"""
pepper_admin.py  --  a small web admin panel for the Pepper games and general
robot control, with a built-in Wiki / cheat-sheet.

It uses only the Python 2.7 standard library for the web server (no Flask), plus
the packages the games already rely on: qi (pynaoqi), paramiko and requests.

Run it inside the 'pepper' conda env with pynaoqi on PYTHONPATH, e.g. via
start_pepper_admin.ps1, then open http://127.0.0.1:8080 in your browser.

    conda activate pepper
    set HCTL_TOKEN=eyJhbGci...
    set PEPPER_SSH_PASSWORD=...
    set PYTHONPATH=...\pynaoqi...\lib
    python pepper_admin.py            # --port 8080  --ip 192.168.137.214
"""

import os
import sys
import json
import time
import threading
import argparse
import subprocess

from BaseHTTPServer import BaseHTTPRequestHandler, HTTPServer
from SocketServer import ThreadingMixIn

import qi

from pepper_echo import LANGUAGE_DICT, TOKEN_ENV_VAR, PEPPER_PW_ENV_VAR
from pepper_parrot_game import (find_pepper, show_on_tablet, set_eyes,
                                NAOQI_PORT, HOTSPOT_SUBNET)

HERE = os.path.dirname(os.path.abspath(__file__))
SSH_USER = 'nao'
SSH_PW = os.environ.get(PEPPER_PW_ENV_VAR)

LOCK = threading.Lock()
STATE = {'ip': None, 'session': None}
GAME = {'proc': None, 'name': None, 'logpath': os.path.join(HERE, 'game_output.log')}
KIOSK = {'proc': None, 'logpath': os.path.join(HERE, 'kiosk_output.log')}


# --------------------------------------------------------------------------
# robot helpers
# --------------------------------------------------------------------------
def get_session(ip=None, scan=False):
    """Return a live qi session, (re)connecting if needed.

    scan=True is only used by the explicit /api/connect call; the frequent
    status poll passes scan=False so it never triggers a slow subnet scan when
    Pepper is off (which would pile up on every poll).
    """
    if ip:
        STATE['ip'] = ip
    if STATE['session'] is not None:
        try:
            STATE['session'].service('ALMemory')      # cheap liveness check
            return STATE['session']
        except Exception:
            STATE['session'] = None
    target = STATE['ip'] or (find_pepper() if scan else None)
    if not target:
        raise RuntimeError('not connected (press Connect, or check Pepper is on)')
    s = qi.Session()
    s.connect('tcp://%s:%d' % (target, NAOQI_PORT))
    STATE['ip'] = target
    STATE['session'] = s
    return s


def svc(name, ip=None):
    return get_session(ip).service(name)


def has_service(session, name):
    try:
        return any(x['name'] == name
                   for x in session.service('ServiceDirectory').services())
    except Exception:
        return False


def game_running():
    return bool(GAME['proc'] and GAME['proc'].poll() is None)


def collect_status():
    st = {'connected': False, 'ip': STATE['ip'],
          'game': GAME['name'] if game_running() else None,
          'kiosk': kiosk_running()}
    try:
        s = get_session()
        st['connected'] = True
        st['ip'] = STATE['ip']
        try:
            st['battery'] = s.service('ALBattery').getBatteryCharge()
        except Exception:
            st['battery'] = None
        try:
            st['volume'] = s.service('ALAudioDevice').getOutputVolume()
        except Exception:
            st['volume'] = None
        try:
            st['life'] = s.service('ALAutonomousLife').getState()
        except Exception:
            st['life'] = None
        try:
            st['posture'] = s.service('ALRobotPosture').getPosture()
        except Exception:
            st['posture'] = None
        st['tablet'] = has_service(s, 'ALTabletService')
    except Exception as err:
        st['error'] = str(err)
    return st


def sftp_conn(ip):
    import paramiko
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    ssh.connect(ip, 22, SSH_USER, SSH_PW, timeout=10)
    return ssh


# --------------------------------------------------------------------------
# game process management
# --------------------------------------------------------------------------
GAME_SCRIPTS = {
    'parrot': 'pepper_parrot_game.py',
    'qa': 'pepper_qa_game.py',
    'echo': 'pepper_echo.py',
    'guess': 'pepper_guess_game.py',
}


def kiosk_running():
    return bool(KIOSK['proc'] and KIOSK['proc'].poll() is None)


def start_kiosk(rounds, category='mixed', duration=6):
    if kiosk_running():
        return False, 'the tablet menu is already running'
    if GAME['proc'] and GAME['proc'].poll() is None:
        return False, 'stop the running game first'
    ip = STATE['ip'] or find_pepper()
    if not ip:
        return False, 'Pepper not found'
    cmd = [sys.executable, os.path.join(HERE, 'pepper_kiosk.py'), '-ip', ip,
           '-r', str(rounds), '-d', str(duration), '-c', category]
    STATE['session'] = None       # let the kiosk own the tablet
    logf = open(KIOSK['logpath'], 'w')
    env = dict(os.environ)
    env['PYTHONUNBUFFERED'] = '1'
    KIOSK['proc'] = subprocess.Popen(cmd, stdout=logf, stderr=subprocess.STDOUT,
                                     cwd=HERE, env=env)
    return True, 'tablet menu started (pid %d)' % KIOSK['proc'].pid


def stop_kiosk():
    p = KIOSK['proc']
    if not p or p.poll() is not None:
        return False, 'the tablet menu is not running'
    try:
        p.terminate()
        time.sleep(0.5)
        if p.poll() is None:
            p.kill()
    except Exception as err:
        return False, str(err)
    return True, 'tablet menu stopped'


def start_game(name, lang, duration, rounds, category='mixed'):
    if kiosk_running():
        return False, 'stop the tablet menu first'
    if GAME['proc'] and GAME['proc'].poll() is None:
        return False, 'a game is already running (%s)' % GAME['name']
    script = GAME_SCRIPTS.get(name)
    if not script:
        return False, 'unknown game %r' % name
    ip = STATE['ip'] or find_pepper()
    if not ip:
        return False, 'Pepper not found'
    cmd = [sys.executable, os.path.join(HERE, script), '-ip', ip, '-l', lang,
           '-d', str(duration)]
    if name in ('parrot', 'qa', 'guess'):
        cmd += ['-r', str(rounds)]
    if name == 'guess':
        cmd += ['-c', category]
    if name == 'echo' and rounds != 1:
        cmd += ['--loop']
    # release our own session so the game gets clean control of TTS/mics
    STATE['session'] = None
    logf = open(GAME['logpath'], 'w')
    env = dict(os.environ)
    env['PYTHONUNBUFFERED'] = '1'          # so the live log updates in real time
    proc = subprocess.Popen(cmd, stdout=logf, stderr=subprocess.STDOUT,
                            cwd=HERE, env=env)
    GAME['proc'] = proc
    GAME['name'] = name
    return True, 'started %s (pid %d)' % (name, proc.pid)


def stop_game():
    p = GAME['proc']
    if not p or p.poll() is not None:
        GAME['name'] = None
        return False, 'no game running'
    try:
        p.terminate()
        time.sleep(0.5)
        if p.poll() is None:
            p.kill()
    except Exception as err:
        return False, str(err)
    name = GAME['name']
    GAME['name'] = None
    return True, 'stopped %s' % name


def game_log_tail(n=4000):
    try:
        with open(GAME['logpath']) as f:
            data = f.read()
        return data[-n:]
    except Exception:
        return ''


# --------------------------------------------------------------------------
# HTTP handlers
# --------------------------------------------------------------------------
def api(path, body):
    """Dispatch an /api/* call; returns a JSON-serialisable dict."""
    if path == '/api/status':
        return collect_status()

    if path == '/api/connect':
        get_session(body.get('ip') or None, scan=True)
        return {'ok': True, 'ip': STATE['ip']}

    if path == '/api/say':
        s = get_session()
        tts = s.service('ALTextToSpeech')
        lang = body.get('lang')
        if lang and lang in LANGUAGE_DICT.values():
            try: tts.setLanguage(lang)
            except Exception: pass
        tts.say(body.get('text', '').encode('utf-8'))
        return {'ok': True}

    if path == '/api/volume':
        s = get_session()
        s.service('ALAudioDevice').setOutputVolume(int(body.get('value', 60)))
        return {'ok': True}

    if path == '/api/posture':
        s = get_session()
        action = body.get('action')
        if action == 'wake':
            s.service('ALMotion').wakeUp()
        elif action == 'rest':
            s.service('ALMotion').rest()
        else:
            # Pepper (wheeled base) supports only: Crouch, Stand, StandInit, StandZero
            names = {'stand': 'Stand', 'standinit': 'StandInit', 'crouch': 'Crouch'}
            s.service('ALRobotPosture').goToPosture(names.get(action, 'Stand'), 0.6)
        return {'ok': True}

    if path == '/api/life':
        s = get_session()
        s.service('ALAutonomousLife').setState(body.get('state', 'solitary'))
        return {'ok': True}

    if path == '/api/leds':
        s = get_session()
        color = body.get('color', 'off')
        set_eyes(s, None if color == 'off' else int(color.lstrip('#'), 16))
        return {'ok': True}

    if path == '/api/tablet':
        s = get_session()
        if body.get('action') == 'reset':
            try: s.service('ALTabletService').hideWebview()
            except Exception: pass
            return {'ok': True}
        ip = STATE['ip']
        ssh = sftp_conn(ip)
        try:
            ok = show_on_tablet(s, (ssh, ssh.open_sftp()),
                                body.get('title', ''), body.get('text', ''),
                                bg=body.get('bg', '#0f4c81'))
        finally:
            ssh.close()
        return {'ok': ok}

    if path == '/api/tablet/service':
        # best-effort attempt to bring ALTabletService back
        s = get_session()
        try:
            sm = s.service('ALServiceManager')
            sm.restartService('ALTabletService')
        except Exception:
            pass
        time.sleep(2)
        return {'ok': has_service(s, 'ALTabletService'),
                'note': 'If still down, tap the tablet / relaunch its app or reboot.'}

    if path == '/api/game/start':
        ok, msg = start_game(body.get('game', 'qa'), body.get('lang', 'en'),
                             int(body.get('duration', 6)), int(body.get('rounds', 0)),
                             body.get('category', 'mixed'))
        return {'ok': ok, 'msg': msg}

    if path == '/api/game/stop':
        ok, msg = stop_game()
        return {'ok': ok, 'msg': msg}

    if path == '/api/game/log':
        running = bool(GAME['proc'] and GAME['proc'].poll() is None)
        return {'running': running, 'name': GAME['name'], 'log': game_log_tail()}

    if path == '/api/kiosk/start':
        ok, msg = start_kiosk(int(body.get('rounds', 3)),
                              body.get('category', 'mixed'),
                              int(body.get('duration', 6)))
        return {'ok': ok, 'msg': msg}

    if path == '/api/kiosk/stop':
        ok, msg = stop_kiosk()
        return {'ok': ok, 'msg': msg}

    raise RuntimeError('unknown endpoint %s' % path)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass  # keep the console quiet

    def _send(self, code, ctype, body):
        self.send_response(code)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path in ('/', '/index.html'):
            return self._send(200, 'text/html; charset=utf-8', PAGE.encode('utf-8'))
        if self.path.startswith('/api/'):
            return self._api('GET')
        self._send(404, 'text/plain', b'not found')

    def do_POST(self):
        if self.path.startswith('/api/'):
            return self._api('POST')
        self._send(404, 'text/plain', b'not found')

    def _api(self, method):
        length = int(self.headers.get('Content-Length', 0) or 0)
        raw = self.rfile.read(length) if length else b''
        try:
            body = json.loads(raw) if raw else {}
        except Exception:
            body = {}
        try:
            with LOCK:
                result = api(self.path.split('?')[0], body)
            payload = json.dumps(result)
            self._send(200, 'application/json', payload.encode('utf-8'))
        except Exception as err:
            payload = json.dumps({'ok': False, 'error': str(err)})
            self._send(200, 'application/json', payload.encode('utf-8'))


class ThreadingServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True


# --------------------------------------------------------------------------
# the single-page UI (HTML + CSS + JS) and the embedded Wiki
# --------------------------------------------------------------------------
PAGE = u"""<!doctype html><html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Pepper Admin</title>
<style>
:root{--bg:#0f1720;--card:#182430;--acc:#2d9cdb;--ok:#27ae60;--warn:#e2b13c;--err:#e05a4d;--txt:#e6edf3;--mut:#8aa0b3}
*{box-sizing:border-box}body{margin:0;font-family:Segoe UI,Arial,sans-serif;background:var(--bg);color:var(--txt)}
header{background:#0b1118;padding:14px 20px;display:flex;align-items:center;gap:14px;border-bottom:1px solid #22303c}
header h1{font-size:18px;margin:0}.pill{padding:3px 10px;border-radius:12px;font-size:12px;background:#22303c;color:var(--mut)}
.pill.on{background:rgba(39,174,96,.2);color:#7ee2a8}.pill.off{background:rgba(224,90,77,.2);color:#f0a79f}
nav{display:flex;gap:6px;padding:12px 20px 0}nav button{background:none;border:none;color:var(--mut);padding:8px 14px;cursor:pointer;border-radius:8px 8px 0 0;font-size:14px}
nav button.active{background:var(--card);color:var(--txt)}
main{padding:20px;max-width:1000px;margin:0 auto}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));gap:16px}
.card{background:var(--card);border:1px solid #22303c;border-radius:12px;padding:16px}
.card h3{margin:0 0 12px;font-size:14px;color:var(--acc);text-transform:uppercase;letter-spacing:.5px}
button.act{background:var(--acc);color:#04121c;border:none;padding:9px 14px;border-radius:8px;cursor:pointer;font-weight:600;margin:3px}
button.act.ghost{background:#22303c;color:var(--txt)}button.act.warn{background:var(--warn)}button.act.err{background:var(--err);color:#fff}
input,select,textarea{background:#0d151d;border:1px solid #2a3a48;color:var(--txt);border-radius:8px;padding:8px;width:100%;margin:4px 0}
label{font-size:12px;color:var(--mut)}.row{display:flex;gap:8px;align-items:center}.row>*{flex:1}
pre{background:#0a1017;border:1px solid #22303c;border-radius:8px;padding:12px;overflow:auto;max-height:340px;font-size:12px;white-space:pre-wrap}
.kv{display:flex;justify-content:space-between;padding:4px 0;border-bottom:1px solid #1d2a35;font-size:13px}.kv b{color:var(--mut);font-weight:500}
.wiki h2{color:var(--acc);font-size:16px;margin-top:22px}.wiki code{background:#0a1017;padding:2px 6px;border-radius:5px}
.wiki table{border-collapse:collapse;width:100%;font-size:13px}.wiki td{border:1px solid #22303c;padding:6px 10px}
.hidden{display:none}.mut{color:var(--mut);font-size:12px}
</style></head><body>
<header>
  <h1>🤖 Pepper Admin</h1>
  <span id="connPill" class="pill off">disconnected</span>
  <span id="ipPill" class="pill">ip: ?</span>
  <span id="battPill" class="pill">batt: ?</span>
  <span id="tabPill" class="pill">tablet: ?</span>
  <span id="gamePill" class="pill">game: idle</span>
  <span style="flex:1"></span>
  <button class="act ghost" onclick="refresh()">↻ refresh</button>
</header>
<nav>
  <button id="t-control" class="active" onclick="tab('control')">Control</button>
  <button id="t-games" onclick="tab('games')">Games</button>
  <button id="t-wiki" onclick="tab('wiki')">Wiki</button>
</nav>
<main>
 <section id="p-control">
  <div class="grid">
   <div class="card"><h3>Connection</h3>
     <div class="row"><input id="ip" placeholder="192.168.137.214 (blank = auto-find)">
       <button class="act" onclick="connect()">connect</button></div>
     <div id="statusBox"></div>
   </div>
   <div class="card"><h3>Speech</h3>
     <textarea id="sayText" rows="2" placeholder="Type something for Pepper to say"></textarea>
     <div class="row"><select id="sayLang"><option>English</option><option>German</option><option>Chinese</option></select>
       <button class="act" onclick="say()">say</button></div>
   </div>
   <div class="card"><h3>Volume</h3>
     <input id="vol" type="range" min="0" max="100" value="60" oninput="volLbl.textContent=this.value">
     <div class="row"><span id="volLbl" class="mut">60</span><button class="act" onclick="setVol()">set volume</button></div>
   </div>
   <div class="card"><h3>Posture & motion</h3>
     <button class="act" onclick="posture('wake')">wake up</button>
     <button class="act ghost" onclick="posture('stand')">stand</button>
     <button class="act ghost" onclick="posture('crouch')">crouch</button>
     <button class="act warn" onclick="posture('rest')">rest</button>
     <div class="mut">Pepper has a wheeled base, so it cannot sit; crouch is the low resting pose.</div>
   </div>
   <div class="card"><h3>Autonomous Life</h3>
     <button class="act ghost" onclick="life('interactive')">interactive</button>
     <button class="act ghost" onclick="life('solitary')">solitary</button>
     <button class="act warn" onclick="life('disabled')">disabled</button>
   </div>
   <div class="card"><h3>Eyes (LED)</h3>
     <button class="act" style="background:#2d9cdb" onclick="leds('#0000ff')">blue</button>
     <button class="act" style="background:#27ae60" onclick="leds('#00a000')">green</button>
     <button class="act" style="background:#e05a4d" onclick="leds('#ff0000')">red</button>
     <button class="act ghost" onclick="leds('off')">off</button>
   </div>
   <div class="card"><h3>Tablet</h3>
     <input id="tabTitle" placeholder="title (small)">
     <input id="tabText" placeholder="text (big)">
     <div class="row"><button class="act" onclick="tablet('show')">show on tablet</button>
       <button class="act ghost" onclick="tablet('reset')">hide</button></div>
     <button class="act warn" onclick="tabletService()">fix tablet service</button>
     <div class="mut">If the tablet stays on its home screen, ALTabletService dropped - tap the tablet / relaunch its app or reboot.</div>
   </div>
  </div>
 </section>

 <section id="p-games" class="hidden">
  <div class="grid">
   <div class="card" style="grid-column:1/-1;border-color:#ffd23f"><h3>📟 Tablet menu (kiosk)</h3>
     <p class="mut">Show a game menu on Pepper's tablet so visitors start games by tapping. Runs until you stop it.</p>
     <div class="row"><div><label>Rounds per game</label><input id="kRounds" type="number" value="3"></div>
       <div><label>Guess category</label><select id="kCat"><option value="mixed">mixed</option><option value="animals">animals</option><option value="countries">countries</option><option value="cities">cities</option></select></div>
       <div style="display:flex;align-items:flex-end;gap:8px"><button class="act" onclick="kioskStart()">▶ start menu</button>
         <button class="act err" onclick="kioskStop()">■ stop menu</button></div></div>
   </div>
   <div class="card"><h3>🦜 Parrot</h3>
     <p class="mut">Say something - Pepper shows it and repeats it.</p>
     <div class="row"><div><label>Language</label><select id="pLang"><option value="en">English</option><option value="de">German</option></select></div>
       <div><label>Rec sec</label><input id="pDur" type="number" value="6"></div>
       <div><label>Rounds</label><input id="pRounds" type="number" value="0"></div></div>
     <button class="act" onclick="gameStart('parrot')">▶ start parrot</button>
   </div>
   <div class="card"><h3>💬 Ask Pepper (Q&amp;A)</h3>
     <p class="mut">Ask a question - Pepper answers with the AI model.</p>
     <div class="row"><div><label>Language</label><select id="qLang"><option value="en">English</option><option value="de">German</option></select></div>
       <div><label>Rec sec</label><input id="qDur" type="number" value="6"></div>
       <div><label>Rounds</label><input id="qRounds" type="number" value="0"></div></div>
     <button class="act" onclick="gameStart('qa')">▶ start Q&amp;A</button>
   </div>
   <div class="card"><h3>🧩 Guess What</h3>
     <p class="mut">Pepper describes an animal / country / city; kids guess. Tablet has a hint button.</p>
     <div class="row"><div><label>Language</label><select id="uLang"><option value="en">English</option><option value="de">German</option></select></div>
       <div><label>Category</label><select id="uCat"><option value="mixed">mixed</option><option value="animals">animals</option><option value="countries">countries</option><option value="cities">cities</option></select></div>
       <div><label>Rounds</label><input id="uRounds" type="number" value="0"></div></div>
     <button class="act" onclick="gameStart('guess')">▶ start guess</button>
   </div>
   <div class="card"><h3>🔁 Echo</h3>
     <p class="mut">Listen and repeat, once or looping.</p>
     <div class="row"><div><label>Language</label><select id="eLang"><option value="en">English</option><option value="de">German</option></select></div>
       <div><label>Rec sec</label><input id="eDur" type="number" value="6"></div>
       <div><label>Mode</label><select id="eLoop"><option value="0">loop</option><option value="1">once</option></select></div></div>
     <button class="act" onclick="gameStart('echo')">▶ start echo</button>
   </div>
   <div class="card" style="grid-column:1/-1"><h3>Running game</h3>
     <button class="act err" onclick="gameStop()">■ stop current game</button>
     <pre id="gameLog">(no game running)</pre>
   </div>
  </div>
 </section>

 <section id="p-wiki" class="hidden"><div class="card wiki">__WIKI__</div></section>
</main>
<footer style="text-align:center;padding:16px 20px;color:var(--mut);font-size:12px;border-top:1px solid #22303c">
  Pepper Admin Panel
</footer>
<script>
var IP_KNOWN=null;
function j(url,body){return fetch(url,{method:body?'POST':'GET',headers:{'Content-Type':'application/json'},
  body:body?JSON.stringify(body):null}).then(function(r){return r.json();});}
function tab(n){['control','games','wiki'].forEach(function(x){
  document.getElementById('p-'+x).classList.toggle('hidden',x!==n);
  document.getElementById('t-'+x).classList.toggle('active',x===n);});
  if(n==='games')pollLog();}
function refresh(){j('/api/status').then(function(s){
  var c=document.getElementById('connPill');c.textContent=s.connected?'connected':'disconnected';
  c.className='pill '+(s.connected?'on':'off');
  document.getElementById('ipPill').textContent='ip: '+(s.ip||'?');IP_KNOWN=s.ip;
  document.getElementById('battPill').textContent='batt: '+(s.battery!=null?s.battery+'%':'?');
  var t=document.getElementById('tabPill');t.textContent='tablet: '+(s.tablet?'ok':'down');
  t.className='pill '+(s.tablet?'on':'off');
  document.getElementById('gamePill').textContent=s.kiosk?'tablet menu: ON':('game: '+(s.game||'idle'));
  var b=document.getElementById('statusBox');b.innerHTML='';
  [['posture',s.posture],['autonomous life',s.life],['volume',s.volume],['error',s.error]].forEach(function(kv){
    if(kv[1]!=null){var d=document.createElement('div');d.className='kv';d.innerHTML='<b>'+kv[0]+'</b><span>'+kv[1]+'</span>';b.appendChild(d);}});
});}
function connect(){j('/api/connect',{ip:document.getElementById('ip').value.trim()}).then(refresh);}
function say(){j('/api/say',{text:document.getElementById('sayText').value,lang:document.getElementById('sayLang').value});}
function setVol(){j('/api/volume',{value:parseInt(document.getElementById('vol').value)});}
function posture(a){j('/api/posture',{action:a}).then(refresh);}
function life(s){j('/api/life',{state:s}).then(refresh);}
function leds(c){j('/api/leds',{color:c});}
function tablet(a){j('/api/tablet',{action:a,title:document.getElementById('tabTitle').value,text:document.getElementById('tabText').value});}
function tabletService(){j('/api/tablet/service',{}).then(function(r){alert(r.ok?'ALTabletService is back':'still down. '+(r.note||''));refresh();});}
function gameStart(name){var body={game:name};
  if(name==='guess'){body.lang=document.getElementById('uLang').value;
    body.category=document.getElementById('uCat').value;
    body.rounds=parseInt(document.getElementById('uRounds').value);body.duration=5;}
  else{var m={parrot:['pLang','pDur','pRounds'],qa:['qLang','qDur','qRounds'],echo:['eLang','eDur','eLoop']};
    var ids=m[name];body.lang=document.getElementById(ids[0]).value;
    body.duration=parseInt(document.getElementById(ids[1]).value);
    body.rounds=parseInt(document.getElementById(ids[2]).value);}
  j('/api/game/start',body).then(function(r){if(!r.ok)alert(r.msg||r.error);refresh();pollLog();});}
function kioskStart(){j('/api/kiosk/start',{rounds:parseInt(document.getElementById('kRounds').value),
  category:document.getElementById('kCat').value}).then(function(r){if(!r.ok)alert(r.msg||r.error);refresh();});}
function kioskStop(){j('/api/kiosk/stop',{}).then(function(r){if(!r.ok)alert(r.msg||r.error);refresh();});}
function gameStop(){j('/api/game/stop',{}).then(function(r){refresh();pollLog();});}
function pollLog(){j('/api/game/log').then(function(r){
  document.getElementById('gameLog').textContent=r.log||'(no game running)';});}
setInterval(function(){refresh();if(!document.getElementById('p-games').classList.contains('hidden'))pollLog();},4000);
refresh();
</script>
</body></html>"""

WIKI_HTML = u"""
<h2>Quick facts</h2>
<table>
<tr><td>Robot IP (Mobile Hotspot)</td><td><code>192.168.137.214</code> &nbsp; subnet <code>192.168.137.x</code></td></tr>
<tr><td>naoqi port</td><td><code>9559</code></td></tr>
<tr><td>SSH login</td><td><code>nao</code> / env var <code>PEPPER_SSH_PASSWORD</code> (port 22, ask a lab member for the value)</td></tr>
<tr><td>STT / chat server</td><td><code>https://hctlsrvb.edu.sot.tum.de</code> (Open WebUI at TUM)</td></tr>
<tr><td>Token</td><td>env var <code>HCTL_TOKEN</code> (Bearer). Set before starting this panel.</td></tr>
<tr><td>Tablet web root</td><td><code>/home/nao/.local/share/PackageManager/apps/robot-page/html/</code> &rarr; served at <code>http://198.18.0.1/</code></td></tr>
<tr><td>Python env</td><td>conda <code>pepper</code> (Python 2.7, 32-bit) + pynaoqi on <code>PYTHONPATH</code></td></tr>
</table>

<h2>How to run the games</h2>
<pre>conda activate pepper
set HCTL_TOKEN=eyJhbGci...
set PYTHONPATH=...\\sdk\\pynaoqi-win32\\...\\lib

python pepper_qa_game.py     -ip 192.168.137.214 -l en        # Ask Pepper
python pepper_parrot_game.py -ip 192.168.137.214 -l en        # Parrot / repeat
python pepper_echo.py        -ip 192.168.137.214 -d 6 --loop  # Echo</pre>
<p class="mut">Or just use the <b>Games</b> tab above - it launches these with the right environment.</p>

<h2>Microphone &amp; speech-to-text</h2>
<ul>
<li>The games record <b>all four microphones</b> and send the <b>loudest single channel</b> to Whisper.
    Recording one channel directly comes out much quieter; averaging the spaced mics smears speech.</li>
<li>The STT server returns <code>{"text": ...}</code> only - <b>no confidence / no_speech_prob</b>.
    For silence it returns an empty string, which the games treat as &ldquo;not heard&rdquo;.</li>
<li><b>Force the language</b> (<code>-l en</code> / <code>-l de</code>). <code>auto</code> mis-detects even clean English as German.</li>
<li>Speak <b>clearly and close</b>, and wait for the green &ldquo;Listening&rdquo; screen / blue eyes before talking.
    Recorded speech is only slightly above the noise floor, so an amplitude threshold cannot tell speech from silence.</li>
<li>Files bigger than ~10&nbsp;MB get HTTP 413 from the STT server.</li>
</ul>

<h2>Tablet troubleshooting</h2>
<ul>
<li><b>Tablet stuck on its home screen?</b> <code>ALTabletService</code> has dropped out of naoqi. It is provided by the
    tablet&rsquo;s own Android app; when that crashes the service disappears (all other services stay).</li>
<li>The header shows <span class="pill off">tablet: down</span> when this happens.</li>
<li>Fix: tap the tablet / relaunch its app, or reboot the robot. Then it re-registers - <b>no code change needed</b>.
    The &ldquo;fix tablet service&rdquo; button tries a best-effort restart but usually a tap/reboot is required.</li>
<li>The web-display pipeline itself is correct: HTML is uploaded to the robot-page web root and shown with
    <code>ALTabletService.showWebview('http://198.18.0.1/parrot.html')</code>.</li>
</ul>

<h2>The Q&amp;A model</h2>
<ul>
<li>Chat endpoint <code>/openai/chat/completions</code>, model <code>openai/gpt-oss-120b</code>.</li>
<li>It is a reasoning model: the thinking goes in a separate <code>reasoning</code> field, the answer in
    <code>content</code>. The game also strips any leaked <code>&lt;think&gt;...&lt;/think&gt;</code> so Pepper never
    reads its own reasoning aloud.</li>
</ul>

<h2>Common ALProxies (for reference)</h2>
<table>
<tr><td>ALTextToSpeech</td><td><code>.say(text)</code>, <code>.setLanguage('English')</code></td></tr>
<tr><td>ALAudioDevice</td><td><code>.setOutputVolume(0-100)</code>, <code>.getFrontMicEnergy()</code></td></tr>
<tr><td>ALRobotPosture</td><td><code>.goToPosture('Stand'|'Sit'|'Crouch', 0.6)</code></td></tr>
<tr><td>ALMotion</td><td><code>.wakeUp()</code>, <code>.rest()</code></td></tr>
<tr><td>ALAutonomousLife</td><td><code>.setState('interactive'|'solitary'|'disabled')</code></td></tr>
<tr><td>ALBattery</td><td><code>.getBatteryCharge()</code></td></tr>
<tr><td>ALLeds</td><td><code>.fadeRGB('FaceLeds', 0x0000FF, 0.2)</code>, <code>.reset('FaceLeds')</code></td></tr>
</table>
<p class="mut">Docs: SoftBank/Aldebaran NAOqi 2.5 API - http://doc.aldebaran.com/2-5/naoqi/index.html</p>

"""


def main():
    parser = argparse.ArgumentParser(description='Pepper web admin panel.')
    parser.add_argument('--port', type=int, default=8080)
    parser.add_argument('--ip', type=str, default=None,
                        help='Pepper IP (auto-find if omitted)')
    args = parser.parse_args()

    if args.ip:
        STATE['ip'] = args.ip
    if not os.environ.get(TOKEN_ENV_VAR):
        print('WARNING: %s is not set -- the Q&A game will not be able to '
              'reach the AI server.' % TOKEN_ENV_VAR)
    if not SSH_PW:
        print('WARNING: %s is not set -- tablet uploads and SSH-based control '
              'will not work.' % PEPPER_PW_ENV_VAR)

    global PAGE
    PAGE = PAGE.replace(u'__WIKI__', WIKI_HTML)

    srv = ThreadingServer(('0.0.0.0', args.port), Handler)
    print('Pepper Admin running at http://127.0.0.1:%d  (Ctrl+C to stop)' % args.port)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print('\nshutting down')


if __name__ == '__main__':
    main()

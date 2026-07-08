# -*- coding: utf-8 -*-
"""
pepper_kiosk.py  --  tablet game launcher for Pepper.

Shows a touch menu on Pepper's tablet (Parrot / Ask Pepper / Guess What / Echo
and an English/Deutsch toggle). When a game is tapped, the kiosk launches it,
waits for it to finish its rounds, then shows the menu again -- so visitors can
pick and play games straight from the tablet, without touching the PC.

The tablet page talks to naoqi through qimessaging.js (it writes the choice into
ALMemory); this process polls ALMemory and starts the matching game.

Runs in the Python 2.7 'pepper' conda env. HCTL_TOKEN must be set for the games.

    python pepper_kiosk.py                 # 3 rounds per game
    python pepper_kiosk.py -r 5 -c animals
"""

import os
import sys
import time
import argparse
import subprocess

import qi

from pepper_echo import TOKEN_ENV_VAR, PEPPER_PW_ENV_VAR
from pepper_parrot_game import find_pepper, NAOQI_PORT, HOTSPOT_SUBNET

HERE = os.path.dirname(os.path.abspath(__file__))
REMOTE_HTML = '/home/nao/.local/share/PackageManager/apps/robot-page/html/menu.html'
CHOICE_KEY = 'Pepper/Menu/Choice'
LANG_KEY = 'Pepper/Menu/Lang'
LOCK_PATH = os.path.join(HERE, 'kiosk.lock')


def _pid_alive(pid):
    """Windows-only liveness check (no extra dependencies needed)."""
    try:
        import ctypes
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        handle = ctypes.windll.kernel32.OpenProcess(
            PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if handle:
            ctypes.windll.kernel32.CloseHandle(handle)
            return True
        return False
    except Exception:
        return False


def _another_kiosk_running():
    """True if kiosk.lock names a PID that is still alive.

    Both the admin panel's "start menu" button and a game's own EXIT button
    (see relaunch_kiosk_menu() in pepper_qa_game.py / pepper_guess_game.py)
    launch this same script -- without this guard, two kiosk processes could
    end up fighting over the same tablet and ALMemory keys at once.
    """
    if not os.path.exists(LOCK_PATH):
        return False
    try:
        with open(LOCK_PATH) as f:
            pid = int(f.read().strip())
    except Exception:
        return False
    return _pid_alive(pid)

GAME_SCRIPTS = {
    'parrot': 'pepper_parrot_game.py',
    'qa': 'pepper_qa_game.py',
    'guess': 'pepper_guess_game.py',
    'echo': 'pepper_echo.py',
}

MENU_HTML = u"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
html,body{height:100%;margin:0}
body{background:#12203a;color:#fff;font-family:Arial,Helvetica,sans-serif;text-align:center}
.wrap{min-height:100%;display:flex;flex-direction:column;justify-content:center;padding:3%}
h1{font-size:6vw;margin:1vh 0 3vh}
.lang{margin-bottom:3vh}
.lang button{font-size:4vw;padding:1.5vh 6vw;margin:0 1vw;border:none;border-radius:2vw;background:#31456b;color:#fff}
.lang button.on{background:#ffd23f;color:#12203a;font-weight:bold}
.grid{display:grid;grid-template-columns:1fr 1fr;gap:3vw}
.grid button{font-size:6vw;font-weight:bold;padding:7vh 2vw;border:none;border-radius:3vw;color:#12203a}
.parrot{background:#5ec5c9}.qa{background:#8ee06b}.guess{background:#ffd23f}.echo{background:#e29bff}
</style><script src="qimessaging.js"></script></head><body>
<div class="wrap">
 <h1>Choose a game</h1>
 <div class="lang">
   <button id="en" class="on" onclick="setLang('en')">English</button>
   <button id="de" onclick="setLang('de')">Deutsch</button>
 </div>
 <div class="grid">
   <button class="parrot" onclick="pick('parrot')">Parrot</button>
   <button class="qa" onclick="pick('qa')">Ask Pepper</button>
   <button class="guess" onclick="pick('guess')">Guess What</button>
   <button class="echo" onclick="pick('echo')">Echo</button>
 </div>
</div>
<script>
var M=null;
function conn(){try{QiSession(function(s){var p=s.service("ALMemory");
var set=function(m){M=m;};if(p.then)p.then(set);else if(p.done)p.done(set);},
function(){setTimeout(conn,2000);});}catch(e){setTimeout(conn,2000);}}
conn();
function setLang(l){if(M)M.insertData("__LANG__",l);
 document.getElementById('en').className=(l=='en'?'on':'');
 document.getElementById('de').className=(l=='de'?'on':'');}
function pick(g){if(M){M.insertData("__CHOICE__",g);}
 document.querySelector('.wrap').innerHTML='<h1>Starting '+g+' ...</h1>';}
</script></body></html>""".replace('__LANG__', LANG_KEY).replace('__CHOICE__', CHOICE_KEY)


def main():
    parser = argparse.ArgumentParser(description='Pepper tablet game launcher.')
    parser.add_argument('-ip', '--ip', type=str, default='192.168.137.214')
    parser.add_argument('-port', '--port', type=int, default=NAOQI_PORT)
    parser.add_argument('-ssh_port', '--ssh_port', type=int, default=22)
    parser.add_argument('-u', '--username', type=str, default='nao')
    parser.add_argument('-p', '--password', type=str,
                        default=os.environ.get(PEPPER_PW_ENV_VAR),
                        help='Pepper ssh password (or set %s)' % PEPPER_PW_ENV_VAR)
    parser.add_argument('-r', '--rounds', type=int, default=3,
                        help='rounds to play per tapped game before returning to the menu')
    parser.add_argument('-d', '--duration', type=int, default=6,
                        help='seconds to record each turn')
    parser.add_argument('-c', '--category', type=str, default='mixed',
                        choices=['animals', 'countries', 'cities', 'mixed'],
                        help='category for the Guess game')
    args = parser.parse_args()

    if not os.environ.get(TOKEN_ENV_VAR):
        print('WARNING: %s not set -- the games will not reach the AI server.'
              % TOKEN_ENV_VAR)
    if not args.password:
        print('ERROR: set the %s environment variable, or pass -p <password>.'
              % PEPPER_PW_ENV_VAR)
        sys.exit(1)

    if _another_kiosk_running():
        print('a tablet menu (kiosk) is already running -- not starting a second one')
        return
    with open(LOCK_PATH, 'w') as f:
        f.write(str(os.getpid()))

    ip = find_pepper(args.ip)
    if not ip:
        print('could not find Pepper on the %s.x network.' % HOTSPOT_SUBNET)
        try:
            os.remove(LOCK_PATH)
        except Exception:
            pass
        sys.exit(1)

    session = qi.Session()
    session.connect('tcp://%s:%d' % (ip, args.port))
    print('connected to Pepper at %s:%d' % (ip, args.port))
    tablet = session.service('ALTabletService')
    memory = session.service('ALMemory')
    memory.insertData(CHOICE_KEY, '')
    memory.insertData(LANG_KEY, 'en')

    import paramiko
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    ssh.connect(ip, args.ssh_port, args.username, args.password, timeout=10)
    sftp = ssh.open_sftp()
    f = sftp.open(REMOTE_HTML, 'w')
    f.write(MENU_HTML.encode('utf-8'))
    f.close()

    def show_menu():
        url = 'http://198.18.0.1/menu.html?%d' % int(time.time())
        try:
            tablet.showWebview(url)
        except Exception:
            tablet.loadUrl(url)
            tablet.showWebview()

    def launch(game, lang):
        cmd = [sys.executable, os.path.join(HERE, GAME_SCRIPTS[game]),
               '-ip', ip, '-l', lang, '-d', str(args.duration)]
        if game in ('parrot', 'qa', 'guess'):
            cmd += ['-r', str(args.rounds)]
        if game == 'guess':
            cmd += ['-c', args.category]
        if game in ('qa', 'guess'):
            # tell the game it's running under the kiosk, so tapping its own
            # EXIT button doesn't spawn a second, redundant kiosk process --
            # this kiosk already re-shows the menu once the game exits.
            cmd += ['--kiosk']
        print('launching %s (%s, %d rounds) ...' % (game, lang, args.rounds))
        env = dict(os.environ)
        env['PYTHONUNBUFFERED'] = '1'
        subprocess.call(cmd, cwd=HERE, env=env)
        print('%s finished, back to menu' % game)

    print('kiosk running -- tap a game on the tablet (Ctrl+C to stop)')
    try:
        while True:
            memory.insertData(CHOICE_KEY, '')
            show_menu()
            # wait for a tap
            choice = ''
            while not choice:
                try:
                    choice = memory.getData(CHOICE_KEY)
                except Exception:
                    choice = ''
                if choice in GAME_SCRIPTS:
                    break
                choice = ''
                time.sleep(0.4)
            memory.insertData(CHOICE_KEY, '')
            try:
                lang = memory.getData(LANG_KEY) or 'en'
            except Exception:
                lang = 'en'
            if lang not in ('en', 'de'):
                lang = 'en'
            launch(choice, lang)
    except KeyboardInterrupt:
        print('\nstopping kiosk ...')
    finally:
        try:
            tablet.hideWebview()
        except Exception:
            pass
        try:
            sftp.close(); ssh.close()
        except Exception:
            pass
        try:
            os.remove(LOCK_PATH)
        except Exception:
            pass


if __name__ == '__main__':
    main()

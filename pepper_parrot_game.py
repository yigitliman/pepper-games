# -*- coding: utf-8 -*-
"""
pepper_parrot_game.py  --  "Papagei / Parrot" game for Pepper

A simple, fun listen-and-repeat game:
    1. Pepper invites the player to say something.
    2. It records from its microphone and transcribes the speech (HCTL server).
    3. It shows the recognised sentence on its tablet (if the tablet service
       is available) and repeats it out loud like a parrot.
    4. Next round -- until you stop it with Ctrl+C.

Runs in the Python 2.7 (32-bit) 'pepper' conda environment.
The speech-to-text token is read from the HCTL_TOKEN environment variable.

Examples:
    python pepper_parrot_game.py                 # auto-find Pepper, English
    python pepper_parrot_game.py --lang de        # German game
    python pepper_parrot_game.py -ip 192.168.137.214 -d 6
"""

import os
import sys
import time
import socket
import argparse

import qi

# reuse the proven building blocks from the echo pipeline
from pepper_echo import (record_audio, download_audio, transcribe,
                         STT_BASE_URL, BASE_URL_ENV_VAR, TOKEN_ENV_VAR,
                         PEPPER_PW_ENV_VAR, LANGUAGE_DICT)

HOTSPOT_SUBNET = '192.168.137'   # Windows Mobile Hotspot range
NAOQI_PORT = 9559

# shared ALMemory keys for the tablet's STOP / EXIT buttons, used by every
# game (parrot, qa, guess, echo) so a player can always end the session or
# return to the kiosk menu from the tablet, not just via Ctrl+C on the PC.
STOP_KEY = 'Pepper/Game/StopReq'
EXIT_KEY = 'Pepper/Game/ExitReq'

# localised game phrases: (tts language name, prompt, repeat lead-in,
#                          nothing-heard, welcome, goodbye)
PHRASES = {
    'en': {
        'tts': 'English',
        'welcome': "Let's play the parrot game! Say something and I will repeat it.",
        'prompt': 'Your turn. Say something after the beep!',
        'listening': u'Listening...',
        'stop_btn': u'STOP',
        'exit_btn': u'EXIT',
        'lead': 'You said:',
        'nothing': "Sorry, I could not hear you. Could you please say it again?",
        'hint': 'Please wait for the beep, then speak clearly and close to me.',
        'goodbye': 'Good game! See you next time.',
    },
    'de': {
        'tts': 'German',
        'welcome': 'Lass uns das Papageienspiel spielen! Sag etwas und ich wiederhole es.',
        'prompt': 'Du bist dran. Sag etwas nach dem Ton!',
        'listening': u'Ich hoere zu...',
        'stop_btn': u'STOPP',
        'exit_btn': u'ENDE',
        'lead': 'Du hast gesagt:',
        'nothing': 'Entschuldigung, ich konnte dich nicht hoeren. Kannst du es bitte nochmal sagen?',
        'hint': 'Bitte warte auf den Ton und sprich dann deutlich und nah bei mir.',
        'goodbye': 'Schoenes Spiel! Bis zum naechsten Mal.',
    },
}


def find_pepper(preferred_ip=None):
    """Return an IP on the hotspot subnet whose naoqi port is open.

    Tries preferred_ip first, then scans the subnet so the game keeps working
    even if Pepper got a new IP after a reboot.
    """
    def naoqi_open(ip):
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(0.25)
        try:
            return s.connect_ex((ip, NAOQI_PORT)) == 0
        except socket.error:
            return False
        finally:
            s.close()

    if preferred_ip and naoqi_open(preferred_ip):
        return preferred_ip

    print('scanning %s.0/24 for Pepper (naoqi on %d) ...' % (HOTSPOT_SUBNET, NAOQI_PORT))
    for host in range(2, 255):
        ip = '%s.%d' % (HOTSPOT_SUBNET, host)
        if naoqi_open(ip):
            print('found Pepper at %s' % ip)
            return ip
    return None


def _esc(text):
    """Escape text so speech/answers with < > & do not break the HTML page."""
    if text is None:
        return u''
    if not isinstance(text, unicode):
        text = text.decode('utf-8', 'replace')
    return (text.replace(u'&', u'&amp;')
                .replace(u'<', u'&lt;')
                .replace(u'>', u'&gt;'))


def read_mem_int(memory, key):
    """Read an ALMemory int key, defaulting to 0 if unset/unavailable.

    Used to poll the tablet's STOP/EXIT (and HINT/SKIP in the guess game)
    buttons, which write a millisecond timestamp into ALMemory when tapped.
    """
    if not memory:
        return 0
    try:
        return int(memory.getData(key))
    except Exception:
        return 0


_CONTROLS_JS = (
    u'<script src="qimessaging.js"></script>'
    u'<script>var M=null;'
    u'function conn(){try{QiSession(function(s){var p=s.service("ALMemory");'
    u'var set=function(m){M=m;};if(p.then)p.then(set);else if(p.done)p.done(set);},'
    u'function(){setTimeout(conn,2000);});}catch(e){setTimeout(conn,2000);}}conn();'
    u'function tap(id,key){if(M){M.insertData(key,Date.now());}'
    u'var b=document.getElementById(id);var t=b.textContent;b.textContent="...";'
    u'setTimeout(function(){b.textContent=t;},1200);}</script>'
)


def show_on_tablet(session, sftp_info, title, text, bg='#0f4c81',
                   show_controls=True, stop_label=u'STOP', exit_label=u'EXIT'):
    """Best-effort: show 'text' on Pepper's tablet as a web page.

    'bg' sets the background colour so callers can signal state (e.g. green
    while listening, amber while thinking). Returns True if it managed to
    display, False otherwise (e.g. the tablet service is not running -- the
    game then simply continues without it).

    Unless show_controls=False, a small STOP/EXIT button bar is overlaid at
    the bottom of the screen (writes to ALMemory[STOP_KEY]/[EXIT_KEY] via
    qimessaging.js) so the player can end the game or return to the kiosk
    menu from the tablet at any time; the game loop must poll those keys
    with read_mem_int().
    """
    try:
        tablet = session.service('ALTabletService')
    except Exception:
        return False

    controls_html = u''
    if show_controls:
        controls_html = (
            u'<div class="ctl">'
            u'<button id="stb" onclick="tap(\'stb\',\'%s\')">%s</button>'
            u'<button id="exb" onclick="tap(\'exb\',\'%s\')">%s</button>'
            u'</div>' + _CONTROLS_JS
        ) % (STOP_KEY, _esc(stop_label), EXIT_KEY, _esc(exit_label))

    # build a small HTML page and push it to the robot's local web root,
    # which the tablet serves at http://198.18.0.1/
    html = (
        u'<!doctype html><html><head><meta charset="utf-8">'
        u'<meta name="viewport" content="width=device-width,initial-scale=1">'
        u'<style>html,body{height:100%%;margin:0}'
        u'body{display:flex;flex-direction:column;justify-content:center;'
        u'align-items:center;background:%s;color:#fff;'
        u'font-family:Arial,Helvetica,sans-serif;text-align:center;padding:6%%}'
        u'.title{font-size:5vw;opacity:.85;margin-bottom:4vh}'
        u'.text{font-size:8vw;font-weight:bold;line-height:1.25}'
        u'.ctl{position:fixed;left:0;right:0;bottom:3vh;display:flex;'
        u'justify-content:center;gap:4vw}'
        u'.ctl button{font-size:3.3vw;font-weight:bold;border:none;'
        u'border-radius:2vw;padding:1.5vh 5vw;color:#fff}'
        u'.ctl button:active{transform:translateY(1vh)}'
        u'#stb{background:#c0392b}#exb{background:#555b6e}'
        u'</style></head><body>'
        u'<div class="title">%s</div><div class="text">%s</div>%s'
        u'</body></html>'
    ) % (bg, _esc(title), _esc(text), controls_html)

    remote_html = '/home/nao/.local/share/PackageManager/apps/robot-page/html/parrot.html'
    try:
        ssh, sftp = sftp_info
        f = sftp.open(remote_html, 'w')
        f.write(html.encode('utf-8'))
        f.close()
        # cache-busting query so the tablet reloads the new content each round
        url = 'http://198.18.0.1/parrot.html?%d' % int(time.time())
        try:
            tablet.showWebview(url)
        except Exception:
            tablet.loadUrl(url)
            tablet.showWebview()
        return True
    except Exception as err:
        print('tablet display skipped (%s)' % err)
        return False


def show_listening(session, sftp_info, msg, stop_label=u'STOP', exit_label=u'EXIT'):
    """Green 'listening' screen so the player knows to speak now."""
    return show_on_tablet(session, sftp_info, u'', msg, bg='#1b7f4b',
                          stop_label=stop_label, exit_label=exit_label)


def show_thinking(session, sftp_info, msg, stop_label=u'STOP', exit_label=u'EXIT'):
    """Amber 'thinking' screen while an answer is being fetched."""
    return show_on_tablet(session, sftp_info, u'', msg, bg='#b8860b',
                          stop_label=stop_label, exit_label=exit_label)


def set_eyes(session, rgb):
    """Best-effort eye-LED colour to signal state; ignored if unavailable."""
    try:
        leds = session.service('ALLeds')
        if rgb is None:
            leds.reset('FaceLeds')
        else:
            leds.fadeRGB('FaceLeds', rgb, 0.2)
    except Exception:
        pass


def main():
    parser = argparse.ArgumentParser(description='Pepper parrot (listen & repeat) game.')
    parser.add_argument('-ip', '--ip', type=str, default='192.168.137.214',
                        help='IP of Pepper (auto-detected if this one is down)')
    parser.add_argument('-port', '--port', type=int, default=NAOQI_PORT)
    parser.add_argument('-ssh_port', '--ssh_port', type=int, default=22)
    parser.add_argument('-u', '--username', type=str, default='nao')
    parser.add_argument('-p', '--password', type=str,
                        default=os.environ.get(PEPPER_PW_ENV_VAR),
                        help='Pepper ssh password (or set %s)' % PEPPER_PW_ENV_VAR)
    parser.add_argument('-d', '--duration', type=int, default=6,
                        help='seconds to record each round')
    parser.add_argument('-l', '--lang', type=str, default='en',
                        choices=['en', 'de'],
                        help="game language: en or de")
    parser.add_argument('-r', '--rounds', type=int, default=0,
                        help='number of rounds to play (0 = endless until Ctrl+C)')
    parser.add_argument('-ra', '--remote_audio_path', type=str,
                        default='/home/nao/recording.wav')
    parser.add_argument('-la', '--local_audio_path', type=str,
                        default='./recording.wav')
    args = parser.parse_args()

    if not STT_BASE_URL:
        print('ERROR: set the %s environment variable first.' % BASE_URL_ENV_VAR)
        print('  Windows:  set %s=https://your-open-webui-server'
              % BASE_URL_ENV_VAR)
        sys.exit(1)
    token = os.environ.get(TOKEN_ENV_VAR)
    if not token:
        print('ERROR: set the %s environment variable first.' % TOKEN_ENV_VAR)
        sys.exit(1)
    if not args.password:
        print('ERROR: set the %s environment variable, or pass -p <password>.'
              % PEPPER_PW_ENV_VAR)
        sys.exit(1)

    ph = PHRASES[args.lang]

    ip = find_pepper(args.ip)
    if not ip:
        print('could not find Pepper on the %s.x network.' % HOTSPOT_SUBNET)
        print('is Pepper on and connected to the "Pepper" hotspot?')
        sys.exit(1)

    session = qi.Session()
    try:
        session.connect('tcp://%s:%d' % (ip, args.port))
    except RuntimeError:
        print('cannot connect to Pepper at %s:%d' % (ip, args.port))
        sys.exit(1)
    print('connected to Pepper at %s:%d' % (ip, args.port))

    tts = session.service('ALTextToSpeech')
    tts.setLanguage(ph['tts'])

    # one shared ssh/sftp connection for tablet uploads (built lazily)
    sftp_info = None
    try:
        import paramiko
        ssh = paramiko.SSHClient()
        ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        ssh.connect(ip, args.ssh_port, args.username, args.password, timeout=10)
        sftp_info = (ssh, ssh.open_sftp())
    except Exception as err:
        print('note: tablet uploads disabled (%s)' % err)

    tts.say(ph['welcome'])

    rounds = 0
    misses = 0
    try:
        while args.rounds == 0 or rounds < args.rounds:
            rounds += 1
            print('\n=== round %d ===' % rounds)
            # tell the player -- on the tablet and with blue eyes -- to speak now
            # (show_controls=False: this game's loop does not poll STOP/EXIT
            # yet, so we don't show tappable buttons that would silently do
            # nothing -- avoid that dead-button confusion)
            if sftp_info:
                show_on_tablet(session, sftp_info, u'', ph['listening'],
                               bg='#1b7f4b', show_controls=False)
            set_eyes(session, 0x0000FF)
            # the prompt itself is the "speak now" cue
            record_audio(session, args.remote_audio_path, args.duration, cue=ph['prompt'])
            set_eyes(session, None)
            download_audio(ip, args.ssh_port, args.username, args.password,
                           args.remote_audio_path, args.local_audio_path)
            text = transcribe(args.local_audio_path, token, args.lang)

            # the STT server returns "" for silence; treat empty or trivially
            # short results (punctuation-only junk) as "not heard" instead of
            # parroting nonsense back.
            alnum = [c for c in text if c.isalnum()] if text else []
            if len(alnum) < 2:
                misses += 1
                tts.say(ph['nothing'])
                if misses >= 2:                 # struggling -> give a hint once
                    tts.say(ph['hint'])
                    misses = 0
                rounds -= 1
                continue

            misses = 0
            if sftp_info:
                show_on_tablet(session, sftp_info, ph['lead'], text, show_controls=False)
            tts.say(text.encode('utf-8'))
            time.sleep(1)
        # finished the requested number of rounds
        try:
            tts.say(ph['goodbye'])
        except Exception:
            pass
    except KeyboardInterrupt:
        print('\nstopping game ...')
        try:
            tts.say(ph['goodbye'])
        except Exception:
            pass
    finally:
        set_eyes(session, None)
        if sftp_info:
            try:
                sftp_info[1].close(); sftp_info[0].close()
            except Exception:
                pass
    print('total rounds played: %d' % rounds)


if __name__ == '__main__':
    main()

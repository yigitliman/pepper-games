# -*- coding: utf-8 -*-
"""
pepper_qa_game.py  --  "Ask Pepper" question & answer game

The player asks Pepper a question out loud; Pepper thinks with an AI model
(the HCTL server) and answers back with its own voice:

    1. Pepper invites the player to ask a question.
    2. It records the question and transcribes it (HCTL speech-to-text API).
    3. It sends the question to the AI chat model (HCTL /v1/chat/completions).
    4. Pepper speaks the answer out loud.
    5. Next question -- until you stop it with Ctrl+C.

Runs in the Python 2.7 (32-bit) 'pepper' conda environment.
The server token is read from the HCTL_TOKEN environment variable.

Examples:
    python pepper_qa_game.py                 # auto-find Pepper, English
    python pepper_qa_game.py --lang de        # German
    python pepper_qa_game.py -r 1             # play a single round
"""

import os
import re
import sys
import json
import time
import argparse
import subprocess

import qi
import requests

from pepper_echo import (record_audio, download_audio, transcribe,
                         STT_BASE_URL, TOKEN_ENV_VAR, PEPPER_PW_ENV_VAR)
from pepper_parrot_game import (find_pepper, show_on_tablet, show_listening,
                                show_thinking, set_eyes, read_mem_int,
                                STOP_KEY, EXIT_KEY, HOTSPOT_SUBNET)

# HCTL chat model (OpenAI-compatible endpoint on the same server).
# The powerful gpt-oss-120b is reachable through Open WebUI's /openai router.
CHAT_API_URL = STT_BASE_URL + '/openai/chat/completions'
CHAT_MODEL = 'openai/gpt-oss-120b'

PHRASES = {
    'en': {
        'tts': 'English',
        'system': ('You are Pepper, a friendly educational robot. '
                   'Answer the question in at most two short sentences. '
                   'Answer in English.'),
        'welcome': 'Ask me a question, and I will answer!',
        'prompt': 'What is your question? Ask after the beep.',
        'listening': u'Listening...',
        'thinking_txt': u'Thinking...',
        'stop_btn': u'STOP',
        'exit_btn': u'EXIT',
        'thinking': 'Let me think.',
        'nothing': "Sorry, I could not hear you. Could you please repeat your question?",
        'hint': 'Please wait for the beep, then speak clearly and close to me.',
        'error': 'Sorry, I could not reach my brain right now.',
        'goodbye': 'Thanks for the questions! Goodbye.',
    },
    'de': {
        'tts': 'German',
        'system': ('Du bist Pepper, ein freundlicher Roboter fuer die Lehre. '
                   'Beantworte die Frage in hoechstens zwei kurzen Saetzen. '
                   'Antworte auf Deutsch.'),
        'welcome': 'Stell mir eine Frage, und ich antworte!',
        'prompt': 'Was ist deine Frage? Frag nach dem Ton.',
        'listening': u'Ich hoere zu...',
        'thinking_txt': u'Ich denke nach...',
        'stop_btn': u'STOPP',
        'exit_btn': u'ENDE',
        'thinking': 'Lass mich nachdenken.',
        'nothing': 'Entschuldigung, ich konnte dich nicht hoeren. Kannst du deine Frage bitte wiederholen?',
        'hint': 'Bitte warte auf den Ton und sprich dann deutlich und nah bei mir.',
        'error': 'Entschuldigung, ich konnte mein Gehirn gerade nicht erreichen.',
        'goodbye': 'Danke fuer die Fragen! Auf Wiedersehen.',
    },
}


def relaunch_kiosk_menu(ip):
    """Spawn the tablet menu (kiosk) so the player returns to game selection.

    Called when the tablet EXIT button was tapped and this game was NOT
    already launched by the kiosk (e.g. started directly from the PC admin
    panel) -- in that case nothing else would bring the game-picker screen
    back, so this game brings it back itself before it exits. If the kiosk
    IS the parent (args.kiosk), it already re-shows the menu on its own once
    this process ends, so we skip spawning a second one.
    """
    try:
        here = os.path.dirname(os.path.abspath(__file__))
        cmd = [sys.executable, os.path.join(here, 'pepper_kiosk.py'), '-ip', ip]
        subprocess.Popen(cmd, cwd=here)
    except Exception as err:
        print('could not bring back the tablet menu (%s)' % err)


def clean_answer(text):
    """Strip any leaked chain-of-thought so Pepper only speaks the final answer.

    gpt-oss normally puts its reasoning in a separate 'reasoning' field, but if
    a '<think>...</think>' block (or a leading 'analysis'/'final' marker) leaks
    into the content we remove it, otherwise Pepper would read its own thinking.
    """
    if not text:
        return u''
    text = re.sub(r'(?is)<think>.*?</think>', u'', text)
    text = re.sub(r'(?is)<think>.*$', u'', text)          # unterminated block
    # harmony-style channel markers, if any slip through
    text = re.sub(r'(?is)^\s*analysis.*?(?:final|assistantfinal)[:>]?\s*', u'', text)
    return text.strip()


def ask_llm(question, token, system_prompt):
    """Send the question to the HCTL chat model and return the answer text."""
    print('asking the AI model (%s) ...' % CHAT_MODEL)
    headers = {'Authorization': 'Bearer ' + token,
               'Content-Type': 'application/json'}
    payload = {
        'model': CHAT_MODEL,
        'messages': [
            {'role': 'system', 'content': system_prompt},
            {'role': 'user', 'content': question},
        ],
        'stream': False,
        'temperature': 0.7,
        'max_tokens': 200,
    }
    response = requests.post(CHAT_API_URL, headers=headers,
                             data=json.dumps(payload), timeout=90)
    if response.status_code != 200:
        raise RuntimeError('chat API %d: %s' % (response.status_code,
                                                response.text[:200]))
    message = response.json()['choices'][0]['message']
    answer = clean_answer(message.get('content') or u'')
    if not answer:
        # the model returned only reasoning / an empty answer
        raise RuntimeError('empty answer from chat model')
    print('answer: %s' % answer.encode('utf-8'))
    return answer


def main():
    parser = argparse.ArgumentParser(description='Ask-Pepper question & answer game.')
    parser.add_argument('-ip', '--ip', type=str, default='192.168.137.214',
                        help='IP of Pepper (auto-detected if this one is down)')
    parser.add_argument('-port', '--port', type=int, default=9559)
    parser.add_argument('-ssh_port', '--ssh_port', type=int, default=22)
    parser.add_argument('-u', '--username', type=str, default='nao')
    parser.add_argument('-p', '--password', type=str,
                        default=os.environ.get(PEPPER_PW_ENV_VAR),
                        help='Pepper ssh password (or set %s)' % PEPPER_PW_ENV_VAR)
    parser.add_argument('-d', '--duration', type=int, default=6,
                        help='seconds to record each question')
    parser.add_argument('-l', '--lang', type=str, default='en',
                        choices=['en', 'de'], help='game language: en or de')
    parser.add_argument('-r', '--rounds', type=int, default=0,
                        help='number of questions (0 = endless until Ctrl+C)')
    parser.add_argument('-ra', '--remote_audio_path', type=str,
                        default='/home/nao/recording.wav')
    parser.add_argument('-la', '--local_audio_path', type=str,
                        default='./recording.wav')
    parser.add_argument('--kiosk', action='store_true',
                        help='internal: set when launched by the tablet kiosk menu '
                             '(so EXIT does not spawn a second kiosk process)')
    args = parser.parse_args()

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
    try:
        memory = session.service('ALMemory')
        memory.insertData(STOP_KEY, 0)
        memory.insertData(EXIT_KEY, 0)
    except Exception:
        memory = None
    base_stop = read_mem_int(memory, STOP_KEY)
    base_exit = read_mem_int(memory, EXIT_KEY)

    def quit_requested():
        return (read_mem_int(memory, STOP_KEY) > base_stop or
                read_mem_int(memory, EXIT_KEY) > base_exit)

    # shared ssh/sftp for optional tablet display
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
    quit_game = False
    try:
        while not quit_game and (args.rounds == 0 or rounds < args.rounds):
            rounds += 1
            print('\n=== question %d ===' % rounds)
            if quit_requested():
                quit_game = True
                break
            # tell the player -- on the tablet and with blue eyes -- to speak now
            if sftp_info:
                show_listening(session, sftp_info, ph['listening'],
                               ph['stop_btn'], ph['exit_btn'])
            set_eyes(session, 0x0000FF)
            # poll_fn lets the tablet STOP/EXIT buttons interrupt the
            # recording immediately instead of waiting out the full duration
            aborted = record_audio(session, args.remote_audio_path, args.duration,
                                   cue=ph['prompt'], poll_fn=quit_requested)
            set_eyes(session, None)
            if aborted or quit_requested():
                quit_game = True
                break
            download_audio(ip, args.ssh_port, args.username, args.password,
                           args.remote_audio_path, args.local_audio_path)
            question = transcribe(args.local_audio_path, token, args.lang)

            # the STT server returns "" for silence; treat empty or trivially
            # short results as "not heard" so we never feed junk to the LLM
            # (which would answer an imaginary question -> irrelevant reply).
            alnum = [c for c in question if c.isalnum()] if question else []
            if len(alnum) < 2:
                misses += 1
                tts.say(ph['nothing'])
                if misses >= 2:                 # struggling -> give a hint once
                    tts.say(ph['hint'])
                    misses = 0
                rounds -= 1
                continue

            misses = 0
            if quit_requested():
                quit_game = True
                break
            if sftp_info:
                show_thinking(session, sftp_info, ph['thinking_txt'],
                             ph['stop_btn'], ph['exit_btn'])
            set_eyes(session, 0x00A000)         # green while thinking
            tts.say(ph['thinking'])

            try:
                answer = ask_llm(question, token, ph['system'])
            except Exception as err:
                print('LLM error: %s' % err)
                set_eyes(session, None)
                tts.say(ph['error'])
                continue
            set_eyes(session, None)

            if sftp_info:
                show_on_tablet(session, sftp_info, question, answer,
                               stop_label=ph['stop_btn'], exit_label=ph['exit_btn'])
            tts.say(answer.encode('utf-8'))
            time.sleep(1)
        exited_via_menu = quit_game and read_mem_int(memory, EXIT_KEY) > base_exit
        if quit_game and sftp_info and not exited_via_menu:
            # STOP was tapped (not EXIT): leave the tablet on a neutral,
            # button-free screen instead of a frozen page with dead buttons
            show_on_tablet(session, sftp_info, u'', ph['goodbye'],
                           bg='#12203a', show_controls=False)
        try:
            tts.say(ph['goodbye'])
        except Exception:
            pass
        if exited_via_menu and not args.kiosk:
            # EXIT was tapped and nothing else is going to bring the game
            # menu back (we were not launched by the kiosk) -- do it ourselves
            relaunch_kiosk_menu(ip)
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
    print('total questions answered: %d' % rounds)


if __name__ == '__main__':
    main()

# -*- coding: utf-8 -*-
"""
pepper_echo.py

Let Pepper listen and repeat what it heard:
    1. record audio from Pepper's microphone (ALAudioRecorder, saved on robot)
    2. download the .wav from Pepper to this computer via SFTP (paramiko)
    3. transcribe the audio to text with OpenAI Whisper
    4. Pepper says the same text back (ALTextToSpeech)

Run inside the Python 2.7 (32-bit) 'pepper' conda environment.
Speech-to-text is done by an Open WebUI server. Both its address and the
access token are read from the environment (HCTL_BASE_URL and HCTL_TOKEN);
neither is hard-coded in this file.

Example:
    conda activate pepper
    set HCTL_BASE_URL=https://your-open-webui-server
    set HCTL_TOKEN=eyJhbGci...
    python pepper_echo.py -ip 192.168.137.190 -d 6
"""

import os
import sys
import time
import wave
import audioop
import argparse

import qi
import paramiko
import requests

# Speech-to-text server (an Open WebUI instance). Its address is read from
# the environment so that no server hostname is hard-coded in this repo.
# Endpoint accepts a multipart 'file' upload and returns {"text": ...}.
BASE_URL_ENV_VAR = 'HCTL_BASE_URL'
STT_BASE_URL = os.environ.get(BASE_URL_ENV_VAR, '').rstrip('/')
STT_API_URL = STT_BASE_URL + '/api/v1/audio/transcriptions'
# environment variable that holds the Bearer token for the server
TOKEN_ENV_VAR = 'HCTL_TOKEN'
# environment variable that holds Pepper's SSH/hotspot password -- never
# hardcode the real password in source, this repo may go public
PEPPER_PW_ENV_VAR = 'PEPPER_SSH_PASSWORD'

# spoken-language code -> Pepper ALTextToSpeech language name.
# Pepper can only speak languages that are installed on the robot
# (this HCTL Pepper has: English, German, Chinese).
LANGUAGE_DICT = {
    'en': 'English',
    'de': 'German',
    'zh-cn': 'Chinese',
    # 'fr': 'French',
    # 'it': 'Italian',
    # 'es': 'Spanish',
}


def record_audio(session, remote_audio_path, duration,
                 cue='I am listening, please speak now.',
                 warmup=0.5, beep=True, poll_fn=None, poll_interval=0.2):
    """Record 'duration' seconds from Pepper's microphone into a wav file on the robot.

    'cue' is spoken right before recording so the person knows when to talk;
    pass '' to skip it (e.g. if the caller already gave its own prompt).

    The recorder needs a moment to actually start capturing, so we wait
    'warmup' seconds and play a short 'go' beep -- the person speaks after the
    beep and their first word is not clipped.

    'poll_fn', if given, is called every 'poll_interval' seconds while
    recording; if it returns a truthy value (e.g. a tablet STOP/EXIT button
    was tapped) the recording is stopped immediately instead of waiting out
    the full duration. Returns True if the recording was stopped early.
    """
    if cue:
        try:
            session.service('ALTextToSpeech').say(cue)
        except Exception:
            pass
    recorder = session.service('ALAudioRecorder')
    recorder.stopMicrophonesRecording()
    # record ALL FOUR microphones -- recording a single channel directly comes
    # out much quieter/worse; transcribe() then picks the clearest channel.
    recorder.startMicrophonesRecording(
        remote_audio_path,
        'wav',
        16000,
        [1, 1, 1, 1]
    )
    time.sleep(warmup)              # let the mics actually start capturing
    if beep:
        try:
            session.service('ALAudioPlayer').playSine(1000, 25, 0, 0.15)
        except Exception:
            pass
    print('audio recording (%d s) -- speak now ...' % duration)
    elapsed = 0.0
    aborted = False
    while elapsed < duration:
        step = min(poll_interval, duration - elapsed)
        time.sleep(step)
        elapsed += step
        if poll_fn is not None and poll_fn():
            aborted = True
            break
    recorder.stopMicrophonesRecording()
    print('audio recording finished' + (' (stopped early)' if aborted else ''))
    return aborted


def download_audio(ip, ssh_port, username, password, remote_audio_path, local_audio_path):
    """Download the recorded wav from Pepper to this computer over SFTP."""
    print('downloading recording from Pepper ...')
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    ssh.connect(ip, ssh_port, username, password)
    sftp = ssh.open_sftp()
    sftp.get(remote_audio_path, local_audio_path)
    sftp.close()
    ssh.close()
    print('download finished -> %s' % local_audio_path)


def best_mono_path(local_audio_path):
    """If the recording has several channels, pick the loudest (clearest) one
    and write it as a mono wav next to the original; return that path.

    We record all four microphones because a directly-recorded single channel
    is much quieter, but Whisper does best on ONE clean channel -- averaging
    the spaced mics smears speech. So we keep the loudest channel only.
    """
    try:
        w = wave.open(local_audio_path, 'rb')
        nch, sw, fr, nfr = (w.getnchannels(), w.getsampwidth(),
                            w.getframerate(), w.getnframes())
        raw = w.readframes(nfr)
        w.close()
    except Exception as err:
        print('could not read wav (%s); sending as-is' % err)
        return local_audio_path
    if nch <= 1:
        return local_audio_path

    # split interleaved frames into per-channel byte strings, keep loudest
    frame = nch * sw
    best_bytes, best_rms = None, -1
    for c in range(nch):
        chan = b''.join(raw[i + c * sw:i + c * sw + sw]
                        for i in range(0, len(raw) - frame + 1, frame))
        rms = audioop.rms(chan, sw)
        if rms > best_rms:
            best_rms, best_bytes = rms, chan
    mono_path = local_audio_path[:-4] + '_mono.wav' \
        if local_audio_path.lower().endswith('.wav') else local_audio_path + '.mono.wav'
    out = wave.open(mono_path, 'wb')
    out.setnchannels(1); out.setsampwidth(sw); out.setframerate(fr)
    out.writeframes(best_bytes)
    out.close()
    print('picked loudest of %d channels (rms=%d) -> %s' % (nch, best_rms, mono_path))
    return mono_path


def transcribe(local_audio_path, token, lang='auto'):
    """Send the wav to the HCTL speech-to-text server and return the text.

    lang: 'auto' lets the server detect the language (works well for clear
    English, but short/accented German is often mis-heard as English).
    Pass an ISO code like 'de' or 'en' to force the language -> much more
    reliable, e.g. German stays German instead of being translated.
    """
    send_path = best_mono_path(local_audio_path)
    print('transcribing on HCTL server (lang=%s) ...' % lang)
    headers = {'Authorization': 'Bearer ' + token}
    audio_file = open(send_path, 'rb')
    try:
        files = {'file': ('recording.wav', audio_file, 'audio/wav')}
        data = {} if lang == 'auto' else {'language': lang}
        response = requests.post(STT_API_URL, headers=headers, files=files, data=data)
    finally:
        audio_file.close()

    try:
        response_json = response.json()
    except ValueError:
        raise RuntimeError('server did not return JSON (status %s): %s'
                           % (response.status_code, response.text[:200]))
    if 'text' not in response_json:
        raise RuntimeError('server returned no text: %s' % response_json)

    text = response_json['text']
    print('heard: %s' % text.encode('utf-8'))
    return text


def detect_language(text):
    """Best-effort language detection; returns a Pepper language name or None."""
    try:
        from langdetect import detect
        code = detect(text)
        print('detected language code: %s' % code)
        return LANGUAGE_DICT.get(code, None)
    except Exception as err:
        print('language detection failed (%s), keeping current language' % err)
        return None


def repeat(session, text, lang='auto'):
    """Make Pepper say the given text, matching the spoken language if possible.

    If lang is an explicit code ('de'/'en'/...), use it directly so Pepper
    speaks with the right accent; 'auto' falls back to text-based detection.
    """
    tts = session.service('ALTextToSpeech')
    if lang != 'auto':
        language = LANGUAGE_DICT.get(lang, None)
    else:
        language = detect_language(text)
    if language is not None:
        try:
            tts.setLanguage(language)
            print('Pepper language set to %s' % language)
        except Exception as err:
            print('could not set language to %s (%s); using current' % (language, err))
    print('Pepper repeats ...')
    tts.say(text.encode('utf-8'))


def main():
    parser = argparse.ArgumentParser(description='Pepper listens and repeats what it heard.')
    parser.add_argument('-ip', '--ip', type=str, default='192.168.137.214',
                        help='IP address of Pepper')
    parser.add_argument('-port', '--port', type=int, default=9559,
                        help='naoqi port of Pepper')
    parser.add_argument('-ssh_port', '--ssh_port', type=int, default=22,
                        help='ssh port for file download')
    parser.add_argument('-u', '--username', type=str, default='nao',
                        help='Pepper ssh username')
    parser.add_argument('-p', '--password', type=str,
                        default=os.environ.get(PEPPER_PW_ENV_VAR),
                        help='Pepper ssh password (or set %s)' % PEPPER_PW_ENV_VAR)
    parser.add_argument('-d', '--duration', type=int, default=6,
                        help='recording duration in seconds')
    parser.add_argument('-l', '--lang', type=str, default='auto',
                        help="language of the speech: 'auto' (detect), "
                             "'de' (German), 'en' (English), 'zh-cn' (Chinese). "
                             "Force 'de' when speaking German for correct "
                             "transcription and accent.")
    parser.add_argument('-ra', '--remote_audio_path', type=str,
                        default='/home/nao/recording.wav',
                        help='path of the recording on the robot')
    parser.add_argument('-la', '--local_audio_path', type=str,
                        default='./recording.wav',
                        help='local path to save the downloaded recording')
    parser.add_argument('--loop', action='store_true',
                        help='keep listening and repeating until Ctrl+C')
    args = parser.parse_args()

    if not STT_BASE_URL:
        print('ERROR: set the %s environment variable first.' % BASE_URL_ENV_VAR)
        print('  Windows:  set %s=https://your-open-webui-server'
              % BASE_URL_ENV_VAR)
        sys.exit(1)
    token = os.environ.get(TOKEN_ENV_VAR)
    if not token:
        print('ERROR: set the %s environment variable first.' % TOKEN_ENV_VAR)
        print('  Windows:  set %s=eyJhbGci...' % TOKEN_ENV_VAR)
        sys.exit(1)
    if not args.password:
        print('ERROR: set the %s environment variable, or pass -p <password>.'
              % PEPPER_PW_ENV_VAR)
        sys.exit(1)

    session = qi.Session()
    try:
        session.connect('tcp://' + args.ip + ':' + str(args.port))
    except RuntimeError:
        print('cannot connect to Pepper at ip %s and port %d' % (args.ip, args.port))
        print('is Pepper on and on the same "Pepper" WiFi? is naoqi (9559) reachable?')
        sys.exit(1)
    print('connected to Pepper at %s:%d' % (args.ip, args.port))

    def one_round():
        record_audio(session, args.remote_audio_path, args.duration)
        download_audio(args.ip, args.ssh_port, args.username, args.password,
                       args.remote_audio_path, args.local_audio_path)
        text = transcribe(args.local_audio_path, token, args.lang)
        if text and text.strip():
            repeat(session, text, args.lang)
        else:
            print('nothing recognized, skipping.')

    try:
        if args.loop:
            print('loop mode: press Ctrl+C to stop.')
            while True:
                one_round()
                time.sleep(1)
        else:
            one_round()
    except KeyboardInterrupt:
        print('\nstopped by user.')


if __name__ == '__main__':
    main()

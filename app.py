#!/usr/bin/env python3

import logging
import os
from functools import wraps, update_wrapper
from pathlib import Path
from urllib.parse import urlparse

import arrow
import pychromecast
import requests
import vobject
from flask import request, abort, make_response
from flask_api import FlaskAPI, status
from flask_apscheduler import APScheduler
from flask_cors import CORS
from gtts import gTTS
from pylev3 import Levenshtein
from slugify import slugify
from webargs import fields
from webargs.flaskparser import use_args

import gh_state_machine
import string_finder
from connectors import facebook_messenger
from connectors import youtube

logging.basicConfig(level=logging.INFO)

app = FlaskAPI(__name__)
CORS(app)
app.config.from_pyfile('app_config.py')

logging.info("Connecting to ChromeCast '%s'" % app.config.get("CHROMECAST_IP"))
chromecast = pychromecast.Chromecast(app.config.get("CHROMECAST_IP"))

messenger = facebook_messenger.FacebookMessengerClient()
gh_adapter = gh_state_machine.GoogleHomeStateMachine(app.config.get("RESET_SENTENCE"), app.config.get("ERROR_SENTENCE"))

logging.info("Reading contact file: '%s'" % app.config.get("VCF_FILE"))
with open(app.config.get("VCF_FILE")) as file:
    contact_to_tel = {
        contact.contents["fn"][0].value: contact.contents["tel"][0].value
        for contact in vobject.readComponents(file)
    }
    contacts = [string_finder.normalize(item) for item in list(contact_to_tel.keys())]


def check_secret(view):
    @wraps(view)
    def inner_check_secret(*args, **kwargs):
        secret = request.args.get("secret") if request.method == 'GET' else request.get_json()["secret"]
        if secret != app.config.get("API_SECRET"):
            abort(401)
        else:
            response = make_response(view(*args, **kwargs))
            return response

    return update_wrapper(inner_check_secret, view)


@app.route('/play/<filename>', methods=['GET'])
@check_secret
def play(filename):
    mp3 = Path("./static/" + filename)
    if mp3.is_file():
        _play_audio("http://" + urlparse(request.url).netloc + "/static/" + filename)
        return {}, status.HTTP_204_NO_CONTENT
    else:
        return {"error": "%s is not a file" % mp3.absolute()}, status.HTTP_500_INTERNAL_SERVER_ERROR


@app.route('/say', methods=['GET'])
@use_args({
    "text": fields.Str(required=True),
    "lang": fields.Str(missing=app.config.get("DEFAULT_LOCALE"))
})
@check_secret
def say(args):
    _play_tts(args["text"], lang=args["lang"])
    return {}, status.HTTP_204_NO_CONTENT


@app.route('/youtube/play', methods=['GET'])
@use_args({
    "query": fields.Str(required=True)
})
@check_secret
def play_song_from_youtube(args):
    song = youtube.find_and_download_first_song(args['query'])
    song_url = "http://" + urlparse(request.url).netloc + "/static/cache/" + song.name
    logging.info("Playing %s", song_url)
    _play_audio(song_url, codec="audio/%s" % song.suffix[1:])
    return {}, status.HTTP_204_NO_CONTENT


@app.route('/facebook/messenger/say', methods=['POST'])
@use_args({
    "to": fields.Str(required=True),
    "message": fields.Str(required=True),
}, locations=('json', 'form'))
@check_secret
def say_on_facebook_messenger(args):
    return _say_on_facebook_messenger(args["to"], args["message"])


@app.route('/android/sms/send', methods=['POST'])
@use_args({
    "to": fields.Str(required=True),
    "message": fields.Str(required=True),
}, locations=('json', 'form'))
@check_secret
def send_sms(args):
    return _send_sms(args["to"], args["message"])


@app.route('/google/home/adapter', methods=['GET'])
@use_args({
    "token": fields.Str(required=True)
})
@check_secret
def adapt_to_google(args):
    message = gh_adapter.process(args['token'])
    return {"message": message}, status.HTTP_200_OK


def _play_tts(text, lang=app.config.get("DEFAULT_LOCALE"), slow=False):
    tts = gTTS(text=text, lang=lang, slow=slow)
    filename = slugify(text + "-" + lang + "-" + str(slow)) + ".mp3"
    path = "/static/cache/"
    cache_filename = "." + path + filename
    tts_file = Path(cache_filename)
    if not tts_file.is_file():
        tts.save(cache_filename)

    mp3_url = "http://" + urlparse(request.url).netloc + path + filename
    logging.info("Playing %s", mp3_url)
    _play_audio(mp3_url)


def _play_audio(audio_url, codec='audio/mp3'):
    chromecast.wait()
    chromecast.media_controller.play_media(audio_url, codec)


def _say_on_facebook_messenger(to, message):
    messenger.send_message(to, message)
    return {}, status.HTTP_204_NO_CONTENT


def _send_sms(to, message):
    try:

        result = Levenshtein.wfi(contacts, to)
        index = next(x[0] for x in enumerate(result) if x[1] <= 3)
        logging.info("Contact found: %s", contacts[index])
        tel = contact_to_tel[contacts[index]]

        r = requests.get(app.config.get("SEND_SMS_WS"), data={'value1': tel, 'value2': message})
        if r.status_code == 200:
            return {}, status.HTTP_204_NO_CONTENT
        else:
            return {"error": "the IFTTT webservice return an error (status=%s)" % r.status_code}, \
                   status.HTTP_500_INTERNAL_SERVER_ERROR

    except StopIteration:
        return {"error": "No contact named %s foud" % to}, status.HTTP_404_NOT_FOUND


def _clean_cache():
    logging.info("Cleaning cache")
    critical_time = arrow.now().shift(days=-app.config.get("AUDIO_CACHING_DAYS"))
    for item in Path('./static/cache/').glob('[!.]*'):
        if item.is_file():
            if arrow.get(item.stat().st_atime) < critical_time:
                logging.info("Removing '%s'" % item)
                os.remove(item)


if __name__ == '__main__':
    scheduler = APScheduler()
    scheduler.init_app(app)
    scheduler.start()
    gh_adapter.init_config({
        "messenger": {"method": _say_on_facebook_messenger, "dialog": [
            "OK, l'interface facebook est prête",
            "OK, le destinataire est correctement sélectionné",
            "OK, le message facebook est envoyé"
        ]},
        "sms": {"method": _send_sms, "dialog": [
            "OK, l'interface sms est prête",
            "OK, le destinataire est correctement sélectionné",
            "OK, le message SMS est envoyé"
        ]}
    }, _play_tts)
    app.run(host='0.0.0.0', port=8080, debug=app.config.get("DEBUG"), use_reloader=False)

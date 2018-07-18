#!/usr/bin/env python3

import logging
import multiprocessing
import re
from pathlib import Path

import requests
import youtube_dl

USER_AGENT = "Mozilla/5.0 (Windows NT 6.1; Win64; x64; rv:47.0) Gecko/20100101 Firefox/40.0"

PLAYLIST = multiprocessing.Queue()


def find_and_download_song(search_query, follow_playlist=False):
    r = requests.post(
        'https://www.youtube.com/results',
        data={'search_query': search_query},
        headers={"User-Agent": USER_AGENT}
    )
    result = re.search(r'href=\"/watch\?v=(.{11})', r.text).group()[-11:]
    if result:
        logging.info("Song find with tag %s" % result)
        if follow_playlist:
            _download_song_non_blocking(result)
        else:
            _download_song(result, PLAYLIST)

        return PLAYLIST

    else:
        raise Exception("No Youtube result found for '%s'" % search_query)


def _download_song_non_blocking(video_id):
    process = multiprocessing.Process(target=_download_song, args=(video_id, PLAYLIST))
    process.start()


def _download_song(video_id, playlist):
    filename = None

    def _play_hook(d):
        nonlocal filename
        if d['status'] == 'finished':
            filename = d['filename']

    with youtube_dl.YoutubeDL({
        'format': 'bestaudio/best',
        'progress_hooks': [_play_hook],
        "outtmpl": "./static/cache/%(title)s.%(ext)s"
    }) as ydl:
        ydl.download(['http://www.youtube.com/watch?v=%s' % video_id])

    playlist.put(Path(filename))

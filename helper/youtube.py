#!/usr/bin/env python3
"""One request per process. No server, browser, telemetry, or idle worker.

Anonymous by default. When the caller passes an ``auth`` path pointing at an
account file written by the OAuth flow below, the same helper signs its
requests with that account so the personal library becomes reachable. The
account file stays local; nothing is uploaded and no worker is kept alive.
"""
import json
import os
import re
import sys
from urllib.parse import urlparse, parse_qs

# Google OAuth scope for YouTube (Music). The client id/secret are supplied by
# the user (their own "TVs and Limited Input devices" OAuth client) and stored
# alongside the token in the account file, so no shared secret ships with Spun.


class SafeError(Exception):
    """An error whose message is safe to show verbatim (no URLs or tokens).

    Used for sign-in guidance, where a generic message would strand the user."""


def load_account(path):
    """Return the stored account bundle, or None when signed out or unreadable."""
    if not path or not os.path.isfile(path) or os.path.getsize(path) > 256 * 1024:
        return None
    try:
        with open(path, encoding='utf8') as file:
            data = json.load(file)
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    auth = data.get('auth')
    if not isinstance(auth, dict) or 'cookie' not in {k.lower() for k in auth}:
        return None
    return data


def store_account(path, auth):
    """Write the browser auth bundle atomically with owner-only permissions."""
    payload = json.dumps({'version': 2, 'type': 'browser', 'auth': auth},
                         ensure_ascii=False)
    tmp = path + '.tmp'
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
    fd = os.open(tmp, flags, 0o600)
    try:
        with os.fdopen(fd, 'w', encoding='utf8') as file:
            file.write(payload)
    except Exception:
        try:
            os.unlink(tmp)
        finally:
            raise
    os.replace(tmp, path)


def normalize_headers(text):
    """Accept raw headers, a "Copy as cURL", or a "Copy as fetch" blob and
    return a plain "Name: value" block for ytmusicapi.setup to parse."""
    t = (text or '').strip()
    # "Copy as cURL": collect every -H 'name: value' (Chrome puts the cookie
    # here too) plus any -b/--cookie value.
    if t.startswith('curl ') or ' -H ' in t or '\n-H' in t:
        lines = [m.group(2) for m in
                 re.finditer(r"-H\s+(['\"])(.*?)\1", t, re.S)]
        cookie = re.search(r"(?:-b|--cookie)\s+(['\"])(.*?)\1", t, re.S)
        if cookie and not any(l.lower().startswith('cookie:') for l in lines):
            lines.append('cookie: ' + cookie.group(2))
        if lines:
            return '\n'.join(lines)
    # "Copy as fetch"/"fetch (Node.js)": a JS object after "headers":
    match = re.search(r'["\']headers["\']\s*:\s*(\{.*?\})', t, re.S)
    if match:
        try:
            obj = json.loads(match.group(1))
            lines = [f'{k}: {v}' for k, v in obj.items()]
            cookie = re.search(r'["\']cookie["\']\s*:\s*["\'](.*?)["\']', t, re.S)
            if cookie and not any(l.lower().startswith('cookie:') for l in lines):
                lines.append('cookie: ' + cookie.group(1))
            return '\n'.join(lines)
        except ValueError:
            pass
    return text


def account_cookie(account):
    """Return the Cookie header value stored for a signed-in account, or ''."""
    if not account:
        return ''
    for key, value in account.get('auth', {}).items():
        if key.lower() == 'cookie':
            return value
    return ''


def write_cookiefile(cookie_header, path):
    """Write a Netscape cookie file (for yt-dlp) from a Cookie header string."""
    import time
    expiry = int(time.time()) + 315360000  # ~10 years; yt-dlp needs a value.
    lines = ['# Netscape HTTP Cookie File']
    for part in cookie_header.split(';'):
        part = part.strip()
        if '=' not in part:
            continue
        name, value = part.split('=', 1)
        name = name.strip()
        if not name:
            continue
        lines.append('\t'.join(['.youtube.com', 'TRUE', '/', 'TRUE',
                                str(expiry), name, value.strip()]))
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w', encoding='utf8') as file:
        file.write('\n'.join(lines) + '\n')


def detect_cookies_browser():
    """Pick the browser yt-dlp should read live cookies from for playback.

    YouTube rotates account cookies, so an exported header goes stale quickly;
    reading them straight from the browser stays fresh. SPUN_YOUTUBE_COOKIES_BROWSER
    overrides; otherwise prefer a currently-running browser whose profile exists,
    else the first installed one. Returns a yt-dlp browser name or ''."""
    import glob
    override = os.environ.get('SPUN_YOUTUBE_COOKIES_BROWSER', '').strip().lower()
    if override:
        return '' if override in ('none', 'off') else override
    home = os.path.expanduser('~')
    candidates = [
        ('vivaldi', ('vivaldi',), home + '/.config/vivaldi'),
        ('brave', ('brave',), home + '/.config/BraveSoftware/Brave-Browser'),
        ('chrome', ('chrome',), home + '/.config/google-chrome'),
        ('chromium', ('chromium',), home + '/.config/chromium'),
        ('edge', ('msedge', 'microsoft-edge'), home + '/.config/microsoft-edge'),
        ('opera', ('opera',), home + '/.config/opera'),
        ('firefox', ('firefox',), home + '/.mozilla/firefox'),
    ]
    running = set()
    for comm in glob.glob('/proc/[0-9]*/comm'):
        try:
            with open(comm) as file:
                running.add(file.read().strip().lower())
        except OSError:
            pass
    joined = ' '.join(running)
    for name, procs, path in candidates:
        if os.path.isdir(path) and any(p in joined for p in procs):
            return name
    for name, procs, path in candidates:
        if os.path.isdir(path):
            return name
    return ''


def pot_script_path():
    """Path to the built bgutil PO-token generator, or '' when not installed."""
    default = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                           'runtime', 'bgutil', 'server', 'build', 'generate_once.js')
    path = os.environ.get('SPUN_YOUTUBE_POT_SCRIPT', default)
    return path if path and os.path.isfile(path) else ''


def apply_playback_auth(opts, account):
    """Add cookies + PO-token options for a signed-in session to yt-dlp opts.

    Returns a temp cookie-file path to delete afterwards (or '')."""
    cookiefile = ''
    if not account:
        return cookiefile
    browser = detect_cookies_browser()
    if browser:
        opts['cookiesfrombrowser'] = (browser, None, None, None)
    else:
        cookie = account_cookie(account)
        if cookie:
            import tempfile
            handle, cookiefile = tempfile.mkstemp(prefix='spun-yt-', suffix='.txt')
            os.close(handle)
            write_cookiefile(cookie, cookiefile)
            opts['cookiefile'] = cookiefile
    pot = pot_script_path()
    if pot:
        opts['extractor_args'] = {'youtubepot-bgutilscript': {'script_path': [pot]}}
    return cookiefile


def _bounded(original):
    """Wrap a requests session so every call carries a hard timeout."""
    def bounded(*args, **kwargs):
        kwargs.setdefault('timeout', 15)
        return original(*args, **kwargs)
    return bounded


def artwork(item):
    thumbs = item.get('thumbnails') or []
    if not thumbs:
        return ''
    src = thumbs[-1].get('url', '')
    # Google returns tiny search thumbnails; ask the same image service for 544px.
    if 'googleusercontent.com/' in src or 'ggpht.com/' in src:
        src = re.sub(r'=w\d+-h\d+[^?]*$', '=w544-h544-l90-rj', src)
    return src


def normalize(item, kind='', parent=None):
    parent = parent or {}
    video = item.get('videoId') or ''
    browse = item.get('browseId') or item.get('playlistId') or ''
    kind = item.get('resultType') or kind or ('song' if video else 'playlist' if item.get('playlistId') else 'album')
    artists = item.get('artists') or parent.get('artists') or []
    if isinstance(artists, str):
        artists = [{'name': artists}]
    album = item.get('album') or {}
    if isinstance(album, str):
        album = {'name': album}
    if parent.get('type') == 'album':
        album = {'name': parent.get('title', ''), 'id': parent.get('browseId', '')}
    author = item.get('author') or {}
    artist_name = ', '.join(x.get('name', '') for x in artists)
    if not artist_name and isinstance(author, dict):
        artist_name = author.get('name', '')
    return {'id': video or browse, 'videoId': video, 'browseId': browse,
            'kind': kind, 'title': item.get('title') or item.get('name') or (item.get('artist') if isinstance(item.get('artist'), str) else '') or 'Untitled',
            'artist': artist_name,
            'artistId': next((a.get('id') for a in artists if a.get('id')), browse if kind == 'artist' else ''),
            'album': album.get('name', ''), 'albumId': album.get('id', ''),
            'art': artwork(item) or artwork(parent), 'duration': item.get('duration') or item.get('length') or '',
            'seconds': item.get('duration_seconds') or 0,
            'discNumber': item.get('discNumber') or item.get('disc_number') or 1,
            'explicit': bool(item.get('isExplicit')), 'available': item.get('isAvailable', True)}


def clean(items, kind='', parent=None):
    return [t for i in items if isinstance(i, dict) and (t := normalize(i, kind, parent))['id']]


def normalize_lyrics(data):
    from dataclasses import asdict, is_dataclass
    if is_dataclass(data): data=asdict(data)
    data=data or {}
    raw=data.get('lyrics') or ''
    if isinstance(raw,str):return {'lyrics':raw,'lines':[]}
    lines=[]
    for line in raw:
        if is_dataclass(line):line=asdict(line)
        if not isinstance(line,dict):continue
        start=line.get('start_time');end=line.get('end_time');text=line.get('text','')
        if not isinstance(start,(int,float)) or start<0 or not isinstance(text,str):continue
        lines.append({'start':int(start),'end':int(end) if isinstance(end,(int,float)) and end>=start else 0,'text':text})
    lines.sort(key=lambda line:line['start'])
    return {'lyrics':'\n'.join(line['text'] for line in lines),'lines':lines}



def run(req):
    op = req.get('op', '')
    if op == 'check':
        import ytmusicapi, yt_dlp, shutil
        if not shutil.which('node'):
            raise RuntimeError('Node.js is required for YouTube playback.')
        return {'ready': True}
    if op == 'browser_login':
        # Browser (cookie) authentication: the user pastes the request headers
        # from a signed-in music.youtube.com session. ytmusicapi.setup parses
        # them (it needs at least the cookie and x-goog-authuser headers).
        from ytmusicapi import setup as yt_setup, YTMusic
        from ytmusicapi.exceptions import YTMusicUserError
        raw = req.get('headers', '')
        if not raw.strip():
            raise SafeError('Paste the request headers copied from '
                            'music.youtube.com while signed in.')
        auth_path = req.get('auth', '')
        if not auth_path:
            raise ValueError('No account location was provided.')
        try:
            auth = json.loads(yt_setup(headers_raw=normalize_headers(raw)))
        except YTMusicUserError as exc:
            raise SafeError(str(exc)[:280])
        api = YTMusic(auth=auth)
        api._session.request = _bounded(api._session.request)
        # Validate the session, but do not hinge login on get_account_info():
        # it fails for some accounts even when the cookies are perfectly valid.
        # Fall back to a library call before rejecting the sign-in.
        name = ''
        validated = False
        try:
            name = (api.get_account_info() or {}).get('accountName', '')
            validated = True
        except Exception:
            try:
                api.get_library_playlists(limit=1)
                validated = True
            except Exception:
                validated = False
        if not validated:
            raise SafeError('These headers were not accepted. Copy them from a '
                            'POST request to music.youtube.com/youtubei (for '
                            'example /browse) while signed in, then try again.')
        store_account(auth_path, auth)
        return {'account': {'name': name}}
    if op == 'buffer':
        import yt_dlp
        from pathlib import Path
        vid = req.get('id', '')
        if not re.fullmatch(r'[A-Za-z0-9_-]{11}', vid):
            raise ValueError('Invalid YouTube song')
        directory = Path(req['directory']).resolve(strict=True)
        def bound_size(progress):
            if progress.get('downloaded_bytes', 0) > 64*1024*1024:
                raise RuntimeError('This song exceeds the 64 MiB playback buffer.')
        opts = dict(quiet=True, noprogress=True, no_warnings=True, noplaylist=True,
                    format='bestaudio[ext=m4a]/bestaudio', socket_timeout=15,
                    retries=1, extractor_retries=1, cachedir=False,
                    max_filesize=64*1024*1024,
                    outtmpl=str(directory / (vid + '.%(ext)s')),
                    progress_hooks=[bound_size])
        # js_runtimes is left unset so yt-dlp can also use Deno, which the
        # bgutil PO-token provider and current YouTube signature challenge need.
        # YouTube blocks anonymous extraction and serves SABR-only formats, so a
        # signed-in session (live browser cookies) plus a PO token is required.
        cookiefile = apply_playback_auth(opts, load_account(req.get('auth')))
        try:
            with yt_dlp.YoutubeDL(opts) as dl:
                info = dl.extract_info('https://music.youtube.com/watch?v=' + vid, download=True)
                path = Path(dl.prepare_filename(info))
        finally:
            if cookiefile:
                try:
                    os.unlink(cookiefile)
                except OSError:
                    pass
        if not path.is_file() or path.stat().st_size > 64*1024*1024:
            raise RuntimeError('Could not buffer this song within the playback limit.')
        return {'file': str(path), 'seconds': info.get('duration', 0)}
    if op == 'stream':
        # Resolve a direct, streamable audio URL (no download). The app hands it
        # to its media player, which range-streams and seeks over it.
        import yt_dlp
        import tempfile
        vid = req.get('id', '')
        if not re.fullmatch(r'[A-Za-z0-9_-]{11}', vid):
            raise ValueError('Invalid YouTube song')
        base = dict(quiet=True, no_warnings=True, noplaylist=True,
                    format='bestaudio[ext=m4a]/bestaudio', socket_timeout=15,
                    retries=1, extractor_retries=1, cachedir=False,
                    skip_download=True)
        pot = pot_script_path()
        if pot:
            base['extractor_args'] = {'youtubepot-bgutilscript': {'script_path': [pot]}}
        account = load_account(req.get('auth'))
        # Cookie strategies, least intrusive first: the account's own cookies
        # (saved at sign-in) need no open browser; a live browser is the fallback
        # for when those have rotated; anonymous is the last resort.
        tempfiles = []
        strategies = []
        if account:
            cookie = account_cookie(account)
            if cookie:
                handle, cf = tempfile.mkstemp(prefix='spun-yt-', suffix='.txt')
                os.close(handle)
                write_cookiefile(cookie, cf)
                tempfiles.append(cf)
                strategies.append({'cookiefile': cf})
            browser = detect_cookies_browser()
            if browser:
                strategies.append({'cookiesfrombrowser': (browser, None, None, None)})
        if not strategies:
            strategies.append({})
        info = None
        last_error = None
        try:
            for extra in strategies:
                opts = dict(base)
                opts.update(extra)
                try:
                    with yt_dlp.YoutubeDL(opts) as dl:
                        info = dl.extract_info(
                            'https://music.youtube.com/watch?v=' + vid, download=False)
                    if info:
                        break
                except Exception as exc:
                    last_error = exc
                    info = None
            if info is None:
                low = str(last_error or '').lower()
                if 'not a bot' in low or 'confirm you' in low or 'sign in to confirm' in low:
                    raise SafeError('YouTube is blocking playback. Your saved '
                                    'sign-in may have expired — sign in again on '
                                    'the Account tab with fresh headers. (Opening '
                                    'your signed-in browser also works.)')
                if 'country' in low or 'not available in your' in low or 'geo' in low:
                    raise SafeError('This song is not available in your country.')
                if 'private' in low or 'members-only' in low or 'premium' in low or 'purchase' in low:
                    raise SafeError('This song needs special access (private, '
                                    'members-only, or Premium).')
                raise last_error or RuntimeError('Could not resolve a stream.')
        finally:
            for cf in tempfiles:
                try:
                    os.unlink(cf)
                except OSError:
                    pass
        url = info.get('url')
        if not url:
            for fmt in info.get('formats', []):
                if fmt.get('acodec') not in (None, 'none') \
                        and fmt.get('vcodec') in ('none', None) and fmt.get('url'):
                    url = fmt['url']
        if not url:
            raise RuntimeError('Could not resolve a playable stream for this song.')
        return {'url': url, 'seconds': info.get('duration', 0)}
    from ytmusicapi import YTMusic
    account = load_account(req.get('auth'))
    if account:
        api = YTMusic(auth=account['auth'])
    else:
        api = YTMusic(requests_session=True)
    api._session.request = _bounded(api._session.request)
    if account:
        # A rejected browser session (expired or incomplete cookies) shows up as
        # 401/403 or a generic 400; turn it into a re-sign-in hint.
        inner = api._send_request
        def guarded(*call_args, **call_kwargs):
            try:
                return inner(*call_args, **call_kwargs)
            except Exception as exc:
                text = str(exc)
                if any(m in text for m in ('400', '401', '403',
                                           'INVALID_ARGUMENT', 'UNAUTHENTICATED',
                                           'PERMISSION_DENIED')):
                    raise SafeError(
                        'Your YouTube Music sign-in was rejected, most likely '
                        'because the copied session expired. Sign in again with '
                        'fresh request headers from music.youtube.com.')
                raise
        api._send_request = guarded
    if op == 'account':
        if not account:
            raise RuntimeError('Sign in to view your account.')
        info = api.get_account_info() or {}
        return {'account': {'name': info.get('accountName', ''),
                            'handle': info.get('channelHandle', '')}}
    if op == 'library':
        if not account:
            raise RuntimeError('Sign in to see your YouTube Music library.')
        kind = req.get('kind', '')
        limit = min(max(int(req.get('limit', 200)), 1), 5000)
        if kind == 'liked':
            data = api.get_liked_songs(limit=limit)
            return {'items': clean(data.get('tracks', []), 'song')}
        if kind == 'playlists':
            return {'items': clean(api.get_library_playlists(limit=None), 'playlist')}
        if kind == 'albums':
            return {'items': clean(api.get_library_albums(limit=limit), 'album')}
        if kind == 'artists':
            return {'items': clean(api.get_library_artists(limit=limit), 'artist')}
        if kind == 'subscriptions':
            return {'items': clean(api.get_library_subscriptions(limit=limit), 'artist')}
        raise ValueError('Unknown library section')
    if op == 'rate':
        if not account:
            raise RuntimeError('Sign in to like songs on YouTube Music.')
        vid = req.get('id', '')
        if not re.fullmatch(r'[A-Za-z0-9_-]{11}', vid):
            raise ValueError('Invalid YouTube song')
        api.rate_song(vid, 'LIKE' if req.get('like') else 'INDIFFERENT')
        return {'rated': True}
    if op == 'like_status':
        if not account:
            return {'status': ''}
        vid = req.get('id', '')
        if not re.fullmatch(r'[A-Za-z0-9_-]{11}', vid):
            raise ValueError('Invalid YouTube song')
        data = api.get_watch_playlist(videoId=vid, limit=1)
        tracks = data.get('tracks') or []
        status = (tracks[0].get('likeStatus') if tracks else '') or ''
        return {'status': status}
    if op == 'home':
        return {'sections': [{'title': s.get('title', ''), 'items': clean(s.get('contents', []))}
                             for s in api.get_home(limit=5) if s.get('contents')]}
    if op == 'search':
        limit = min(max(int(req.get('limit', 30)), 1), 200)
        return {'items': clean(api.search(req['query'], filter=req.get('filter') or None, limit=limit))}
    if op == 'album':
        data = api.get_album(req['id'])
        data.update(type='album', browseId=req['id'])
        return {'title': data.get('title', ''), 'artist': ', '.join(a.get('name','') for a in data.get('artists',[]) if isinstance(a,dict)), 'year': data.get('year',''), 'art': artwork(data), 'items': clean(data.get('tracks', []), 'song', data)}
    if op == 'playlist':
        data = api.get_playlist(req['id'], limit=min(int(req.get('limit', 100)), 5000))
        return {'title': data.get('title', ''), 'art': artwork(data), 'items': clean(data.get('tracks', []), 'song', data), 'total': data.get('trackCount', 0)}
    if op == 'artist':
        data = api.get_artist(req['id'])
        sections = []
        for key, title, kind in [('songs','Songs','song'),('albums','Albums','album'),('singles','Singles','album'),('videos','Videos','video'),('related','Related artists','artist')]:
            section = data.get(key) or {}
            items = section.get('results', []) if isinstance(section, dict) else section
            if items:
                sections.append({'title':title,'items':clean(items,kind)})
        return {'title':data.get('name',''), 'art':artwork(data), 'sections':sections}
    if op == 'radio':
        data = api.get_watch_playlist(videoId=req['id'], radio=True, limit=30)
        return {'items': clean(data.get('tracks', []), 'song')}
    if op == 'lyrics':
        data = api.get_watch_playlist(videoId=req['id'], limit=1)
        if not data.get('lyrics'): return {'lyrics': '', 'lines': []}
        try: result = api.get_lyrics(data['lyrics'], timestamps=True)
        except Exception: result = api.get_lyrics(data['lyrics'])
        return normalize_lyrics(result)
    if op == 'link':
        parsed = urlparse(req['url'])
        if parsed.hostname not in ('youtube.com','www.youtube.com','music.youtube.com','m.youtube.com','youtu.be'):
            raise ValueError('Paste a YouTube or YouTube Music link')
        query = parse_qs(parsed.query)
        video = (query.get('v') or [''])[0]
        if parsed.hostname == 'youtu.be':
            video = parsed.path.strip('/')
        if video:
            data = api.get_song(video).get('videoDetails', {})
            if not data:
                raise ValueError('This song is unavailable')
            t = normalize({'videoId':video,'title':data.get('title'), 'artists':[{'name':data.get('author',''),'id':data.get('channelId','')}], 'thumbnails':data.get('thumbnail',{}).get('thumbnails',[]), 'duration_seconds':int(data.get('lengthSeconds') or 0)}, 'song')
            return {'title':t['title'],'items':[t]}
        playlist = (query.get('list') or [''])[0]
        if playlist:
            return run({'op':'playlist','id':playlist,'limit':100})
        raise ValueError('This link has no song or playlist')
    raise ValueError('Unknown request')


if __name__ == '__main__':
    try:
        payload = sys.stdin.read(65537)
        if len(payload) > 65536: raise ValueError('Request is too large')
        print(json.dumps({'ok': True, **run(json.loads(payload))}, ensure_ascii=False))
    except SafeError as exc:
        # Sign-in guidance is safe to show verbatim; it carries no URLs or tokens.
        print(json.dumps({'ok': False, 'error': str(exc)[:300]}, ensure_ascii=False))
        sys.exit(1)
    except Exception as exc:
        # Keep provider diagnostics on stderr; the application displays a short,
        # actionable message without stream URLs or server response bodies.
        print(str(exc)[-1800:], file=sys.stderr)
        print(json.dumps({'ok': False, 'error': 'YouTube could not complete this request.'}))
        sys.exit(1)

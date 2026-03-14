import os, re, html, time, threading, json, tempfile, requests
from flask import Flask, Response, request, jsonify
from xml.etree import ElementTree as ET

app     = Flask(__name__)
RSS_URL = 'https://anchor.fm/s/1016b2f68/podcast/rss'
HEADERS = {'User-Agent': 'Mozilla/5.0', 'Referer': 'https://open.spotify.com/'}

rss_cache  = {}
cache_lock = threading.Lock()
bulk_running = False  # verrou — bloque les appels individuels pendant /transcribe/all

FIRESTORE_BASE = 'https://firestore.googleapis.com/v1/projects/radiohdr-39922/databases/(default)/documents'

def firestore_get(collection, doc_id):
    url = f'{FIRESTORE_BASE}/{collection}/{doc_id}'
    try:
        r = requests.get(url, timeout=10)
        if r.status_code == 200:
            return r.json().get('fields', {})
    except:
        pass
    return None

def firestore_set(collection, doc_id, data):
    url = f'{FIRESTORE_BASE}/{collection}/{doc_id}'
    fields = {k: {'stringValue': str(v)} for k, v in data.items()}
    body = json.dumps({'fields': fields})
    try:
        requests.patch(url, data=body, headers={'Content-Type': 'application/json'}, timeout=10)
    except:
        pass

# ─── Proxy image ───────────────────────────────────────────────────────────────
@app.route('/image')
def proxy_image():
    url = request.args.get('url')
    if not url:
        return Response('Missing url', status=400)
    try:
        r = requests.get(url, timeout=10, headers=HEADERS)
        return Response(r.content,
                        content_type=r.headers.get('Content-Type', 'image/jpeg'),
                        headers={'Cache-Control': 'public, max-age=86400'})
    except Exception as e:
        return Response(str(e), status=502)

# ─── RSS parsé en JSON (cache 30min) ──────────────────────────────────────────
@app.route('/rss')
def proxy_rss():
    with cache_lock:
        cached = rss_cache.get('data')
        ts     = rss_cache.get('ts', 0)
    if cached and (time.time() - ts) < 1800:
        return jsonify(cached)
    try:
        r = requests.get(RSS_URL, timeout=20, headers={'User-Agent': 'Mozilla/5.0'})
        r.raise_for_status()
        root    = ET.fromstring(r.text.encode('utf-8'))
        ns      = {'itunes': 'http://www.itunes.com/dtds/podcast-1.0.dtd'}
        channel = root.find('channel')

        img_el        = channel.find('itunes:image', ns)
        podcast_image = img_el.get('href') if img_el is not None else None

        base = os.environ.get('RAILWAY_PUBLIC_DOMAIN', 'localhost:5000')
        base = f"https://{base}" if not base.startswith('http') else base

        def px(u):
            return f"{base}/image?url={requests.utils.quote(u, safe='')}" if u else None

        items = []
        for item in channel.findall('item'):
            def g(tag):
                el = item.find(tag) or item.find(tag, ns)
                return (el.text or '').strip() if el is not None else ''

            titre = g('title').replace('<![CDATA[', '').replace(']]>', '').strip()

            if re.search(r'interview', titre, re.IGNORECASE):
                emission, sous = 'Interview', re.sub(r'interview', '', titre, flags=re.IGNORECASE).lstrip(' :-').strip()
            else:
                m = re.match(r'^(.+?)(?:\s+-\s+|:)(.*)$', titre)
                emission, sous = (m.group(1).strip(), m.group(2).strip()) if m else (titre, '')

            enc = item.find('enclosure')
            if enc is None: continue
            audio = enc.get('url')

            img       = item.find('itunes:image', ns)
            pochette  = px((img.get('href') if img is not None else None) or podcast_image)

            dr = item.find('itunes:duration', ns)
            d  = (dr.text or '').strip() if dr is not None else ''
            if d.isdigit():
                s = int(d); h, m2, s2 = s//3600, (s%3600)//60, s%60
                d = f"{h}:{h}:{m2:02d}:{s2:02d}" if h else f"{m2}:{s2:02d}"

            de = item.find('description')
            desc = html.unescape(re.sub('<[^>]+>', '', (de.text or '').replace('<![CDATA[','').replace(']]>',''))).strip() if de is not None else ''

            items.append({'id': f"rss-{len(items)}", 'titre': titre, 'emission': emission,
                          'sousTitre': sous, 'description': desc, 'date': g('pubDate'),
                          'duree': d, 'pochette': pochette, 'audioUrl': audio})

        # Propager pochettes par émission
        poch = {it['emission']: it['pochette'] for it in items if it['pochette']}
        for it in items:
            if not it['pochette']: it['pochette'] = poch.get(it['emission'])

        result = {'items': items, 'count': len(items)}
        with cache_lock:
            rss_cache['data'], rss_cache['ts'] = result, time.time()
        return jsonify(result)
    except Exception as e:
        return jsonify({'error': str(e)}), 500

# ─── Transcription Groq Whisper (cache Firestore) ─────────────────────────────
@app.route('/transcribe/<item_id>')
def transcribe(item_id):
    global bulk_running
    # 1. Vérifier le cache Firestore
    fields = firestore_get('transcriptions', item_id)
    if fields and fields.get('text', {}).get('stringValue'):
        text = fields['text']['stringValue']
        print(f'[transcription] cache hit : {item_id}')
        return jsonify({'id': item_id, 'text': text, 'cached': True})

    # Bloquer si transcription globale en cours
    if bulk_running:
        return jsonify({'error': 'Transcription globale en cours, réessaie dans quelques minutes'}), 503

    # 2. Retrouver l'audioUrl depuis le RSS
    with cache_lock:
        cached = rss_cache.get('data')
    if not cached:
        # Forcer le fetch RSS
        try:
            proxy_rss()
            with cache_lock:
                cached = rss_cache.get('data')
        except:
            pass

    audio_url = None
    if cached:
        for item in cached.get('items', []):
            if item['id'] == item_id:
                audio_url = item['audioUrl']
                break

    if not audio_url:
        return jsonify({'error': 'Replay introuvable'}), 404

    # 3. Télécharger le MP3
    groq_key = os.environ.get('HDR_GROQ_KEY')
    if not groq_key:
        return jsonify({'error': 'HDR_GROQ_KEY manquante'}), 500

    try:
        print(f'[transcription] téléchargement audio : {audio_url}')
        audio_resp = requests.get(audio_url, timeout=60, headers=HEADERS, stream=True)
        audio_resp.raise_for_status()

        with tempfile.NamedTemporaryFile(suffix='.mp3', delete=False) as tmp:
            for chunk in audio_resp.iter_content(chunk_size=8192):
                tmp.write(chunk)
            tmp_path = tmp.name

        # 4. Découper en segments avec ffmpeg CLI et transcrire
        print(f'[transcription] découpage audio en segments...')
        import subprocess

        # Obtenir la durée totale
        probe = subprocess.run(
            ['ffprobe', '-v', 'error', '-show_entries', 'format=duration',
             '-of', 'default=noprint_wrappers=1:nokey=1', tmp_path],
            capture_output=True, text=True
        )
        total_sec = float(probe.stdout.strip() or '0')
        segment_sec = 10 * 60  # 10 minutes
        starts = list(range(0, int(total_sec), segment_sec))
        print(f'[transcription] durée {total_sec:.0f}s → {len(starts)} segment(s)')

        full_text = []
        for idx, start in enumerate(starts):
            with tempfile.NamedTemporaryFile(suffix='.mp3', delete=False) as stmp:
                seg_path = stmp.name

            subprocess.run([
                'ffmpeg', '-y', '-ss', str(start), '-t', str(segment_sec),
                '-i', tmp_path, '-q:a', '5', seg_path
            ], capture_output=True)

            print(f'[transcription] segment {idx+1}/{len(starts)}...')
            seg_ok = False
            for attempt in range(2):
                with open(seg_path, 'rb') as f:
                    groq_resp = requests.post(
                        'https://api.groq.com/openai/v1/audio/transcriptions',
                        headers={'Authorization': f'Bearer {groq_key}'},
                        files={'file': ('audio.mp3', f, 'audio/mpeg')},
                        data={
                            'model':           'whisper-large-v3',
                            'language':        'fr',
                            'response_format': 'text',
                        },
                        timeout=300,
                    )
                if groq_resp.status_code == 200:
                    full_text.append(groq_resp.text.strip())
                    seg_ok = True
                    break
                elif groq_resp.status_code == 429:
                    wait = 30 * (2 ** attempt)  # 30s, 60s
                    print(f'[transcription] rate limit segment {idx+1}, attente {wait}s (tentative {attempt+1}/2)...')
                    time.sleep(wait)
                else:
                    os.unlink(seg_path)
                    os.unlink(tmp_path)
                    return jsonify({'error': f'Groq error {groq_resp.status_code}', 'detail': groq_resp.text}), 502

            os.unlink(seg_path)
            if not seg_ok:
                os.unlink(tmp_path)
                return jsonify({'error': 'Groq rate limit — réessaie dans quelques minutes'}), 429
            time.sleep(30)  # pause entre segments

        os.unlink(tmp_path)
        text = '\n\n'.join(full_text)
        print(f'[transcription] ✅ {len(text)} caractères')

        # 5. Stocker en Firestore via API REST
        firestore_set('transcriptions', item_id, {
            'text':       text,
            'item_id':    item_id,
            'audio_url':  audio_url,
            'created_at': str(int(time.time())),
        })
        print(f'[transcription] sauvegardé Firestore : {item_id}')

        return jsonify({'id': item_id, 'text': text, 'cached': False})

    except Exception as e:
        return jsonify({'error': str(e)}), 500

# ─── Transcription de tous les épisodes (tâche de fond) ──────────────────────
@app.route('/transcribe/all')
def transcribe_all():
    # Récupérer la liste des épisodes
    with cache_lock:
        cached = rss_cache.get('data')
    if not cached:
        try:
            proxy_rss()
            with cache_lock:
                cached = rss_cache.get('data')
        except:
            pass
    if not cached:
        return jsonify({'error': 'RSS non disponible'}), 500

    items = cached.get('items', [])
    results = {'total': len(items), 'done': 0, 'skipped': 0, 'errors': []}

    def run_all():
        global bulk_running
        bulk_running = True
        print('[all] 🔒 verrou activé — appels individuels bloqués')
        try:
            for item in items:
                item_id   = item['id']
                audio_url = item['audioUrl']

                # Vérifier cache Firestore
                fields = firestore_get('transcriptions', item_id)
                if fields and fields.get('text', {}).get('stringValue'):
                    print(f'[all] déjà transcrit : {item_id}')
                    results['skipped'] += 1
                    continue

                groq_key = os.environ.get('HDR_GROQ_KEY')
                if not groq_key:
                    results['errors'].append({'id': item_id, 'error': 'HDR_GROQ_KEY manquante'})
                    continue

                try:
                    print(f'[all] transcription : {item_id} — {item["titre"]}')
                    audio_resp = requests.get(audio_url, timeout=60, headers=HEADERS, stream=True)
                    audio_resp.raise_for_status()

                    with tempfile.NamedTemporaryFile(suffix='.mp3', delete=False) as tmp:
                        for chunk in audio_resp.iter_content(chunk_size=8192):
                            tmp.write(chunk)
                        tmp_path = tmp.name

                    import subprocess
                    probe = subprocess.run(
                        ['ffprobe', '-v', 'error', '-show_entries', 'format=duration',
                         '-of', 'default=noprint_wrappers=1:nokey=1', tmp_path],
                        capture_output=True, text=True
                    )
                    total_sec   = float(probe.stdout.strip() or '0')
                    segment_sec = 10 * 60
                    starts      = list(range(0, int(total_sec), segment_sec))
                    print(f'[all] {item_id} : {total_sec:.0f}s → {len(starts)} segment(s)')

                    full_text = []
                    for idx, start in enumerate(starts):
                        with tempfile.NamedTemporaryFile(suffix='.mp3', delete=False) as stmp:
                            seg_path = stmp.name
                        subprocess.run([
                            'ffmpeg', '-y', '-ss', str(start), '-t', str(segment_sec),
                            '-i', tmp_path, '-q:a', '5', seg_path
                        ], capture_output=True)

                        # Retry avec backoff exponentiel sur 429
                        seg_ok = False
                        for attempt in range(2):
                            with open(seg_path, 'rb') as f:
                                groq_resp = requests.post(
                                    'https://api.groq.com/openai/v1/audio/transcriptions',
                                    headers={'Authorization': f'Bearer {groq_key}'},
                                    files={'file': ('audio.mp3', f, 'audio/mpeg')},
                                    data={'model': 'whisper-large-v3', 'language': 'fr', 'response_format': 'text'},
                                    timeout=300,
                                )
                            if groq_resp.status_code == 200:
                                full_text.append(groq_resp.text.strip())
                                seg_ok = True
                                break
                            elif groq_resp.status_code == 429:
                                wait = 30 * (2 ** attempt)  # 30s, 60s
                                print(f'[all] rate limit segment {idx+1}, attente {wait}s (tentative {attempt+1}/2)...')
                                print(f'[all] détail Groq : {groq_resp.text[:200]}')
                                time.sleep(wait)
                            else:
                                print(f'[all] erreur Groq segment {idx+1} : {groq_resp.status_code}')
                                break

                        if not seg_ok:
                            print(f'[all] ⚠ segment {idx+1} échoué — abandon de {item_id}')
                            os.unlink(seg_path)
                            break

                        os.unlink(seg_path)
                        time.sleep(10)  # pause entre segments

                    os.unlink(tmp_path)
                    text = '\n\n'.join(full_text)

                    # Ne sauvegarder que si TOUS les segments ont réussi
                    if len(full_text) == len(starts) and text.strip():
                        firestore_set('transcriptions', item_id, {
                            'text':       text,
                            'item_id':    item_id,
                            'audio_url':  audio_url,
                            'created_at': str(int(time.time())),
                        })
                        print(f'[all] ✅ {item_id} sauvegardé ({len(text)} chars)')
                        results['done'] += 1
                    else:
                        print(f'[all] ⚠ {item_id} incomplet — {len(full_text)}/{len(starts)} segments')
                        url = f'{FIRESTORE_BASE}/transcriptions/{item_id}'
                        try:
                            r = requests.delete(url, timeout=10)
                            if r.status_code in (200, 204):
                                print(f'[all] 🗑 {item_id} supprimé de Firestore')
                        except:
                            pass

                    time.sleep(60)  # pause entre épisodes — laisse le quota Groq se recharger

                except Exception as e:
                    print(f'[all] erreur {item_id} : {e}')
                    results['errors'].append({'id': item_id, 'error': str(e)})
        finally:
            bulk_running = False
            print('[all] 🔓 verrou libéré — appels individuels autorisés')

    # Lancer en thread pour ne pas bloquer la réponse HTTP
    t = threading.Thread(target=run_all, daemon=True)
    t.start()

    return jsonify({
        'status':  'started',
        'total':   len(items),
        'message': f'Transcription de {len(items)} épisodes lancée en arrière-plan. Suivre dans les logs Railway.'
    })


@app.route('/transcribe/<item_id>/status')
def transcribe_status(item_id):
    fields = firestore_get('transcriptions', item_id)
    if fields and fields.get('text', {}).get('stringValue'):
        return jsonify({'available': True, 'cached': True})
    return jsonify({'available': False, 'cached': False})

# ─── Nettoyage des transcriptions vides ───────────────────────────────────────
@app.route('/transcribe/cleanup')
def transcribe_cleanup():
    with cache_lock:
        cached = rss_cache.get('data')
    if not cached:
        try:
            proxy_rss()
            with cache_lock:
                cached = rss_cache.get('data')
        except:
            pass
    if not cached:
        return jsonify({'error': 'RSS non disponible'}), 500

    items   = cached.get('items', [])
    deleted = []
    kept    = []

    for item in items:
        item_id = item['id']
        fields  = firestore_get('transcriptions', item_id)
        if fields is None:
            continue  # pas de doc, rien à faire
        text = fields.get('text', {}).get('stringValue', '')
        if not text.strip():
            # Supprimer le doc vide via API REST
            url = f'{FIRESTORE_BASE}/transcriptions/{item_id}'
            try:
                requests.delete(url, timeout=10)
                deleted.append(item_id)
                print(f'[cleanup] supprimé : {item_id}')
            except Exception as e:
                print(f'[cleanup] erreur suppression {item_id} : {e}')
        else:
            kept.append(item_id)

    return jsonify({
        'deleted': len(deleted),
        'kept':    len(kept),
        'ids':     deleted,
    })

@app.route('/')
def health():
    return jsonify({'status': 'ok', 'service': 'radiohdr-rss-proxy'})

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 5000)))

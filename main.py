import os, re, html, time, threading, json, tempfile, requests
from flask import Flask, Response, request, jsonify
from xml.etree import ElementTree as ET

app     = Flask(__name__)
RSS_URL = 'https://anchor.fm/s/1016b2f68/podcast/rss'
HEADERS = {'User-Agent': 'Mozilla/5.0', 'Referer': 'https://open.spotify.com/'}

rss_cache  = {}
cache_lock = threading.Lock()

# ─── Firebase init (optionnel — uniquement si GOOGLE_TOKEN_JSON présent) ──────
db = None
try:
    import firebase_admin
    from firebase_admin import credentials, firestore

    token_json = os.environ.get('GOOGLE_TOKEN_JSON')
    if token_json:
        cred_dict = json.loads(token_json)
        cred      = credentials.Certificate(cred_dict)
        firebase_admin.initialize_app(cred)
        db = firestore.client()
        print('[firebase] connecté')
except Exception as e:
    print(f'[firebase] non disponible : {e}')

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
    # 1. Vérifier le cache Firestore
    if db:
        doc = db.collection('transcriptions').document(item_id).get()
        if doc.exists:
            data = doc.to_dict()
            if data.get('text'):
                print(f'[transcription] cache hit : {item_id}')
                return jsonify({'id': item_id, 'text': data['text'], 'cached': True})

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
        return jsonify({'error': 'GROQ_API_KEY manquante'}), 500

    try:
        print(f'[transcription] téléchargement audio : {audio_url}')
        audio_resp = requests.get(audio_url, timeout=60, headers=HEADERS, stream=True)
        audio_resp.raise_for_status()

        with tempfile.NamedTemporaryFile(suffix='.mp3', delete=False) as tmp:
            for chunk in audio_resp.iter_content(chunk_size=8192):
                tmp.write(chunk)
            tmp_path = tmp.name

        # 4. Envoyer à Groq Whisper
        print(f'[transcription] envoi à Groq Whisper...')
        with open(tmp_path, 'rb') as f:
            groq_resp = requests.post(
                'https://api.groq.com/openai/v1/audio/transcriptions',
                headers={'Authorization': f'Bearer {groq_key}'},
                files={'file': ('audio.mp3', f, 'audio/mpeg')},
                data={
                    'model':    'whisper-large-v3',
                    'language': 'fr',
                    'response_format': 'text',
                },
                timeout=300,
            )
        os.unlink(tmp_path)

        if groq_resp.status_code != 200:
            return jsonify({'error': f'Groq error {groq_resp.status_code}', 'detail': groq_resp.text}), 502

        text = groq_resp.text.strip()
        print(f'[transcription] ✅ {len(text)} caractères')

        # 5. Stocker en Firestore
        if db:
            db.collection('transcriptions').document(item_id).set({
                'text':       text,
                'item_id':    item_id,
                'audio_url':  audio_url,
                'created_at': int(time.time()),
            })
            print(f'[transcription] sauvegardé Firestore : {item_id}')

        return jsonify({'id': item_id, 'text': text, 'cached': False})

    except Exception as e:
        return jsonify({'error': str(e)}), 500

# ─── Statut transcription (sans déclencher) ───────────────────────────────────
@app.route('/transcribe/<item_id>/status')
def transcribe_status(item_id):
    if not db:
        return jsonify({'available': False, 'reason': 'Firestore non configuré'})
    doc = db.collection('transcriptions').document(item_id).get()
    if doc.exists and doc.to_dict().get('text'):
        return jsonify({'available': True, 'cached': True})
    return jsonify({'available': False, 'cached': False})

@app.route('/')
def health():
    return jsonify({'status': 'ok', 'service': 'radiohdr-rss-proxy', 'firebase': db is not None})

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 5000)))
